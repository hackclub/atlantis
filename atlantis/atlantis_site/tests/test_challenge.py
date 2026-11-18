"""The weekly challenge: weeks, streaks, savers, elimination and the printer.

The tests that matter most here are the ones about *time*, because almost every
way this can be wrong is a timezone or a boundary: a week runs Monday to Sunday
in Eastern while every stored timestamp is UTC, one of the eight weeks contains
a DST change, and an hour recorded at 11:59pm on a Sunday belongs to the week
that is ending rather than the one starting a minute later.

Everything else follows from `challenge.standing`, which is recomputed from
timelapses and saver rows on every read — so these tests assert against that
rather than against anything written down at the moment a week closed.
"""

import csv
import io
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .. import challenge, weeks
from ..models import (
    AuditLog, HourRemoval, Item, Order, PrinterClaim, Profile, SaverCredit, Ship,
    WeekOutcome, WeekReminder,
)
from ..printers import item_key
from .base import (
    BaseTestCase, after_post_prep, after_week, approve_timelapse, before_the_program, during_week,
    grant_perms, in_week, make_journal, make_project, make_ship, make_timelapse,
    make_user, message_texts, ship_checklist,
)

ET = ZoneInfo("America/New_York")


def et(text):
    """An Eastern-local instant from "YYYY-MM-DD HH:MM"."""
    return datetime.fromisoformat(text).replace(tzinfo=ET)


@override_settings(CHALLENGE_START_DATE="2026-09-21", CHALLENGE_WEEKS=8)
class WeekMathTests(BaseTestCase):
    """Where the week boundaries actually fall."""

    def test_the_program_opens_at_local_midnight(self):
        self.assertEqual(weeks.starts_at(), et("2026-09-21 00:00"))

    def test_the_program_closes_at_local_midnight_after_the_extended_last_week(self):
        # Eight weeks from Mon Sep 21 would end as Sun Nov 15 turns into Nov 16;
        # week 8 was extended two days, to Tue Nov 17 11:59pm.
        self.assertEqual(weeks.ends_at(), et("2026-11-18 00:00"))

    def test_week_8_is_extended_to_tuesday(self):
        self.assertEqual(weeks.week_bounds(8), (et("2026-11-09 00:00"), et("2026-11-18 00:00")))
        self.assertEqual(weeks.deadline(8), et("2026-11-17 23:59:59"))
        self.assertEqual(weeks.week_for(et("2026-11-16 12:00")), 8)
        self.assertEqual(weeks.week_for(et("2026-11-17 23:59:59")), 8)
        self.assertEqual(weeks.week_label(8), "Week 8 · Nov 9–17")
        self.assertEqual(weeks.reminder_at(8), et("2026-11-16 18:00"))
        self.assertNotIn(8, weeks.closed_weeks(et("2026-11-17 12:00")))
        self.assertIn(8, weeks.closed_weeks(et("2026-11-18 00:00")))

    @override_settings(CHALLENGE_WEEKS=9)
    def test_an_extension_is_ignored_unless_it_is_the_last_week(self):
        self.assertEqual(weeks.week_bounds(8)[1], et("2026-11-16 00:00"))
        self.assertEqual(weeks.week_for(et("2026-11-16 12:00")), 9)

    def test_a_week_runs_monday_to_the_next_monday(self):
        opens, closes = weeks.week_bounds(1)
        self.assertEqual(opens, et("2026-09-21 00:00"))
        self.assertEqual(closes, et("2026-09-28 00:00"))

    def test_the_week_containing_the_dst_change_still_starts_at_midnight(self):
        """Week 6 spans the November fall-back, so it is 169 hours long.

        Built by adding a timedelta to an aware datetime it would start an hour
        early, which is the bug this pins.
        """
        opens, closes = weeks.week_bounds(6)
        self.assertEqual(opens, et("2026-10-26 00:00"))
        self.assertEqual(closes, et("2026-11-02 00:00"))
        self.assertEqual(opens.utcoffset(), timedelta(hours=-4))
        self.assertEqual(closes.utcoffset(), timedelta(hours=-5))
        # Converted to UTC before subtracting: two aware datetimes sharing a
        # tzinfo subtract by the wall clock, which would hide the extra hour.
        self.assertEqual(
            closes.astimezone(dt_timezone.utc) - opens.astimezone(dt_timezone.utc),
            timedelta(hours=169),
        )

    def test_every_later_week_still_opens_at_local_midnight(self):
        for index in range(1, 9):
            with self.subTest(week=index):
                opens = weeks.week_bounds(index)[0].astimezone(ET)
                self.assertEqual((opens.hour, opens.minute), (0, 0))
                self.assertEqual(opens.weekday(), 0, "a week has to open on a Monday")

    def test_the_last_second_of_a_week_is_still_that_week(self):
        self.assertEqual(weeks.week_for(et("2026-09-27 23:59:59")), 1)
        self.assertEqual(weeks.week_for(et("2026-09-28 00:00:00")), 2)

    def test_before_the_start_is_prep_and_no_week(self):
        moment = et("2026-09-20 23:59")
        self.assertIsNone(weeks.week_for(moment))
        self.assertTrue(weeks.is_prep(moment))

    def test_two_prep_weeks_follow_the_program_to_sunday_nov_29(self):
        # Week 8 ends Tue Nov 17, but the prep weeks still end on the Sunday
        # two weeks after its original one.
        self.assertEqual(weeks.shipping_closes_at(), et("2026-11-30 00:00"))
        self.assertFalse(weeks.is_post_prep(et("2026-11-17 23:59")))
        self.assertTrue(weeks.is_post_prep(et("2026-11-18 00:00")))
        self.assertTrue(weeks.is_post_prep(et("2026-11-29 23:59:59")))
        self.assertFalse(weeks.is_post_prep(et("2026-11-30 00:00")))
        self.assertFalse(weeks.shipping_has_closed(et("2026-11-29 23:59")))
        self.assertTrue(weeks.shipping_has_closed(et("2026-11-30 00:00")))

    def test_after_the_end_is_neither_a_week_nor_prep(self):
        moment = et("2026-11-18 00:00")
        self.assertIsNone(weeks.week_for(moment))
        self.assertFalse(weeks.is_prep(moment))

    def test_a_week_is_only_closed_once_it_is_over(self):
        self.assertEqual(weeks.closed_weeks(et("2026-09-27 23:59")), [])
        self.assertEqual(weeks.closed_weeks(et("2026-09-28 00:01")), [1])
        self.assertEqual(len(weeks.closed_weeks(et("2026-12-01 00:00"))), 8)

    def test_current_week_bounds_is_the_challenge_week_while_it_runs(self):
        self.assertEqual(
            weeks.current_week_bounds(et("2026-10-07 15:00")),
            weeks.week_bounds(3),
        )

    def test_current_week_bounds_falls_back_to_the_local_monday_outside_it(self):
        opens, closes = weeks.current_week_bounds(et("2026-12-03 15:00"))
        self.assertEqual(opens, et("2026-11-30 00:00"))
        self.assertEqual(closes, et("2026-12-07 00:00"))

    def test_forty_printer_hours_is_exactly_every_week(self):
        self.assertEqual(weeks.printer_hours(), weeks.WEEKLY_HOURS * weeks.week_count())
        self.assertEqual(weeks.printer_hours(), 40)


@override_settings(CHALLENGE_START_DATE="2026-09-21", CHALLENGE_WEEKS=8)
class BufferTests(BaseTestCase):
    """The two hours after a week closes, up to 2am Eastern on the Monday.

    Week 3 here runs Oct 5-11, so its buffer is Mon Oct 12, midnight to 2am.
    Week 3 rather than 1, so the grace pair's ten isn't in play.
    """

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper")
        self.project = make_project(self.user)

    def _log(self, minutes, when):
        journal = make_journal(self.project, time_spent=0)
        return make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def _weeks(self, now="2026-10-14 12:00"):
        state = challenge.standing(self.user, et(now))
        return state, state.weeks[2], state.weeks[3]

    def test_the_buffer_runs_from_midnight_to_two_on_the_monday(self):
        self.assertIsNone(weeks.buffer_week(et("2026-10-11 23:59")))
        self.assertEqual(weeks.buffer_week(et("2026-10-12 00:00")), 3)
        self.assertEqual(weeks.buffer_week(et("2026-10-12 01:59")), 3)
        self.assertIsNone(weeks.buffer_week(et("2026-10-12 02:00")))
        self.assertIsNone(weeks.buffer_week(et("2026-10-13 01:00")))
        self.assertEqual(weeks.buffer_bounds(3), (et("2026-10-12 00:00"), et("2026-10-12 02:00")))

    def test_the_buffer_is_two_hours_either_side_of_the_dst_change(self):
        # Week 6 is the one with the fall-back in it; its buffer is after it.
        start, end = weeks.buffer_bounds(6)
        self.assertEqual(end - start, timedelta(hours=2))
        self.assertEqual(weeks.buffer_week(et("2026-11-02 01:30")), 6)

    def test_there_is_a_buffer_after_the_last_week(self):
        # Week 8 is extended, so its buffer is after its Tuesday, not Sunday.
        self.assertEqual(weeks.buffer_week(et("2026-11-18 01:00")), 8)
        self.assertIsNone(weeks.buffer_week(et("2026-11-16 01:00")))
        self.assertIsNone(weeks.buffer_week(et("2026-09-21 01:00")))

    def test_buffer_time_saves_a_week_that_closed_short(self):
        self._log(240, et("2026-10-08 12:00"))
        self._log(90, et("2026-10-12 01:00"))
        _state, week3, week4 = self._weeks()
        self.assertEqual((week3.tracked_minutes, week4.tracked_minutes), (330, 0))
        self.assertTrue(week3.met)
        self.assertFalse(week3.missed)

    def test_buffer_time_goes_to_the_new_week_once_the_old_one_is_met(self):
        self._log(300, et("2026-10-08 12:00"))
        self._log(90, et("2026-10-12 01:00"))
        _state, week3, week4 = self._weeks()
        self.assertEqual((week3.tracked_minutes, week4.tracked_minutes), (300, 90))

    def test_savers_count_towards_whether_the_week_was_met(self):
        self._log(240, et("2026-10-08 12:00"))
        self._log(90, et("2026-10-12 01:00"))
        SaverCredit.objects.create(user=self.user, week_index=3)
        _state, week3, week4 = self._weeks()
        self.assertEqual((week3.tracked_minutes, week4.tracked_minutes), (240, 90))
        self.assertTrue(week3.met)

    def test_an_organizers_pass_sends_the_buffer_forward(self):
        self._log(60, et("2026-10-12 01:00"))
        WeekOutcome.objects.create(user=self.user, week_index=3, override=WeekOutcome.Override.PASS)
        _state, week3, week4 = self._weeks()
        self.assertEqual((week3.tracked_minutes, week4.tracked_minutes), (0, 60))

    def test_a_buffer_sent_forward_counts_towards_the_next_week(self):
        self._log(300, et("2026-10-08 12:00"))
        self._log(60, et("2026-10-12 01:00"))
        self._log(240, et("2026-10-15 12:00"))
        self._log(30, et("2026-10-19 01:00"))
        state = challenge.standing(self.user, et("2026-10-21 12:00"))
        # Week 4 has five with week 3's buffer in it, so its own goes to week 5.
        self.assertEqual([w.tracked_minutes for w in state.weeks[2:5]], [300, 300, 30])

    def test_a_short_week_is_not_missed_until_its_buffer_closes(self):
        self._log(240, et("2026-10-08 12:00"))
        state, week3, _week4 = self._weeks("2026-10-12 00:30")
        self.assertTrue(week3.closed)
        self.assertTrue(week3.pending)
        self.assertFalse(week3.missed)
        self.assertEqual(state.current.index, 4)

        _state, week3, _week4 = self._weeks("2026-10-12 02:00")
        self.assertTrue(week3.missed)

    def test_the_buffer_after_week_two_keeps_the_grace_pair_open(self):
        self._log(240, et("2026-09-24 12:00"))
        state = challenge.standing(self.user, et("2026-10-05 01:00"))
        self.assertFalse(state.eliminated)
        self.assertTrue(challenge.standing(self.user, et("2026-10-05 02:00")).eliminated)

    def test_the_buffer_counts_against_close_weeks_already_settled(self):
        """Applies to weeks long gone: nothing is cached, so it's retroactive."""
        self._log(240, et("2026-10-08 12:00"))
        self._log(60, et("2026-10-12 01:30"))
        WeekOutcome.objects.create(user=self.user, week_index=3, real_minutes=240, passed=False)
        self.assertFalse(challenge.standing(self.user, et("2026-11-01 12:00")).weeks[2].missed)


