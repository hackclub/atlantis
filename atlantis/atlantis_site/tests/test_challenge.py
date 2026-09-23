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

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .. import challenge, weeks
from ..models import (
    Item, Order, PrinterClaim, Profile, SaverCredit, Ship, WeekOutcome,
)
from ..printers import item_key
from .base import (
    BaseTestCase, after_week, approve_timelapse, before_the_program, during_week,
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

    def test_the_program_closes_at_local_midnight_after_the_last_sunday(self):
        # Eight weeks from Mon Sep 21 ends as Sun Nov 15 turns into Nov 16.
        self.assertEqual(weeks.ends_at(), et("2026-11-16 00:00"))

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

    def test_after_the_end_is_neither_a_week_nor_prep(self):
        moment = et("2026-11-16 00:00")
        self.assertIsNone(weeks.week_for(moment))
        self.assertFalse(weeks.is_prep(moment))

    def test_a_week_is_only_closed_once_it_is_over(self):
        self.assertEqual(weeks.closed_weeks(et("2026-09-27 23:59")), [])
        self.assertEqual(weeks.closed_weeks(et("2026-09-28 00:01")), [1])
        self.assertEqual(len(weeks.closed_weeks(et("2026-12-01 00:00"))), 8)

    def test_forty_printer_hours_is_exactly_every_week(self):
        self.assertEqual(weeks.printer_hours(), weeks.WEEKLY_HOURS * weeks.week_count())
        self.assertEqual(weeks.printer_hours(), 40)


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
        with during_week(1):
            self._log(300, in_week(1))
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
        with during_week(2):
            self._log(60, in_week(1))   # week 1 closed short
            self._log(300, in_week(2))  # and this week is full
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
            self.assertTrue(state.printer_unlocked)


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

    def test_the_first_five_hours_of_a_week_pay_the_base_rate(self):
        with during_week(1):
            journals = self._journals((300, in_week(1)))
            pearls, _lines, drawn = challenge.payout_breakdown(journals, 300)
            self.assertEqual(pearls, 5)  # five hours at 1
            self.assertEqual(drawn, {1: 300})

    def test_hours_past_the_weekly_five_pay_the_bonus_rate(self):
        with during_week(1):
            journals = self._journals((480, in_week(1)))
            pearls, _lines, _drawn = challenge.payout_breakdown(journals, 480)
            # 5h at 1 + 3h at 7
            self.assertEqual(pearls, 5 + 21)

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
            # 2h prep at 8, 2h inside week 1's allowance at 1
            self.assertEqual(pearls, 16 + 2)
            self.assertEqual(drawn, {1: 120})
            self.assertEqual([line.label for line in lines], ["Prep weeks", "Week 1"])

    def test_the_multiplier_scales_every_bracket(self):
        with during_week(1):
            journals = self._journals((480, in_week(1)))
            pearls, _lines, _drawn = challenge.payout_breakdown(
                journals, 480, multiplier=Decimal("2.0")
            )
            self.assertEqual(pearls, (5 + 21) * 2)

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
        with during_week(2):
            self._log(240, in_week(1))
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
        with during_week(2):
            self._log(240, in_week(1))
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

    def test_shipping_is_blocked_after_a_missed_week(self):
        with during_week(2):
            self._log(60, in_week(1))
            response = self.client.post(
                reverse("ship_project", args=[self.project.id]), ship_checklist(), follow=True
            )
            self.assertEqual(Ship.objects.count(), 0)
            self.assertTrue(any("out of the program" in m for m in message_texts(response)))

    def test_logging_new_time_is_blocked_too(self):
        with during_week(2):
            self._log(60, in_week(1))
            response = self.client.post(
                reverse("create_journal", args=[self.project.id]),
                {"title": "x", "lapse_timelapses": ["abc"]},
                follow=True,
            )
            self.assertTrue(any("out of the program" in m for m in message_texts(response)))

    def test_shipping_works_again_once_the_week_is_paid_for(self):
        with during_week(2):
            self._log(240, in_week(1))
            SaverCredit.objects.create(user=self.user, week_index=1)
            self.assertEqual(challenge.shipping_blocked_reason(self.user), "")

    def test_shipping_closes_when_the_program_ends(self):
        with after_week(1):
            self._log(300, in_week(1))
            reason = challenge.shipping_blocked_reason(self.user)
            self.assertIn("shipping is closed", reason)

    def test_someone_who_survived_can_still_log_time_after_the_end(self):
        """Shipping stops at the end; taping in what you already did does not."""
        with after_week(1):
            self._log(300, in_week(1))
            self.assertEqual(challenge.journaling_blocked_reason(self.user), "")

    def test_nothing_is_blocked_before_the_program_starts(self):
        self.assertEqual(challenge.shipping_blocked_reason(self.user), "")
        self.assertEqual(challenge.journaling_blocked_reason(self.user), "")


class PrinterClaimTests(BaseTestCase):
    """The one purchase at the end of the program."""

    def setUp(self):
        super().setUp()
        self.user = make_user("shipper", layers=1000)
        self.project = make_project(self.user)
        self.client.force_login(self.user)

    def _survive_the_program(self):
        journal = make_journal(self.project, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=300, recorded_at=in_week(1))

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

    def test_claiming_spends_the_whole_path(self):
        with after_week(1):
            self._survive_the_program()
            self._claim("A1")  # A1 Mini -> A1 is a 240-pearl step
            claim = PrinterClaim.objects.get()
            self.assertEqual(claim.printer_name, "A1")
            self.assertEqual(claim.pearls_spent, 240)
            self.assertEqual(Profile.objects.get(user=self.user).layers, 760)

    def test_a_claim_becomes_a_pending_order(self):
        with after_week(1):
            self._survive_the_program()
            self._claim("A1")
            order = Order.objects.get()
            self.assertEqual(order.status, Order.OrderStatus.PENDING)
            self.assertEqual(order.item.printer_key, item_key("bambu", "A1"))
            self.assertEqual(order.cost, 240)

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
            self.assertTrue(any("costs 240 pearls" in m for m in message_texts(response)))

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
        with during_week(2):
            self._log(60, in_week(1))
            response = self.client.get(reverse("challenge_dash"))
            rows = {row["user"].username: row for row in response.context["rows"]}
            self.assertTrue(rows["shipper"]["standing"].eliminated)

    def test_it_can_filter_to_just_the_dropped(self):
        survivor = make_user("survivor", slack_id="U0SURV")
        survivor_project = make_project(survivor)
        with during_week(2):
            self._log(60, in_week(1))
            journal = make_journal(survivor_project, time_spent=0)
            make_timelapse(survivor_project, journal=journal, minutes=300, recorded_at=in_week(1))

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

    def test_the_projects_page_says_why_you_cannot_ship(self):
        with during_week(2):
            self._log(60, in_week(1))
            response = self.client.get(reverse("projects"))
            self.assertContains(response, "out of the program")

    def test_before_the_start_it_says_so_instead_of_counting_down(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["week"], None)
        self.assertEqual(response.context["week_deadline"], "")


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
        journal = make_journal(self.project, time_spent=0)
        make_timelapse(self.project, journal=journal, minutes=300, recorded_at=in_week(1))

    def test_the_chart_room_renders_in_every_state(self):
        for label, window in (("before", during_week(1)), ("after", after_week(1))):
            with self.subTest(label), window:
                self._survive()
                self.assertEqual(self.client.get(reverse("printer_select")).status_code, 200)

    # The claim strip is off the front end for now, so a chart is the same map
    # in every state. These still render all of them: the view hands the
    # template a standing, a claim and the affordability sums whatever the week
    # is, and one of those going missing is a template error, not a wrong page.
    # What the strip used to drive is covered against the views themselves, in
    # PrinterClaimTests.

    def _chart_carries_no_claim_ui(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Make this my chart")
        self.assertNotContains(response, "Claim this printer")
        self.assertNotContains(response, "printer hours banked")

    def test_a_chart_renders_while_the_program_runs(self):
        with during_week(1):
            self._chart_carries_no_claim_ui(
                self.client.get(reverse("printer_track", args=["bambu"]))
            )

    def test_a_chart_renders_once_the_program_is_over(self):
        with after_week(1):
            self._survive()
            self._chart_carries_no_claim_ui(
                self.client.get(reverse("printer_track", args=["bambu"]))
            )

    def test_a_chart_renders_after_claiming(self):
        with after_week(1):
            self._survive()
            self.client.post(reverse("claim_printer", args=["bambu"]), {"printer": "A1"})
            self.assertTrue(PrinterClaim.objects.exists())
            self._chart_carries_no_claim_ui(
                self.client.get(reverse("printer_track", args=["bambu"]))
            )

    def test_a_chart_renders_for_someone_who_is_out(self):
        with after_week(2):
            self._survive()
            response = self.client.get(reverse("printer_track", args=["bambu"]))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "Claim this printer")

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