class StandingTests(BaseTestCase):
    """What the bars say, and who is still in."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper")
        self.project = make_project(self.user)

    def _log(self, minutes, when):
        journal = make_journal(self.project, time_spent=0)
        return make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def test_hours_land_in_the_week_they_were_recorded_in(self):
        with during_week(2):
            self._log(60, in_week(1))
            self._log(120, in_week(2))
            state = challenge.standing(self.user)
            self.assertEqual(state.weeks[0].tracked_minutes, 60)
            self.assertEqual(state.weeks[1].tracked_minutes, 120)

    def test_work_before_the_program_is_prep_and_not_in_any_week(self):
        with during_week(1):
            self._log(300, before_the_program())
            state = challenge.standing(self.user)
            self.assertEqual(state.prep_minutes, 300)
            self.assertEqual(state.weeks[0].tracked_minutes, 0)
            # Prep hours can't rescue a week or buy a printer.
            self.assertFalse(state.weeks[0].met)

    def test_footage_with_no_journal_is_not_logged_time(self):
        """Taping it into the book is what logging is."""
        with during_week(1):
            make_timelapse(self.project, journal=None, minutes=300, recorded_at=in_week(1))
            self.assertEqual(challenge.standing(self.user).weeks[0].tracked_minutes, 0)

    def test_hours_on_a_deleted_project_do_not_count(self):
        with during_week(1):
            self._log(300, in_week(1))
            self.project.deleted = True
            self.project.save(update_fields=["deleted"])
            self.assertEqual(challenge.standing(self.user).weeks[0].tracked_minutes, 0)

    def test_five_hours_meets_the_week(self):
        # Week 3: in weeks 1 and 2 the bar is the pair's ten.
        with during_week(3):
            self._log(300, in_week(3))
            week = challenge.standing(self.user).current
            self.assertTrue(week.met)
            self.assertEqual(week.percent, 100)
            self.assertEqual(week.shortfall_minutes, 0)

    def test_a_short_week_in_progress_is_not_a_missed_one(self):
        with during_week(1):
            self._log(60, in_week(1))
            state = challenge.standing(self.user)
            self.assertFalse(state.current.met)
            self.assertFalse(state.current.missed)
            self.assertFalse(state.eliminated)

    def test_a_short_week_that_closed_drops_you(self):
        with after_week(1):
            self._log(240, in_week(1))
            state = challenge.standing(self.user)
            self.assertTrue(state.eliminated)
            self.assertEqual(state.earliest_missed.index, 1)
            self.assertEqual(state.saver_hours_needed, 1)

    def test_a_part_hour_short_still_needs_a_whole_saver(self):
        with after_week(1):
            self._log(299, in_week(1))
            self.assertEqual(challenge.standing(self.user).saver_hours_needed, 1)

    def test_savers_make_up_the_difference(self):
        with after_week(1):
            self._log(240, in_week(1))
            SaverCredit.objects.create(user=self.user, week_index=1)
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)
            self.assertTrue(state.weeks[0].met)

    def test_an_organizer_can_force_a_week_either_way(self):
        with after_week(1):
            self._log(300, in_week(1))
            outcome = WeekOutcome.objects.create(user=self.user, week_index=1)
            outcome.override = WeekOutcome.Override.FAIL
            outcome.save()
            self.assertTrue(challenge.standing(self.user).eliminated)

            outcome.override = WeekOutcome.Override.PASS
            outcome.save()
            self.assertFalse(challenge.standing(self.user).eliminated)

    def test_nobody_is_out_before_the_program_starts(self):
        # The default test window puts now in the prep period.
        state = challenge.standing(self.user)
        self.assertFalse(state.started)
        self.assertFalse(state.eliminated)
        self.assertEqual(challenge.elimination_reason(self.user), "")


class GraceWeeksTests(BaseTestCase):
    """Weeks 1 and 2 are judged together: ten across the pair keeps you in."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper")
        self.project = make_project(self.user)

    def _log(self, minutes, when):
        journal = make_journal(self.project, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def test_a_short_week_1_is_not_missed_while_week_2_is_open(self):
        with during_week(2):
            self._log(60, in_week(1))
            state = challenge.standing(self.user)
            self.assertTrue(state.weeks[0].pending)
            self.assertFalse(state.weeks[0].missed)
            self.assertFalse(state.eliminated)
            self.assertEqual(challenge.elimination_reason(self.user), "")

    def test_week_2_asks_for_whatever_the_pair_still_needs(self):
        with during_week(2):
            self._log(180, in_week(1))
            live = challenge.standing(self.user).current
            self.assertEqual(live.required_minutes, 600)
            self.assertEqual(live.left_minutes, 420)
            self.assertFalse(live.done)

    def test_ten_hours_across_the_pair_meets_both(self):
        with after_week(2):
            self._log(120, in_week(1))
            self._log(480, in_week(2))
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)
            self.assertTrue(all(w.met for w in state.weeks))
            self.assertEqual(state.printer_hours, 10)

    def test_short_of_ten_drops_you_once_week_2_closes(self):
        with after_week(2):
            self._log(120, in_week(1))
            self._log(360, in_week(2))
            state = challenge.standing(self.user)
            self.assertTrue(state.eliminated)
            self.assertEqual(state.earliest_missed.index, 1)
            # Two more hours make the pair's ten; week 2 met its own five.
            self.assertEqual(state.saver_hours_needed, 2)
            self.assertEqual(state.saver_hours_outstanding, 2)

    def test_savers_on_either_week_count_towards_the_pair(self):
        with after_week(2):
            self._log(120, in_week(1))
            self._log(360, in_week(2))
            challenge.apply_saver(self.user, Item.Kind.SAVER_PAST, hours=2)
            self.assertFalse(challenge.standing(self.user).eliminated)

    def test_both_weeks_short_need_the_pair_made_up(self):
        with after_week(2):
            self._log(180, in_week(1))
            self._log(60, in_week(2))
            state = challenge.standing(self.user)
            self.assertEqual(state.saver_hours_needed, 6)
            self.assertIn("weeks 1 and 2 ended at", challenge.elimination_reason(self.user))

    def test_the_old_rule_still_passes_a_week_on_its_own_five(self):
        """The pair is a relaxation: five in a week still meets that week."""
        with after_week(2):
            self._log(60, in_week(1))
            self._log(300, in_week(2))
            state = challenge.standing(self.user)
            self.assertTrue(state.weeks[1].met)
            self.assertTrue(state.weeks[0].missed)
            self.assertEqual(state.saver_hours_needed, 4)

    def test_week_3_stands_on_its_own_five(self):
        with after_week(3):
            self._log(600, in_week(2))
            self._log(240, in_week(3))
            state = challenge.standing(self.user)
            self.assertFalse(state.weeks[2].grace)
            self.assertTrue(state.weeks[2].missed)
            self.assertEqual(state.earliest_missed.index, 3)

    def test_close_week_leaves_a_pending_week_1_alone(self):
        Profile.objects.get_or_create(user=self.user)
        with during_week(2):
            self._log(60, in_week(1))
            call_command("close_week", "--no-notify", stdout=open("/dev/null", "w"))
            self.assertFalse(WeekOutcome.objects.filter(user=self.user, week_index=1).exists())

    # ---- weeks 7 and 8, the second pair ----

    def _meet(self, *indices):
        for index in indices:
            self._log(300, in_week(index))

    @override_settings(CHALLENGE_WEEKS=8)
    def test_each_week_knows_its_own_pair(self):
        self.assertEqual(weeks.grace_pairs(), ((1, 2), (7, 8)))
        self.assertEqual(weeks.grace_pair(1), (1, 2))
        self.assertEqual(weeks.grace_pair(8), (7, 8))
        self.assertEqual(weeks.grace_pair(3), ())
        self.assertEqual(weeks.grace_pair(6), ())
        self.assertEqual(weeks.grace_minutes(7), 600)

    def test_weeks_3_to_6_stand_on_their_own_five(self):
        with after_week(8):
            self._meet(1, 2, 3, 4, 5, 6, 7, 8)
            state = challenge.standing(self.user)
            self.assertEqual(
                [w.index for w in state.weeks if w.grace], [1, 2, 7, 8],
            )

    def test_a_short_week_7_is_not_missed_while_week_8_is_open(self):
        Profile.objects.get_or_create(user=self.user)
        with during_week(8):
            self._meet(1, 2, 3, 4, 5, 6)
            self._log(60, in_week(7))
            state = challenge.standing(self.user)
            self.assertTrue(state.weeks[6].pending)
            self.assertFalse(state.eliminated)
            live = state.current
            self.assertEqual(live.required_minutes, 600)
            self.assertEqual(live.left_minutes, 540)
            self.assertEqual(live.grace_label, "weeks 7 and 8")
            # Weeks 1 and 2 are their own pair and don't count towards 7 and 8.
            self.assertEqual(live.grace_minutes, 60)
            call_command("close_week", "--no-notify", stdout=open("/dev/null", "w"))
            self.assertFalse(WeekOutcome.objects.filter(user=self.user, week_index=7).exists())

    def test_week_7_counts_down_to_the_end_of_week_8(self):
        with during_week(7):
            self._meet(1, 2, 3, 4, 5, 6)
            live = challenge.standing(self.user).current
            self.assertTrue(live.grace_due_later)
            self.assertEqual(live.grace_deadline, weeks.deadline(8))

    def test_ten_hours_across_weeks_7_and_8_meets_both(self):
        with after_week(8):
            self._meet(1, 2, 3, 4, 5, 6)
            self._log(120, in_week(7))
            self._log(480, in_week(8))
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)
            self.assertTrue(all(w.met for w in state.weeks))
            self.assertEqual(state.printer_hours, 40)

    def test_short_of_ten_across_weeks_7_and_8_drops_you(self):
        with after_week(8):
            self._meet(1, 2, 3, 4, 5, 6)
            self._log(120, in_week(7))
            self._log(360, in_week(8))
            state = challenge.standing(self.user)
            self.assertTrue(state.eliminated)
            self.assertEqual(state.earliest_missed.index, 7)
            self.assertEqual(state.saver_hours_needed, 2)
            self.assertIn(
                "weeks 7 and 8 ended at 8h 0m of 10h",
                challenge.elimination_reason(self.user),
            )

    def test_missing_both_pairs_asks_only_for_the_earliest(self):
        with after_week(8):
            self._log(60, in_week(1))
            self._meet(2, 3, 4, 5, 6)
            self._log(60, in_week(7))
            self._meet(8)
            state = challenge.standing(self.user)
            self.assertEqual([w.index for w in state.missed], [1, 7])
            # Week 1's four, not week 7's four on top.
            self.assertEqual(state.saver_hours_needed, 4)
            self.assertEqual(state.saver_hours_outstanding, 8)


class PrinterBarTests(BaseTestCase):
    """Five hours a week towards forty, and no more."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper")
        self.project = make_project(self.user)

    def _log(self, minutes, when):
        journal = make_journal(self.project, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def test_a_big_week_still_only_banks_five_hours(self):
        with after_week(1):
            self._log(60 * 30, in_week(1))
            self.assertEqual(challenge.standing(self.user).printer_hours, 5)

    def test_a_week_rescued_by_savers_banks_its_five(self):
        with after_week(1):
            self._log(240, in_week(1))
            SaverCredit.objects.create(user=self.user, week_index=1)
            self.assertEqual(challenge.standing(self.user).printer_hours, 5)

    def test_the_live_week_counts_as_it_goes(self):
        with during_week(2):
            self._log(300, in_week(1))
            self._log(120, in_week(2))
            # Week 1 banked its five; week 2 has two hours in it so far.
            self.assertEqual(challenge.standing(self.user).printer_hours, 7)

    def test_nothing_banks_while_you_are_out(self):
        with during_week(3):
            self._log(60, in_week(1))   # the grace pair closed short
            self._log(300, in_week(3))  # and this week is full
            state = challenge.standing(self.user)
            self.assertTrue(state.eliminated)
            self.assertEqual(state.printer_hours, 0)

    def test_reviving_resumes_banking(self):
        with during_week(2):
            self._log(60, in_week(1))
            self._log(300, in_week(2))
            for _ in range(4):
                SaverCredit.objects.create(user=self.user, week_index=1)
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)
            self.assertEqual(state.printer_hours, 10)

    def test_surviving_every_week_lands_exactly_on_forty(self):
        with after_week(8):
            for index in range(1, 9):
                self._log(300, in_week(index))
            state = challenge.standing(self.user)
            self.assertEqual(state.printer_hours, 40)
            self.assertEqual(state.printer_percent, 100)
            # The bar is logged time; a printer needs it shipped and finalized.
            self.assertFalse(state.printer_unlocked)


class PayoutSplitTests(BaseTestCase):
    """What an hour is worth, which depends on when it was worked."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper")
        self.project = make_project(self.user, shippable=True)

    def _journals(self, *specs):
        """specs are (minutes, recorded_at); returns the journal queryset."""
        from ..models import Journal
        for minutes, when in specs:
            journal = make_journal(self.project, time_spent=0)
            make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)
        return Journal.objects.filter(project=self.project)

    def test_prep_work_pays_the_old_flat_rate(self):
        with during_week(1):
            journals = self._journals((120, before_the_program()))
            pearls, lines, drawn = challenge.payout_breakdown(journals, 120)
            self.assertEqual(pearls, 16)  # two hours at 8
            self.assertEqual(drawn, {})
            self.assertTrue(lines[0].is_prep)

    def test_work_in_the_prep_weeks_after_the_program_pays_the_flat_rate(self):
        with after_week(1):
            journals = self._journals((120, weeks.ends_at() + timedelta(days=3)))
            pearls, lines, drawn = challenge.payout_breakdown(journals, 120)
            self.assertEqual(pearls, 16)  # two hours at 8
            self.assertEqual(drawn, {})
            self.assertTrue(lines[0].is_prep)

    def test_the_first_five_hours_of_a_week_pay_the_base_rate(self):
        with during_week(1):
            journals = self._journals((300, in_week(1)))
            pearls, _lines, drawn = challenge.payout_breakdown(journals, 300)
            self.assertEqual(pearls, 10)  # five hours at 2
            self.assertEqual(drawn, {1: 300})

    def test_hours_past_the_weekly_five_pay_the_bonus_rate(self):
        with during_week(1):
            journals = self._journals((480, in_week(1)))
            pearls, _lines, _drawn = challenge.payout_breakdown(journals, 480)
            # 5h at 2 + 3h at 7
            self.assertEqual(pearls, 10 + 21)

    def test_the_bracket_is_only_spent_once_across_two_ships(self):
        """A week's five cheap hours are the user's, not each project's."""
        with during_week(1):
            journals = self._journals((300, in_week(1)))
            _pearls, _lines, drawn = challenge.payout_breakdown(journals, 300)
            challenge.draw_brackets(self.user, drawn)

            # A second ship covering another five hours of the same week finds
            # the allowance gone and pays the bonus rate throughout.
            more = self._journals((300, in_week(1))).filter(timelapses__tracked_seconds=300 * 60)
            pearls, _lines, _drawn = challenge.payout_breakdown(
                more, 300, brackets=challenge.brackets_for(self.user)
            )
            self.assertEqual(pearls, 35)  # five hours at 7

    def test_work_spanning_prep_and_the_program_is_split(self):
        with during_week(1):
            journals = self._journals(
                (120, before_the_program()),
                (120, in_week(1)),
            )
            pearls, lines, drawn = challenge.payout_breakdown(journals, 240)
            # 2h prep at 8, 2h inside week 1's allowance at 2
            self.assertEqual(pearls, 16 + 4)
            self.assertEqual(drawn, {1: 120})
            self.assertEqual([line.label for line in lines], ["Prep weeks", "Week 1"])

    def test_the_grace_pair_is_still_paid_week_by_week(self):
        """Ten across weeks 1-2 keeps you in, but pay is each week's own five.

        2h in week 1 and 8h in week 2 is 2h at 2, then 5h at 2 and 3h at 7 —
        not ten hours at the base rate as if the pair were one week.
        """
        with during_week(3):
            journals = self._journals((120, in_week(1)), (480, in_week(2)))
            pearls, lines, drawn = challenge.payout_breakdown(journals, 600)
            self.assertEqual(pearls, 4 + 10 + 21)
            self.assertEqual(drawn, {1: 120, 2: 300})
            self.assertEqual(
                [(line.week, line.minutes, line.rate) for line in lines],
                [(1, 120, 2), (2, 300, 2), (2, 180, 7)],
            )

    def test_a_short_week_1_does_not_lend_week_2_its_cheap_hours(self):
        with during_week(3):
            journals = self._journals((60, in_week(1)), (540, in_week(2)))
            pearls, _lines, _drawn = challenge.payout_breakdown(journals, 600)
            # 1h at 2 in week 1; 5h at 2 and 4h at 7 in week 2.
            self.assertEqual(pearls, 2 + 10 + 28)

    def test_the_multiplier_scales_every_bracket(self):
        with during_week(1):
            journals = self._journals((480, in_week(1)))
            pearls, _lines, _drawn = challenge.payout_breakdown(
                journals, 480, multiplier=Decimal("2.0")
            )
            self.assertEqual(pearls, (10 + 21) * 2)

    def test_a_reviewer_paying_less_than_logged_splits_proportionally(self):
        with during_week(2):
            journals = self._journals(
                (300, in_week(1)),
                (300, in_week(2)),
            )
            # Half of what was logged, so half lands in each week.
            _pearls, _lines, drawn = challenge.payout_breakdown(journals, 300)
            self.assertEqual(drawn, {1: 150, 2: 150})

    def test_splitting_never_loses_a_minute(self):
        with during_week(3):
            journals = self._journals(
                (100, in_week(1)),
                (100, in_week(2)),
                (100, in_week(3)),
            )
            _pearls, lines, _drawn = challenge.payout_breakdown(journals, 100)
            self.assertEqual(sum(line.minutes for line in lines), 100)


class SaverPurchaseTests(BaseTestCase):
    """Buying a streak saver, which is spent the moment it is paid for."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper", layers=100)
        self.project = make_project(self.user)
        self.client.force_login(self.user)
        self.current = Item.objects.create(
            name="Streak saver", description="This week", cost=10,
            kind=Item.Kind.SAVER_CURRENT,
        )
        self.past = Item.objects.create(
            name="Streak restore", description="A missed week", cost=20,
            kind=Item.Kind.SAVER_PAST,
        )

    def _log(self, minutes, when):
        journal = make_journal(self.project, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def _buy(self, item, quantity=1):
        return self.client.post(
            reverse("order_item", args=[item.id]), {"quantity": str(quantity)}, follow=True
        )

    def test_buying_the_current_saver_credits_this_week(self):
        with during_week(1):
            self._buy(self.current)
            self.assertEqual(SaverCredit.objects.filter(user=self.user, week_index=1).count(), 1)
            self.assertEqual(Profile.objects.get(user=self.user).layers, 90)

    def test_a_saver_order_never_reaches_the_fulfillment_queue(self):
        with during_week(1):
            self._buy(self.current)
            order = Order.objects.get()
            self.assertEqual(order.status, Order.OrderStatus.FULFILLED)
            self.assertIsNotNone(order.fulfilled_at)

    def test_buying_several_at_once_credits_that_many_hours(self):
        with during_week(1):
            self._buy(self.current, quantity=3)
            self.assertEqual(SaverCredit.objects.filter(week_index=1).count(), 3)
            self.assertEqual(Profile.objects.get(user=self.user).layers, 70)

    def test_the_missed_week_saver_is_refused_when_nothing_is_missed(self):
        with during_week(1):
            response = self._buy(self.past)
            self.assertEqual(SaverCredit.objects.count(), 0)
            self.assertEqual(Profile.objects.get(user=self.user).layers, 100)
            self.assertTrue(any("haven't missed a week" in m for m in message_texts(response)))

    def test_the_missed_week_saver_is_not_offered_when_nothing_is_missed(self):
        with during_week(1):
            response = self.client.get(reverse("shop"))
            blocked = {i.name: i.blocked_reason for i in response.context["items"]}
            self.assertTrue(blocked["Streak restore"])
            self.assertEqual(blocked["Streak saver"], "")

    def test_the_missed_week_saver_revives_you(self):
        with during_week(3):
            self._log(240, in_week(1))
            self._log(300, in_week(2))
            self.assertTrue(challenge.standing(self.user).eliminated)

            self._buy(self.past)
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)
            self.assertEqual(SaverCredit.objects.get().week_index, 1)

    def test_buying_more_than_one_week_needs_rolls_onto_the_next(self):
        """Five savers on a week that was three short spend three there."""
        with during_week(4):
            self._log(180, in_week(1))  # 2h short
            self._log(120, in_week(2))  # 3h short
            self._log(300, in_week(3))  # met, so only the first two need rescuing
            self._buy(self.past, quantity=5)
            self.assertEqual(SaverCredit.objects.filter(week_index=1).count(), 2)
            self.assertEqual(SaverCredit.objects.filter(week_index=2).count(), 3)
            self.assertFalse(challenge.standing(self.user).eliminated)

    def test_buying_more_past_savers_than_there_are_missed_hours_is_refused(self):
        """Rather than running out of weeks part way and rolling the lot back."""
        with during_week(3):
            self._log(240, in_week(1))  # 1h short
            self._log(300, in_week(2))  # met
            response = self._buy(self.past, quantity=4)
            self.assertEqual(SaverCredit.objects.count(), 0)
            self.assertEqual(Profile.objects.get(user=self.user).layers, 100)
            self.assertTrue(any("only need 1 more saver hour" in m for m in message_texts(response)))

    def test_buying_exactly_what_is_outstanding_is_allowed(self):
        with during_week(3):
            self._log(240, in_week(1))  # 1h short
            self._log(180, in_week(2))  # 2h short
            self._buy(self.past, quantity=3)
            self.assertEqual(SaverCredit.objects.count(), 3)
            self.assertFalse(challenge.standing(self.user).eliminated)

    def test_the_current_saver_is_refused_outside_the_program(self):
        with after_week(1):
            response = self._buy(self.current)
            self.assertEqual(SaverCredit.objects.count(), 0)
            self.assertTrue(any("are over" in m for m in message_texts(response)))

    def test_a_saver_you_cannot_afford_takes_nothing(self):
        Profile.objects.filter(user=self.user).update(layers=5)
        with during_week(1):
            self._buy(self.current)
            self.assertEqual(SaverCredit.objects.count(), 0)
            self.assertEqual(Profile.objects.get(user=self.user).layers, 5)

    def test_refunding_a_saver_takes_the_hours_back(self):
        with during_week(3):
            self._log(240, in_week(1))
            self._log(300, in_week(2))
            self._buy(self.past)
            self.assertFalse(challenge.standing(self.user).eliminated)

            admin = grant_perms(make_user("fulfiller", slack_id="U0F"), "fulfillment")
            self.client.force_login(admin)
            self.client.post(
                reverse("update_order_status", args=[Order.objects.get().id]),
                {"action": "refunded"},
            )

            self.assertEqual(SaverCredit.objects.count(), 0)
            self.assertTrue(challenge.standing(self.user).eliminated)
            self.assertEqual(Profile.objects.get(user=self.user).layers, 100)

    def test_being_out_locks_nothing_on_the_shelves(self):
        Item.objects.create(name="Sticker", description="A sticker", cost=5)
        with during_week(3):
            self._log(240, in_week(1))
            self._log(300, in_week(2))
            response = self.client.get(reverse("shop"))
            blocked = {i.name: i.blocked_reason for i in response.context["items"]}
            self.assertEqual(blocked["Streak restore"], "")
            self.assertEqual(blocked["Streak saver"], "")
            self.assertEqual(blocked["Sticker"], "")

    def test_someone_out_can_still_order_anything(self):
        regular = Item.objects.create(name="Sticker", description="A sticker", cost=5)
        with during_week(3):
            self._log(240, in_week(1))
            self._log(300, in_week(2))
            self._buy(regular)
            cache.clear()  # the view is rate limited
            self._buy(self.current)
            self.assertEqual(Order.objects.filter(item=regular).count(), 1)
            self.assertEqual(SaverCredit.objects.filter(week_index=3).count(), 1)
            # A this-week saver doesn't undo a missed week.
            self.assertTrue(challenge.standing(self.user).eliminated)

    def test_printers_are_not_on_the_shelves(self):
        response = self.client.get(reverse("shop"))
        kinds = {item.kind for item in response.context["items"]}
        self.assertNotIn(Item.Kind.PRINTER, kinds)


class EliminationGateTests(BaseTestCase):
    """What being out of the program actually stops."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper", layers=0)
        self.project = make_project(self.user, shippable=True)
        self.client.force_login(self.user)

    def _log(self, minutes, when, journal_kwargs=None):
        journal = make_journal(self.project, time_spent=0, **(journal_kwargs or {}))
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)
        return journal

    def test_shipping_stays_open_after_a_missed_week(self):
        """Ships are where the pearls for savers come from, so being out can't block them."""
        with during_week(3):  # after the grace pair has closed
            self._log(60, in_week(1))
            self._log(120, in_week(3))  # shipping wants two hours logged
            self.assertTrue(challenge.standing(self.user).eliminated)
            self.client.post(
                reverse("ship_project", args=[self.project.id]), ship_checklist(), follow=True
            )
            self.assertEqual(Ship.objects.count(), 1)

    def test_logging_new_time_stays_open_too(self):
        with during_week(3):  # after the grace pair has closed
            self._log(60, in_week(1))
            response = self.client.post(
                reverse("create_journal", args=[self.project.id]),
                {"title": "x", "lapse_timelapses": ["abc"]},
                follow=True,
            )
            self.assertFalse(any("out of the program" in m for m in message_texts(response)))

    def test_the_reason_says_journaling_and_shipping_still_work(self):
        with during_week(3):
            self._log(60, in_week(1))
            self.assertIn("still journal and ship", challenge.elimination_reason(self.user))

    def test_shipping_works_again_once_the_week_is_paid_for(self):
        with during_week(2):
            self._log(240, in_week(1))
            SaverCredit.objects.create(user=self.user, week_index=1)
            self.assertEqual(challenge.shipping_blocked_reason(self.user), "")

    def test_shipping_stays_open_in_the_prep_weeks_after_the_program(self):
        with after_week(1):
            self._log(300, in_week(1))
            self.assertTrue(challenge.standing(self.user).ended)
            self.assertEqual(challenge.shipping_blocked_reason(self.user), "")

    def test_shipping_closes_when_the_prep_weeks_after_the_program_end(self):
        with after_post_prep(1):
            self._log(300, in_week(1))
            reason = challenge.shipping_blocked_reason(self.user)
            self.assertIn("shipping is closed", reason)

    def test_shipping_closes_at_the_end_for_someone_out_too(self):
        with after_post_prep(2):
            self._log(60, in_week(1))
            self.assertTrue(challenge.standing(self.user).eliminated)
            self.assertIn("shipping is closed", challenge.shipping_blocked_reason(self.user))

    def test_nothing_is_blocked_before_the_program_starts(self):
        self.assertEqual(challenge.shipping_blocked_reason(self.user), "")


class PrinterClaimTests(BaseTestCase):
    """The one purchase at the end of the program."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper", layers=1000)
        self.project = make_project(self.user)
        self.client.force_login(self.user)

    def _survive_the_program(self, status=Ship.ShipStatus.FINALIZED, when=None, minutes=300):
        """Week 1's five, logged and shipped. A ship's status is all that
        decides whether its hours are finalized."""
        ship = Ship.objects.create(project=self.project, status=status)
        journal = make_journal(self.project, ship=ship, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when or in_week(1))
        return ship

    def _claim(self, printer, slug="bambu"):
        return self.client.post(
            reverse("claim_printer", args=[slug]), {"printer": printer}, follow=True
        )

    def test_a_track_can_be_chosen_and_changed(self):
        self.client.post(reverse("choose_printer_track", args=["bambu"]))
        self.assertEqual(Profile.objects.get(user=self.user).printer_track, "bambu")
        cache.clear()  # the view is rate limited; this is a second, deliberate post
        self.client.post(reverse("choose_printer_track", args=["qidi"]))
        self.assertEqual(Profile.objects.get(user=self.user).printer_track, "qidi")

    def test_choosing_a_track_costs_nothing(self):
        self.client.post(reverse("choose_printer_track", args=["qidi"]))
        self.assertEqual(Profile.objects.get(user=self.user).layers, 1000)

    def test_claiming_is_closed_while_the_program_runs(self):
        with during_week(1):
            self._survive_the_program()
            response = self._claim("A1 Mini")
            self.assertFalse(PrinterClaim.objects.exists())
            self.assertTrue(any("once the eight weeks are over" in m for m in message_texts(response)))

    def test_claiming_needs_every_printer_hour(self):
        with after_week(2):
            self._survive_the_program()  # week 1 only; week 2 missed
            response = self._claim("A1 Mini")
            self.assertFalse(PrinterClaim.objects.exists())
            self.assertTrue(any("out of the program" in m for m in message_texts(response)))

    def test_logged_but_unshipped_hours_do_not_pass(self):
        with after_week(1):
            journal = make_journal(self.project, time_spent=0)
            make_timelapse(self.project, journal=journal, minutes=300, recorded_at=in_week(1))
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)
            self.assertFalse(state.passed)
            response = self._claim("A1 Mini")
            self.assertFalse(PrinterClaim.objects.exists())
            self.assertTrue(any("shipped to pass" in m for m in message_texts(response)))

    def test_shipped_but_unfinalized_hours_pass_but_cannot_claim(self):
        with after_week(1):
            self._survive_the_program(status=Ship.ShipStatus.T2_QUEUE)
            state = challenge.standing(self.user)
            self.assertTrue(state.passed)
            self.assertFalse(state.can_claim_printer)
            response = self._claim("A1 Mini")
            self.assertFalse(PrinterClaim.objects.exists())
            self.assertTrue(any("have to be finalized" in m for m in message_texts(response)))

    def test_a_rejected_ship_is_not_shipped(self):
        with after_week(1):
            self._survive_the_program(status=Ship.ShipStatus.REJECTED)
            self.assertEqual(challenge.standing(self.user).shipped_printer_minutes, 0)

    def test_finalized_prep_hours_do_not_count(self):
        with after_week(1):
            # Plenty finalized, but before week 1 and in the prep weeks after:
            # week 1 itself is met by logged time that was never shipped.
            self._survive_the_program(when=before_the_program(), minutes=600)
            self._survive_the_program(when=weeks.ends_at() + timedelta(days=2), minutes=600)
            journal = make_journal(self.project, time_spent=0)
            make_timelapse(self.project, journal=journal, minutes=300, recorded_at=in_week(1))
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)
            self.assertEqual(state.finalized_printer_minutes, 0)
            response = self._claim("A1 Mini")
            self.assertFalse(PrinterClaim.objects.exists())

    def test_finalized_time_is_one_total_across_the_weeks(self):
        with after_week(3):
            # Fifteen hours all from week 1 is all fifteen a 3-week program asks
            # for, though weeks 2 and 3 have none.
            self._survive_the_program(minutes=900)
            self.assertEqual(challenge.standing(self.user).finalized_printer_minutes, 900)

    def test_the_total_stops_at_the_printer_hours(self):
        with after_week(1):
            self._survive_the_program(minutes=1000)
            self.assertEqual(challenge.standing(self.user).finalized_printer_minutes, 300)

    def test_a_week_rescued_with_savers_counts_its_saver_hours(self):
        with after_week(1):
            self._survive_the_program(minutes=240)
            SaverCredit.objects.create(user=self.user, week_index=1)
            state = challenge.standing(self.user)
            self.assertTrue(state.can_claim_printer)

    def test_a_week_an_organizer_passed_counts_its_five(self):
        with after_week(2):
            self._survive_the_program(minutes=600)  # weeks 1 and 2's ten, all in week 1
            WeekOutcome.objects.create(
                user=self.user, week_index=2, override=WeekOutcome.Override.PASS
            )
            # Ten from week 1 and five from the pass is fifteen, but a two-week
            # program's printer only asks for ten.
            self.assertEqual(challenge.standing(self.user).finalized_printer_minutes, 600)

    def test_a_passed_week_with_nothing_shipped_still_unlocks(self):
        with after_week(1):
            WeekOutcome.objects.create(
                user=self.user, week_index=1, override=WeekOutcome.Override.PASS
            )
            state = challenge.standing(self.user)
            self.assertEqual(state.shipped_printer_minutes, 300)
            self.assertTrue(state.passed)
            self.assertTrue(state.can_claim_printer)

    def test_cut_time_does_not_count_as_finalized(self):
        with after_week(1):
            ship = self._survive_the_program()
            journal = ship.journals.get()
            approve_timelapse(journal, removals=[(journal.timelapses.get(), 0, 600)])
            state = challenge.standing(self.user)
            self.assertEqual(state.finalized_printer_minutes, 290)
            self.assertFalse(state.can_claim_printer)

    def test_the_bulk_standings_agree_on_shipped_hours(self):
        with after_week(1):
            self._survive_the_program(status=Ship.ShipStatus.T3_QUEUE)
            bulk = challenge.standings_for([self.user.id])[self.user.id]
            self.assertEqual(bulk.shipped_printer_minutes, 300)
            self.assertEqual(bulk.finalized_printer_minutes, 0)

    def _every_week(self):
        for index in range(1, 9):
            self._survive_the_program(when=in_week(index))

    @override_settings(CHALLENGE_START_DATE="2026-09-21", CHALLENGE_WEEKS=8)
    def test_buying_waits_for_the_buffer_after_the_last_week(self):
        """Week 8 ends at midnight going into Wed Nov 18; its buffer runs to 2am."""
        self._every_week()
        with patch("django.utils.timezone.now", return_value=et("2026-11-18 00:30")):
            state = challenge.standing(self.user)
            self.assertTrue(state.ended)
            self.assertFalse(state.settled)
            self.assertFalse(state.passed)
            self.assertFalse(state.can_claim_printer)
            response = self._claim("A1")
            self.assertFalse(PrinterClaim.objects.exists())
            self.assertTrue(any("go on sale Wed Nov 18 at 2:00 AM" in m for m in message_texts(response)))
        cache.clear()  # the view is rate limited; this is a second, deliberate claim
        with patch("django.utils.timezone.now", return_value=et("2026-11-18 02:00")):
            self.assertTrue(challenge.standing(self.user).can_claim_printer)
            self._claim("A1")
            self.assertTrue(PrinterClaim.objects.exists())

    @override_settings(CHALLENGE_START_DATE="2026-09-21", CHALLENGE_WEEKS=8)
    def test_unpaid_week_8_makeup_cannot_buy_in_the_buffer(self):
        """Short on weeks 7 and 8 only by makeup: not out until 2am, so the
        buffer is the one window they could have bought in."""
        from ..models import TimelapseRemoval
        self._every_week()
        ship = self._survive_the_program(when=in_week(1), minutes=120)
        recording = ship.journals.get().timelapses.get()
        review = approve_timelapse(recording.journal)
        # 90 minutes cut during week 8 is owed as makeup in week 8.
        TimelapseRemoval.objects.create(
            review=review, session=recording, start_seconds=0, end_seconds=5400,
            reason="afk", counts_toward_makeup=True, created_at=in_week(8),
        )
        with patch("django.utils.timezone.now", return_value=et("2026-11-18 00:30")):
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)
            self.assertTrue(state.all_finalized)
            self.assertFalse(state.can_claim_printer)
            self._claim("A1")
            self.assertFalse(PrinterClaim.objects.exists())
        with patch("django.utils.timezone.now", return_value=et("2026-11-18 02:00")):
            state = challenge.standing(self.user)
            self.assertTrue(state.eliminated)
            self.assertFalse(state.can_claim_printer)

    def test_an_identity_hca_says_is_ineligible_cannot_buy(self):
        Profile.objects.filter(user=self.user).update(
            verification_status="verified", ysws_eligible=False
        )
        with after_week(1):
            self._survive_the_program()
            self.assertTrue(challenge.standing(self.user).can_claim_printer)
            chart = self.client.get(reverse("printer_track", args=["bambu"]))
            self.assertNotContains(chart, 'class="star-buy"')
            response = self._claim("A1")
            self.assertFalse(PrinterClaim.objects.exists())
            self.assertTrue(any("YSWS-eligible" in m for m in message_texts(response)))

    def test_hours_on_a_locked_project_do_not_count(self):
        with after_week(1):
            self._survive_the_program()
            self.project.locked = True
            self.project.save()
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)  # still logged, so still in
            self.assertEqual(state.shipped_printer_minutes, 0)
            self.assertEqual(state.finalized_printer_minutes, 0)
            response = self._claim("A1")
            self.assertFalse(PrinterClaim.objects.exists())
            self.assertTrue(any("shipped to pass" in m for m in message_texts(response)))

    def test_claiming_spends_the_whole_path(self):
        with after_week(1):
            self._survive_the_program()
            self._claim("A1")  # A1 Mini -> A1 is a 230-pearl step
            claim = PrinterClaim.objects.get()
            self.assertEqual(claim.printer_name, "A1")
            self.assertEqual(claim.pearls_spent, 230)
            self.assertEqual(Profile.objects.get(user=self.user).layers, 770)

    def test_a_claim_becomes_a_pending_order(self):
        with after_week(1):
            self._survive_the_program()
            self._claim("A1")
            order = Order.objects.get()
            self.assertEqual(order.status, Order.OrderStatus.PENDING)
            self.assertEqual(order.item.printer_key, item_key("bambu", "A1"))
            self.assertEqual(order.cost, 230)

    def test_you_only_get_one_printer(self):
        with after_week(1):
            self._survive_the_program()
            self._claim("A1")
            cache.clear()  # the view is rate limited; this is a second, deliberate claim
            response = self._claim("P1S")
            self.assertEqual(PrinterClaim.objects.count(), 1)
            self.assertTrue(any("already claimed" in m for m in message_texts(response)))

    def test_a_printer_you_cannot_afford_is_refused(self):
        Profile.objects.filter(user=self.user).update(layers=10)
        with after_week(1):
            self._survive_the_program()
            response = self._claim("A1")
            self.assertFalse(PrinterClaim.objects.exists())
            self.assertEqual(Profile.objects.get(user=self.user).layers, 10)
            self.assertTrue(any("costs 230 pearls" in m for m in message_texts(response)))

    def test_a_printer_from_another_chart_is_refused(self):
        with after_week(1):
            self._survive_the_program()
            response = self._claim("Q2C", slug="bambu")
            self.assertFalse(PrinterClaim.objects.exists())
            self.assertTrue(any("isn't on this chart" in m for m in message_texts(response)))

    def test_every_printer_has_a_row_behind_it(self):
        from ..printers import TRACKS
        expected = sum(len(spec["printers"]) for spec in TRACKS)
        self.assertEqual(Item.objects.filter(kind=Item.Kind.PRINTER).count(), expected)


class CloseWeekCommandTests(BaseTestCase):
    """Settling a finished week, which nothing else depends on having run."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper", slack_id="U0SHIP")
        self.project = make_project(self.user)

    def _log(self, minutes, when):
        journal = make_journal(self.project, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def test_it_records_what_a_week_looked_like(self):
        with after_week(1):
            self._log(240, in_week(1))
            call_command("close_week")
            outcome = WeekOutcome.objects.get(user=self.user, week_index=1)
            self.assertEqual(outcome.real_minutes, 240)
            self.assertFalse(outcome.passed)

    def test_running_it_twice_settles_nothing_twice(self):
        with after_week(1):
            self._log(240, in_week(1))
            call_command("close_week")
            call_command("close_week")
            self.assertEqual(WeekOutcome.objects.filter(user=self.user).count(), 1)

    def test_the_dropped_are_told_once(self):
        with after_week(1):
            self._log(240, in_week(1))
            with self.settings():
                call_command("close_week")
                call_command("close_week")
            outcome = WeekOutcome.objects.get(user=self.user, week_index=1)
            self.assertIsNotNone(outcome.notified_at)

    def test_it_will_not_settle_a_week_still_running(self):
        with during_week(1):
            self._log(60, in_week(1))
            call_command("close_week")
            self.assertFalse(WeekOutcome.objects.exists())

    def test_a_dry_run_writes_nothing(self):
        with after_week(1):
            self._log(240, in_week(1))
            call_command("close_week", dry_run=True)
            self.assertFalse(WeekOutcome.objects.exists())

    def test_somebody_who_bought_their_way_back_in_is_not_told_they_are_out(self):
        with after_week(1):
            self._log(240, in_week(1))
            SaverCredit.objects.create(user=self.user, week_index=1)
            call_command("close_week")
            outcome = WeekOutcome.objects.get(user=self.user, week_index=1)
            self.assertIsNone(outcome.notified_at)


@override_settings(CHALLENGE_START_DATE="2026-09-21", CHALLENGE_WEEKS=8)
class RemindWeekCommandTests(BaseTestCase):
    """The Saturday-evening DM telling people how much is left."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper", slack_id="U0SHIP")
        self.project = make_project(self.user)
        patcher = patch("atlantis_site.views.helpers.send_slack_dm", return_value=True)
        self.dm = patcher.start()
        self.addCleanup(patcher.stop)

    def _log(self, minutes, when):
        journal = make_journal(self.project, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def _run(self, when):
        with patch("django.utils.timezone.now", return_value=et(when)):
            call_command("remind_week", stdout=StringIO())

    def test_nothing_goes_out_before_saturday_evening(self):
        self._run("2026-10-10 17:59")
        self.dm.assert_not_called()

    def test_someone_short_is_told_how_much_is_left(self):
        self._log(130, et("2026-10-06 10:00"))
        self._log(600, et("2026-09-23 10:00"))  # weeks 1-2 settled
        self._run("2026-10-10 18:05")
        self.dm.assert_called_once()
        text = self.dm.call_args.args[0]
        self.assertIn("week 3", text)
        self.assertIn("2h 10m of the 5 hours this week", text)
        self.assertIn("2h 50m", text)

    def _settle_weeks_1_to_6(self):
        self._log(600, et("2026-09-23 10:00"))
        for wednesday in ("2026-10-07", "2026-10-14", "2026-10-21", "2026-10-28"):
            self._log(300, et(f"{wednesday} 10:00"))

    def test_week_7_gets_no_reminder(self):
        """It's judged with week 8, so nothing is due when it ends."""
        self._settle_weeks_1_to_6()
        self._log(60, et("2026-11-04 10:00"))
        self._run("2026-11-07 18:05")
        self.dm.assert_not_called()

    def test_week_8s_reminder_is_about_the_pair(self):
        self._settle_weeks_1_to_6()
        self._log(120, et("2026-11-04 10:00"))
        self._log(60, et("2026-11-11 10:00"))
        # Week 8 is extended to Tuesday, so its reminder is Monday evening.
        self._run("2026-11-14 18:05")
        self.dm.assert_not_called()
        self._run("2026-11-16 18:05")
        self.dm.assert_called_once()
        text = self.dm.call_args.args[0]
        self.assertIn("week 8", text)
        self.assertIn("ends tomorrow (Tuesday)", text)
        self.assertIn("3h 0m of the 10 hours across weeks 7 and 8", text)
        self.assertIn("7h 0m", text)

    def test_each_person_hears_once(self):
        self._log(600, et("2026-09-23 10:00"))
        self._run("2026-10-10 18:05")
        self._run("2026-10-10 21:00")
        self.assertEqual(self.dm.call_count, 1)
        self.assertEqual(WeekReminder.objects.filter(user=self.user, week_index=3).count(), 1)

    def test_a_full_bar_gets_no_nudge(self):
        self._log(600, et("2026-09-23 10:00"))
        self._log(300, et("2026-10-06 10:00"))
        self._run("2026-10-10 18:05")
        self.dm.assert_not_called()

    def test_the_eliminated_get_no_nudge(self):
        self._log(60, et("2026-09-23 10:00"))  # weeks 1-2 closed short
        self._run("2026-10-10 18:05")
        self.dm.assert_not_called()

    def test_week_2_is_about_the_pairs_ten(self):
        self._log(180, et("2026-09-23 10:00"))
        self._run("2026-10-03 18:05")
        text = self.dm.call_args.args[0]
        self.assertIn("3h 0m of the 10 hours across weeks 1 and 2", text)
        self.assertIn("7h 0m", text)

    def test_week_1_is_skipped_since_nothing_is_due(self):
        self._run("2026-09-26 18:05")
        self.dm.assert_not_called()

    def test_nobody_without_a_slack_id_is_reminded(self):
        Profile.objects.filter(user=self.user).update(slack_id="")
        self._log(600, et("2026-09-23 10:00"))
        self._run("2026-10-10 18:05")
        self.dm.assert_not_called()


@override_settings(CHALLENGE_START_DATE="2026-09-21", CHALLENGE_WEEKS=8)
class BufferRescuedTests(BaseTestCase):
    """The roster's list of who the buffer put back in, and its CSV.

    Now is in week 4; week 3's buffer is Mon Oct 12, midnight to 2am.
    """

    NOW = et("2026-10-14 12:00")

    def setUp(self):
        super().setUp()
        self.organizer = grant_perms(make_user("organizer", slack_id="U0ORG"), "organizer")
        self.client.force_login(self.organizer)
        # Ten across the grace pair, then week 3 short until its buffer.
        self.rescued = self._user("rescued", "U0RESC", (600, "2026-09-24 12:00"), (240, "2026-10-08 12:00"), (90, "2026-10-12 01:00"))
        # Week 3 short with nothing in the buffer: out either way.
        self.out = self._user("out", "U0OUT", (600, "2026-09-24 12:00"), (240, "2026-10-08 12:00"))
        # Week 3 met on its own: the buffer went to week 4 and changed nothing.
        self.fine = self._user("fine", "U0FINE", (600, "2026-09-24 12:00"), (300, "2026-10-08 12:00"), (90, "2026-10-12 01:00"))

    def _user(self, name, slack_id, *logs):
        user = make_user(name, slack_id=slack_id)
        project = make_project(user)
        for minutes, when in logs:
            journal = make_journal(project, time_spent=0)
            make_timelapse(project, journal=journal, minutes=minutes, recorded_at=et(when))
        return user

    def _get(self, name, **params):
        with patch("django.utils.timezone.now", return_value=self.NOW):
            return self.client.get(reverse(name), params)

    def test_only_people_out_without_the_buffer_and_in_with_it(self):
        with patch("django.utils.timezone.now", return_value=self.NOW):
            rescued = challenge.rescued_by_buffer()
        self.assertEqual(set(rescued), {self.rescued.id})

    def test_an_open_buffer_rescues_nobody_it_has_not_filled(self):
        """While the buffer runs, every short week is pending rather than
        missed — which isn't the buffer having saved it. Only what's already
        in the buffer counts."""
        rescued = challenge.rescued_by_buffer(et("2026-10-12 01:30"))
        self.assertEqual(set(rescued), {self.rescued.id})

    def test_the_roster_filters_to_them_and_names_the_week(self):
        response = self._get("challenge_dash", show="buffer")
        rows = response.context["rows"]
        self.assertEqual([row["user"].username for row in rows], ["rescued"])
        self.assertEqual(rows[0]["buffer_weeks"], [3])
        self.assertEqual(response.context["rescued_count"], 1)
        self.assertContains(response, "back in via buffer: W3")

    def test_the_csv_lists_them(self):
        response = self._get("buffer_rescued_csv")
        self.assertEqual(response["Content-Type"], "text/csv")
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        # The columns dm_unfinished_hours.py reads.
        self.assertEqual(rows, [{
            "slack_id": "U0RESC", "username": "rescued", "user_id": str(self.rescued.id),
            "week": "3", "progress": "5h 30m", "left": "0h 0m",
        }])
        self.assertTrue(AuditLog.objects.filter(action="export_buffer_rescued").exists())

    def test_the_csv_leaves_out_the_banned(self):
        self.rescued.hackclub_profile.banned = True
        self.rescued.hackclub_profile.save()
        response = self._get("buffer_rescued_csv")
        self.assertEqual(list(csv.DictReader(io.StringIO(response.content.decode()))), [])

    def test_the_csv_needs_the_organizer_permission(self):
        self.client.force_login(grant_perms(make_user("t1", slack_id="U0T1"), "t1_review"))
        self.assertEqual(self._get("buffer_rescued_csv").status_code, 302)


class ChallengeAdminTests(BaseTestCase):
    """The organizer's roster and the levers on it."""

    def setUp(self):
        super().setUp()
        self.organizer = grant_perms(make_user("organizer", slack_id="U0ORG"), "organizer")
        self.shipper = make_user("shipper")
        self.project = make_project(self.shipper)
        self.client.force_login(self.organizer)

    def _log(self, minutes, when):
        journal = make_journal(self.project, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def test_the_roster_needs_the_organizer_permission(self):
        self.client.force_login(grant_perms(make_user("t1", slack_id="U0T1"), "t1_review"))
        self.assertEqual(self.client.get(reverse("challenge_dash")).status_code, 302)

    def test_the_roster_lists_who_is_out(self):
        with during_week(3):  # after the grace pair has closed
            self._log(60, in_week(1))
            response = self.client.get(reverse("challenge_dash"))
            rows = {row["user"].username: row for row in response.context["rows"]}
            self.assertTrue(rows["shipper"]["standing"].eliminated)

    def test_it_can_filter_to_just_the_dropped(self):
        survivor = make_user("survivor", slack_id="U0SURV")
        survivor_project = make_project(survivor)
        with during_week(3):  # after the grace pair has closed
            self._log(60, in_week(1))
            journal = make_journal(survivor_project, time_spent=0)
            make_timelapse(survivor_project, journal=journal, minutes=600, recorded_at=in_week(1))

            listed = [
                row["user"].username
                for row in self.client.get(
                    reverse("challenge_dash"), {"show": "out"}
                ).context["rows"]
            ]
            self.assertIn("shipper", listed)
            self.assertNotIn("survivor", listed)

    def test_it_can_filter_to_who_has_done_this_week(self):
        finisher = make_user("finisher", slack_id="U0FIN")
        finisher_project = make_project(finisher)
        with during_week(1):
            self._log(60, in_week(1))
            journal = make_journal(finisher_project, time_spent=0)
            make_timelapse(finisher_project, journal=journal, minutes=300, recorded_at=in_week(1))

            listed = [
                row["user"].username
                for row in self.client.get(
                    reverse("challenge_dash"), {"show": "met"}
                ).context["rows"]
            ]
            self.assertIn("finisher", listed)
            self.assertNotIn("shipper", listed)

    def test_search_finds_somebody_by_the_slack_name_the_roster_shows(self):
        make_user("U12345", slack_id="U0SLACK", slack_username="orpheus")
        with during_week(1):
            for query in ("orph", "U123"):
                listed = [
                    row["user"].username
                    for row in self.client.get(
                        reverse("challenge_dash"), {"q": query}
                    ).context["rows"]
                ]
                self.assertEqual(listed, ["U12345"], query)

    def test_granting_hours_revives_somebody(self):
        with during_week(2):
            self._log(240, in_week(1))
            self.client.post(reverse("grant_saver", args=[self.shipper.id]), {
                "week": "1", "hours": "1", "note": "Lapse was down",
            })
            self.assertFalse(challenge.standing(self.shipper).eliminated)
            credit = SaverCredit.objects.get()
            self.assertEqual(credit.source, SaverCredit.Source.ADMIN)
            self.assertEqual(credit.granted_by, self.organizer)

    def test_an_absurd_grant_is_refused(self):
        with during_week(2):
            self.client.post(reverse("grant_saver", args=[self.shipper.id]), {
                "week": "1", "hours": "9999",
            })
            self.assertFalse(SaverCredit.objects.exists())

    def test_removing_hours_can_fail_a_met_week(self):
        with during_week(4):
            self._log(300, in_week(3))
            self.assertTrue(challenge.standing(self.shipper).weeks[2].met)
            self.client.post(reverse("remove_hours", args=[self.shipper.id]), {
                "week": "3", "hours": "1", "note": "idle recording",
            })
            week = challenge.standing(self.shipper).weeks[2]
            self.assertEqual(week.removed_hours, 1)
            self.assertEqual(week.credited_minutes, 240)
            self.assertTrue(week.missed)
            removal = HourRemoval.objects.get()
            self.assertEqual(removal.removed_by, self.organizer)
            self.assertEqual(removal.note, "idle recording")
            self.assertTrue(AuditLog.objects.filter(action="remove_week_hours").exists())

    def test_removed_hours_count_on_the_roster_too(self):
        with during_week(4):
            self._log(300, in_week(3))
            challenge.remove_hours(self.shipper, 3, 2, self.organizer)
            state = challenge.standings_for([self.shipper.id])[self.shipper.id]
            self.assertEqual(state.weeks[2].credited_minutes, 180)

    def test_removing_hours_takes_from_savers_as_well(self):
        with during_week(4):
            self._log(180, in_week(3))
            challenge.grant_saver_hours(self.shipper, 3, 2, self.organizer)
            self.assertTrue(challenge.standing(self.shipper).weeks[2].met)
            challenge.remove_hours(self.shipper, 3, 1, self.organizer)
            self.assertFalse(challenge.standing(self.shipper).weeks[2].met)

    def test_cannot_remove_more_than_the_week_has(self):
        with during_week(4):
            self._log(90, in_week(3))
            self.client.post(reverse("remove_hours", args=[self.shipper.id]), {
                "week": "3", "hours": "3",
            })
            self.assertFalse(HourRemoval.objects.exists())
            # A part-hour rounds up, so all of it can still go.
            self.client.post(reverse("remove_hours", args=[self.shipper.id]), {
                "week": "3", "hours": "2",
            })
            self.assertEqual(challenge.standing(self.shipper).weeks[2].credited_minutes, 0)

    def test_an_empty_week_has_nothing_to_remove(self):
        with during_week(4):
            self.client.post(reverse("remove_hours", args=[self.shipper.id]), {
                "week": "3", "hours": "1",
            })
            self.assertFalse(HourRemoval.objects.exists())

    def test_undoing_a_removal_gives_the_hours_back(self):
        with during_week(4):
            self._log(300, in_week(3))
            removal = challenge.remove_hours(self.shipper, 3, 1, self.organizer)
            self.client.post(reverse("undo_hour_removal", args=[self.shipper.id, removal.id]))
            self.assertFalse(HourRemoval.objects.exists())
            self.assertTrue(challenge.standing(self.shipper).weeks[2].met)

    def test_undo_only_touches_that_users_removals(self):
        other = make_user("other", slack_id="U0OTHER")
        removal = HourRemoval.objects.create(user=other, week_index=3, hours=1)
        response = self.client.post(reverse("undo_hour_removal", args=[self.shipper.id, removal.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(HourRemoval.objects.exists())

    def test_removing_hours_needs_the_organizer_permission(self):
        self.client.force_login(grant_perms(make_user("t1", slack_id="U0T1"), "t1_review"))
        with during_week(4):
            self._log(300, in_week(3))
            self.client.post(reverse("remove_hours", args=[self.shipper.id]), {
                "week": "3", "hours": "1",
            })
        self.assertFalse(HourRemoval.objects.exists())

    def test_the_user_page_lists_removals(self):
        with during_week(4):
            self._log(300, in_week(3))
            challenge.remove_hours(self.shipper, 3, 1, self.organizer, "idle recording")
            response = self.client.get(reverse("challenge_user", args=[self.shipper.id]))
            self.assertContains(response, "idle recording")
            self.assertContains(response, reverse("undo_hour_removal", args=[self.shipper.id, HourRemoval.objects.get().id]))

    def test_overriding_a_week_is_recorded_with_who_did_it(self):
        with during_week(2):
            self._log(60, in_week(1))
            self.client.post(reverse("override_week", args=[self.shipper.id]), {
                "week": "1", "override": WeekOutcome.Override.PASS, "note": "manual review",
            })
            outcome = WeekOutcome.objects.get(user=self.shipper, week_index=1)
            self.assertEqual(outcome.override, WeekOutcome.Override.PASS)
            self.assertEqual(outcome.override_by, self.organizer)
            self.assertFalse(challenge.standing(self.shipper).eliminated)


class StreakPanelTests(BaseTestCase):
    """What the deck and the projects page show."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper")
        self.project = make_project(self.user)
        self.client.force_login(self.user)

    def _log(self, minutes, when):
        journal = make_journal(self.project, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def test_the_dashboard_carries_a_deadline_to_count_down_to(self):
        with during_week(1):
            response = self.client.get(reverse("dashboard"))
            self.assertEqual(
                response.context["week_deadline"], weeks.deadline(1).isoformat()
            )
            self.assertContains(response, "data-week-deadline")

    def test_the_dashboard_shows_both_bars(self):
        with during_week(2):
            self._log(300, in_week(1))
            self._log(60, in_week(2))
            response = self.client.get(reverse("dashboard"))
            self.assertEqual(response.context["week"].credited_minutes, 60)
            self.assertEqual(response.context["standing"].printer_hours, 6)

    def test_the_dashboard_says_you_are_out(self):
        with during_week(3):  # after the grace pair has closed
            self._log(60, in_week(1))
            response = self.client.get(reverse("dashboard"))
            self.assertContains(response, "out of the program")
            self.assertContains(response, "still journal and ship")

    def test_week_1_says_the_ten_are_not_due_when_the_countdown_ends(self):
        with during_week(1):
            self._log(120, in_week(1))
            response = self.client.get(reverse("dashboard"))
            self.assertContains(response, "of 10 hours across weeks 1 and 2")
            self.assertContains(response, "Weeks 1 and 2 count together: due")
            deadline = weeks.deadline(2).astimezone(weeks.zone())
            self.assertContains(response, f"due {deadline:%a %b} {deadline.day}, 11:59 PM Eastern")

    def test_week_2_says_the_ten_are_due_when_the_countdown_ends(self):
        with during_week(2):
            self._log(120, in_week(1))
            response = self.client.get(reverse("dashboard"))
            self.assertContains(response, "count together: due when this countdown ends")
            # No second, later deadline: week 2 is the end of the pair.
            self.assertNotContains(response, "Eastern.</p>")

    def test_week_3_has_no_grace_note(self):
        with during_week(3):
            self._log(600, in_week(1))
            response = self.client.get(reverse("dashboard"))
            self.assertContains(response, "of 5 hours")
            self.assertNotContains(response, "count together")

    def test_the_fallback_deadline_is_in_eastern(self):
        with during_week(3):
            self._log(600, in_week(1))
            response = self.client.get(reverse("dashboard"))
            # Sunday, unless during_week had to start the weeks a day early.
            day = weeks.deadline(3).astimezone(weeks.zone()).strftime("%a")
            self.assertContains(response, f"ends {day} 11:59 PM")

    def test_before_the_start_it_says_so_instead_of_counting_down(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["week"], None)
        self.assertEqual(response.context["week_deadline"], "")


class ShippedPanelTests(BaseTestCase):
    """The deck's readout of the forty hours passing is judged on: shipped
    time from inside the weeks, by week, with what's waiting to be shipped."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper")
        self.project = make_project(self.user)
        self.client.force_login(self.user)

    def _ship(self, minutes, when, status=Ship.ShipStatus.T2_QUEUE, project=None):
        project = project or self.project
        ship = Ship.objects.create(project=project, status=status)
        journal = make_journal(project, ship=ship, time_spent=0)
        make_timelapse(project, journal=journal, minutes=minutes, recorded_at=when)
        return ship

    def _log(self, minutes, when, project=None):
        project = project or self.project
        journal = make_journal(project, time_spent=0)
        make_timelapse(project, journal=journal, minutes=minutes, recorded_at=when)

    def test_hours_count_across_the_weeks_however_they_are_spread(self):
        """Forty across weeks 1–8, not five from each: a heavy week covers a
        light one."""
        with during_week(5):
            self._ship(600, in_week(3))
            self._ship(60, in_week(4))
            self.assertEqual(challenge.standing(self.user).shipped_printer_minutes, 660)

    @override_settings(CHALLENGE_START_DATE="2026-09-21", CHALLENGE_WEEKS=8)
    def test_forty_from_a_few_weeks_is_all_forty(self):
        with patch("django.utils.timezone.now", return_value=et("2026-11-20 12:00")):
            self._ship(1500, in_week(2), status=Ship.ShipStatus.FINALIZED)
            self._ship(1500, in_week(6), status=Ship.ShipStatus.FINALIZED)
            state = challenge.standing(self.user)
            self.assertEqual(state.shipped_printer_minutes, 2400)  # capped at the forty
            self.assertTrue(state.all_shipped)
            self.assertTrue(state.all_finalized)

    def test_logged_time_not_yet_shipped_is_counted_up(self):
        with during_week(4):
            self._ship(180, in_week(3))
            self._log(150, in_week(3))
            self._log(30, in_week(1))
            self.assertEqual(challenge.unshipped_minutes(self.user), 180)

    def test_time_that_can_never_be_shipped_is_not_waiting(self):
        """A rejected ship keeps its journals, a locked project can't ship,
        and prep time is in no week."""
        with during_week(4):
            self._ship(120, in_week(3), status=Ship.ShipStatus.REJECTED)
            locked = make_project(self.user, locked=True)
            self._log(60, in_week(3), project=locked)
            self._log(600, before_the_program())
            self.assertEqual(challenge.unshipped_seconds_by_week(self.user), {})

    def test_time_cut_in_lapse_review_does_not_wait_to_be_shipped(self):
        with during_week(4):
            self._log(120, in_week(3))
            journal = self.project.journals.get()
            approve_timelapse(journal, removals=[(journal.timelapses.get(), 0, 1800)])
            self.assertEqual(challenge.unshipped_minutes(self.user), 90)

    def test_the_deck_shows_the_panel(self):
        with during_week(4):
            self._ship(300, in_week(3))
            self._log(60, in_week(1))
            response = self.client.get(reverse("dashboard"))
            self.assertContains(response, "Shipped for your printer")
            self.assertContains(
                response, f"of {weeks.printer_hours()} hours from weeks 1–{weeks.week_count()}"
            )
            self.assertContains(response, "1h 0m logged during weeks 1–")
            self.assertNotContains(response, "By week")
            left = challenge.format_minutes(weeks.printer_hours() * 60 - 300)
            self.assertContains(response, f"{left} to go")
            self.assertContains(response, "Shipping closes")

    def test_there_is_no_panel_before_the_program(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Shipped for your printer")

    def test_all_shipped_points_at_finalizing(self):
        with after_week(1):
            ship = self._ship(300, in_week(1))
            response = self.client.get(reverse("dashboard"))
            self.assertTrue(response.context["standing"].all_shipped)
            self.assertNotContains(response, 'class="streak-left"')
            self.assertContains(response, "0h 0m finalized so far")

            Ship.objects.filter(pk=ship.pk).update(status=Ship.ShipStatus.FINALIZED)
            response = self.client.get(reverse("dashboard"))
            self.assertContains(response, "All of them are finalized, too.")

    def test_short_after_shipping_closes_says_so(self):
        with after_post_prep(1):
            self._ship(120, in_week(1))
            response = self.client.get(reverse("dashboard"))
            self.assertContains(response, "Shipping has closed.")
            self.assertNotContains(response, "Shipping closes")


class PageRenderTests(BaseTestCase):
    """Every page the challenge touches, rendered.

    A template error only shows up when something renders it, and several of
    these pages grew new blocks that nothing else in the suite visits — the
    printer charts in each of their states, the organizer's roster, and shop
    management with the kind field on its forms.
    """

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper", layers=1000)
        self.project = make_project(self.user)
        self.client.force_login(self.user)

    def _survive(self):
        ship = Ship.objects.create(project=self.project, status=Ship.ShipStatus.FINALIZED)
        journal = make_journal(self.project, ship=ship, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=300, recorded_at=in_week(1))

    def test_the_chart_room_renders_in_every_state(self):
        for label, window in (("before", during_week(1)), ("after", after_week(1))):
            with self.subTest(label), window:
                self._survive()
                self.assertEqual(self.client.get(reverse("printer_select")).status_code, 200)

    # The claim strip: what's shown is checked here, what's enforced against
    # the views themselves in PrinterClaimTests.

    def _chart(self):
        response = self.client.get(reverse("printer_track", args=["bambu"]))
        self.assertEqual(response.status_code, 200)
        return response

    def test_a_chart_has_no_claim_form_while_the_program_runs(self):
        with during_week(1):
            response = self._chart()
            self.assertNotContains(response, "Make this my chart")
            self.assertNotContains(response, 'class="star-buy"')

    def test_a_chart_has_the_claim_form_once_every_hour_is_finalized(self):
        with after_week(1):
            self._survive()
            response = self._chart()
            # One button under every star, priced and named for that printer.
            from ..printers import track
            printers = track("bambu")["printers"]
            self.assertContains(response, 'class="star-buy"', count=len(printers))
            self.assertContains(response, 'aria-label="Buy A1 for 230 pearls"')
            self.assertNotContains(response, "Make this my chart")

    def test_a_chart_holds_the_claim_while_ships_are_in_review(self):
        with after_week(1):
            self._survive()
            Ship.objects.update(status=Ship.ShipStatus.T3_QUEUE)
            response = self._chart()
            self.assertNotContains(response, 'class="star-buy"')

    def test_a_chart_renders_after_claiming(self):
        with after_week(1):
            self._survive()
            self.client.post(reverse("claim_printer", args=["bambu"]), {"printer": "A1"})
            self.assertTrue(PrinterClaim.objects.exists())
            response = self._chart()
            self.assertNotContains(response, 'class="star-buy"')

    def test_a_chart_renders_for_someone_who_is_out(self):
        with after_week(2):
            self._survive()
            response = self.client.get(reverse("printer_track", args=["bambu"]))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'class="star-buy"')

    def test_the_shop_and_an_item_page_render_with_savers_on_them(self):
        Item.objects.create(
            name="Streak saver", description="This week", cost=10,
            kind=Item.Kind.SAVER_CURRENT,
        )
        with during_week(1):
            self.assertEqual(self.client.get(reverse("shop")).status_code, 200)
            item = Item.objects.get(kind=Item.Kind.SAVER_CURRENT)
            response = self.client.get(reverse("item_detail", args=[item.id]))
            self.assertContains(response, "the moment you buy it")

    def test_a_printer_item_is_not_reachable_through_the_shop(self):
        printer = Item.objects.filter(kind=Item.Kind.PRINTER).first()
        self.assertEqual(
            self.client.get(reverse("item_detail", args=[printer.id])).status_code, 404
        )

    def test_the_organizer_pages_render(self):
        organizer = grant_perms(make_user("organizer", slack_id="U0ORG"), "organizer")
        self.client.force_login(organizer)
        with during_week(2):
            self._survive()
            self.assertEqual(self.client.get(reverse("challenge_dash")).status_code, 200)
            response = self.client.get(reverse("challenge_user", args=[self.user.id]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Grant saver hours")

    def test_shop_management_renders_with_the_kind_field(self):
        organizer = grant_perms(make_user("organizer", slack_id="U0ORG"), "organizer")
        self.client.force_login(organizer)
        response = self.client.get(reverse("shop_dash"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="kind"')
        self.assertContains(response, "hidden printer items")

    def test_the_reviewer_sees_the_payout_split(self):
        reviewer = grant_perms(make_user("t3", slack_id="U0T3"), "t3_review", "organizer")
        self.client.force_login(reviewer)
        with during_week(2):
            shippable = make_project(self.user, shippable=True)
            ship = make_ship(shippable, status=Ship.ShipStatus.T3_QUEUE, journal_minutes=())
            journal = make_journal(shippable, ship=ship, time_spent=0)
            make_timelapse(shippable, journal=journal, minutes=300, recorded_at=in_week(1))
            approve_timelapse(journal)

            response = self.client.get(reverse("fraud_review_project", args=[ship.id]))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "data-payout-buckets")
            self.assertEqual(
                [line.label for line in response.context["payout_lines"]], ["Week 1"]
            )


@override_settings(CHALLENGE_START_DATE="2026-09-21", CHALLENGE_WEEKS=8)
class FinalWeekExtensionTests(BaseTestCase):
    """Extra days on the last week for one person, granted by an organizer.

    Week 8 ends Tue Nov 17 11:59pm for everyone; three more days moves it to
    Fri Nov 20 for whoever has them, with the buffer to 2am Sat Nov 21.
    """

    def setUp(self):
        super().setUp()
        self.organizer = grant_perms(make_user("organizer", slack_id="U0ORG"), "organizer")
        self.user = make_user("shipper", slack_id="U0SHIP", layers=1000)
        self.project = make_project(self.user)

    def _extend(self, days):
        from ..models import FinalWeekExtension
        FinalWeekExtension.objects.update_or_create(
            user=self.user, defaults={"days": days, "granted_by": self.organizer},
        )

    def _ship(self, minutes, when, status=Ship.ShipStatus.FINALIZED):
        ship = Ship.objects.create(project=self.project, status=status)
        journal = make_journal(self.project, ship=ship, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=minutes, recorded_at=when)

    def _weeks_1_to_7(self):
        for index in range(1, 8):
            self._ship(300, in_week(index))

    def _at(self, text):
        return patch("django.utils.timezone.now", return_value=et(text))

    def test_the_week_math(self):
        self.assertEqual(weeks.week_bounds(8, 3)[1], et("2026-11-21 00:00"))
        self.assertEqual(weeks.deadline(8, 3), et("2026-11-20 23:59:59"))
        self.assertEqual(weeks.week_for(et("2026-11-19 12:00"), 3), 8)
        self.assertIsNone(weeks.week_for(et("2026-11-19 12:00")))
        # Their buffer is after their own end; everyone else's is just week 8.
        self.assertEqual(weeks.buffer_week(et("2026-11-21 01:00"), 3), 8)
        self.assertIsNone(weeks.buffer_week(et("2026-11-18 01:00"), 3))
        self.assertEqual(weeks.settled_at(3), et("2026-11-21 02:00"))
        self.assertEqual(weeks.week_label(8, 3), "Week 8 · Nov 9–20")
        # Ends a whole day before shipping closes at midnight going into Nov 30.
        self.assertEqual(weeks.max_extension_days(), 11)
        self.assertEqual(weeks.deadline(8, 11), et("2026-11-28 23:59:59"))

    @override_settings(CHALLENGE_WEEKS=9)
    def test_only_the_last_week_moves(self):
        self.assertEqual(weeks.week_bounds(8, 3)[1], et("2026-11-16 00:00"))
        self.assertEqual(weeks.week_bounds(9, 3)[1], et("2026-11-26 00:00"))

    def test_time_in_the_extension_counts_and_they_are_not_out(self):
        self._weeks_1_to_7()
        self._ship(120, in_week(8))
        self._ship(180, et("2026-11-19 15:00"))  # Thursday, inside their extension
        self._extend(3)
        with self._at("2026-11-19 16:00"):
            state = challenge.standing(self.user)
            self.assertEqual(state.current.index, 8)
            self.assertTrue(state.current.extended)
            self.assertFalse(state.eliminated)
            self.assertEqual(state.current.grace_minutes, 600)
            self.assertEqual(state.finalized_printer_minutes, 2400)

    def test_without_it_the_same_person_is_out(self):
        self._weeks_1_to_7()
        self._ship(120, in_week(8))
        self._ship(180, et("2026-11-19 15:00"))
        with self._at("2026-11-19 16:00"):
            state = challenge.standing(self.user)
            self.assertTrue(state.eliminated)
            self.assertEqual(state.finalized_printer_minutes, 2220)

    def test_it_can_reopen_a_week_that_already_closed_short(self):
        self._weeks_1_to_7()
        self._ship(120, in_week(8))
        with self._at("2026-11-18 12:00"):
            self.assertTrue(challenge.standing(self.user).eliminated)
            self._extend(3)
            state = challenge.standing(self.user)
            self.assertFalse(state.eliminated)
            self.assertEqual(state.current.index, 8)

    def test_printers_wait_for_their_own_settle(self):
        self._weeks_1_to_7()
        self._ship(300, in_week(8))
        self._extend(3)
        with self._at("2026-11-18 03:00"):
            self.assertFalse(challenge.standing(self.user).can_claim_printer)
        with self._at("2026-11-21 02:00"):
            self.assertTrue(challenge.standing(self.user).can_claim_printer)

    def test_the_whole_field_agrees_with_one_person(self):
        self._weeks_1_to_7()
        self._ship(120, in_week(8))
        self._ship(180, et("2026-11-19 15:00"))
        self._extend(3)
        with self._at("2026-11-19 16:00"):
            one = challenge.standing(self.user)
            bulk = challenge.standings_for([self.user.id])[self.user.id]
            self.assertEqual(bulk.eliminated, one.eliminated)
            self.assertEqual(bulk.current.index, 8)
            self.assertEqual(bulk.finalized_printer_minutes, one.finalized_printer_minutes)
            self.assertNotIn(self.user.id, challenge.rescued_by_buffer())

    def test_close_week_leaves_their_week_8_until_it_closes_for_them(self):
        self._weeks_1_to_7()
        self._ship(120, in_week(8))
        self._extend(3)
        def told(dm):
            # The organizer's own empty account is DMed too; only the shipper matters.
            return [c for c in dm.call_args_list if c.args[1] == "U0SHIP"]

        with self._at("2026-11-18 03:00"), patch("atlantis_site.views.helpers.send_slack_dm") as dm:
            call_command("close_week", stdout=StringIO())
            self.assertFalse(WeekOutcome.objects.filter(user=self.user, week_index=8).exists())
            self.assertEqual(told(dm), [])
        with self._at("2026-11-21 03:00"), patch("atlantis_site.views.helpers.send_slack_dm") as dm:
            call_command("close_week", stdout=StringIO())
            row = WeekOutcome.objects.get(user=self.user, week_index=8)
            self.assertFalse(row.passed)
            self.assertEqual(len(told(dm)), 1)

    def test_the_dashboard_counts_down_to_their_deadline(self):
        self._extend(3)
        self.client.force_login(self.user)
        with self._at("2026-11-19 16:00"):
            response = self.client.get(reverse("dashboard"))
            self.assertEqual(response.context["week_deadline"], et("2026-11-20 23:59:59").isoformat())

    def test_an_organizer_sets_changes_and_takes_it_away(self):
        from ..models import FinalWeekExtension
        self.client.force_login(self.organizer)
        url = reverse("extend_final_week", args=[self.user.id])
        with self._at("2026-11-17 12:00"):
            response = self.client.post(url, {"days": "3", "note": "flu"}, follow=True)
            extension = FinalWeekExtension.objects.get(user=self.user)
            self.assertEqual((extension.days, extension.note, extension.granted_by), (3, "flu", self.organizer))
            self.assertTrue(any("Fri Nov 20 at 11:59 PM" in m for m in message_texts(response)))
            self.assertTrue(AuditLog.objects.filter(action="extend_final_week").exists())

            page = self.client.get(reverse("challenge_user", args=[self.user.id]))
            self.assertContains(page, "+3 days")
            self.assertContains(page, "Fri Nov 20 at 11:59 PM")
            self.assertContains(self.client.get(reverse("challenge_dash")), "+3d")

            self.client.post(url, {"days": "5"})
            self.assertEqual(FinalWeekExtension.objects.get(user=self.user).days, 5)

            self.client.post(url, {"days": "0"})
            self.assertFalse(FinalWeekExtension.objects.exists())

    def test_shipping_in_the_extension_completes_the_program_then_the_printer(self):
        """Ship the extension's hours, pass once their week settles, buy once
        that ship is finalized."""
        self._weeks_1_to_7()
        self._ship(120, in_week(8))
        self._extend(3)
        with self._at("2026-11-19 15:00"):
            self.assertEqual(challenge.shipping_blocked_reason(self.user), "")
        self._ship(180, et("2026-11-19 15:00"), status=Ship.ShipStatus.T1_QUEUE)
        with self._at("2026-11-21 03:00"):
            state = challenge.standing(self.user)
            self.assertEqual(state.shipped_printer_minutes, 2400)
            self.assertTrue(state.passed)
            self.assertFalse(state.can_claim_printer)
        Ship.objects.filter(status=Ship.ShipStatus.T1_QUEUE).update(status=Ship.ShipStatus.FINALIZED)
        with self._at("2026-11-21 03:00"):
            self.assertTrue(challenge.standing(self.user).can_claim_printer)

    def test_they_are_dmed_when_their_deadline_moves(self):
        self.client.force_login(self.organizer)
        url = reverse("extend_final_week", args=[self.user.id])
        with self._at("2026-11-17 12:00"), \
                patch("atlantis_site.views.admin.challenge.send_slack_dm") as dm:
            self.client.post(url, {"days": "3", "note": "private reason"})
            dm.assert_called_once()
            text, slack_id = dm.call_args.args
            self.assertEqual(slack_id, "U0SHIP")
            self.assertIn("3 extra days on week 8", text)
            self.assertIn("Fri Nov 20 at 11:59 PM", text)
            self.assertIn("Shipping closes Sun Nov 29 at 11:59 PM", text)
            self.assertNotIn("private reason", text)

            # Same days, new note: nothing moved, so nothing to tell them.
            self.client.post(url, {"days": "3", "note": "flu, confirmed"})
            dm.assert_called_once()

            self.client.post(url, {"days": "0"})
            self.assertEqual(dm.call_count, 2)
            self.assertIn("removed", dm.call_args.args[0])
            self.assertIn("Tue Nov 17 at 11:59 PM", dm.call_args.args[0])

    def test_an_extension_past_the_cap_is_refused(self):
        from ..models import FinalWeekExtension
        self.client.force_login(self.organizer)
        with self._at("2026-11-17 12:00"):
            for days in ("12", "-1", "two"):
                with self.subTest(days=days):
                    self.client.post(reverse("extend_final_week", args=[self.user.id]), {"days": days})
                    self.assertFalse(FinalWeekExtension.objects.exists())

    def test_only_organizers_can_extend(self):
        from ..models import FinalWeekExtension
        self.client.force_login(grant_perms(make_user("t1", slack_id="U0T1"), "t1_review"))
        self.client.post(reverse("extend_final_week", args=[self.user.id]), {"days": "3"})
        self.assertFalse(FinalWeekExtension.objects.exists())
