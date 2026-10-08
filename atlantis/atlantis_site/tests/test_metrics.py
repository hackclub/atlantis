"""Presence tracking, and the activity/time figures the metrics page draws."""

import os
from datetime import datetime, time, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from .. import challenge, weeks
from ..models import ActiveDay, AirtableSubmission, Journal, Ship, T3, MetricsSnapshot, Profile, SaverCredit
from ..presence import WRITE_EVERY, record_seen
from ..views.admin.metrics import build_streak_stats
from .base import (
	BaseTestCase,
	approve_timelapse,
	grant_perms,
	make_journal,
	make_project,
	make_ship,
	make_timelapse,
	make_user,
)

DEFAULT_PFP_ENV = {"DEFAULT_PFP": "https://example.com/default.png"}


class PresenceTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		# The throttle lives in the cache, so a stale window from another test
		# would silently swallow the writes under test.
		cache.clear()

	def test_request_records_last_seen_and_the_day(self):
		user = make_user("builder")
		self.client.force_login(user)

		self.client.get(reverse("dashboard"))

		profile = Profile.objects.get(user=user)
		self.assertIsNotNone(profile.last_seen)
		self.assertEqual(
			list(ActiveDay.objects.filter(user=user).values_list("day", flat=True)),
			[timezone.localdate()],
		)

	def test_anonymous_requests_record_nothing(self):
		self.client.get(reverse("index"))

		self.assertFalse(ActiveDay.objects.exists())
		self.assertFalse(Profile.objects.filter(last_seen__isnull=False).exists())

	def test_second_request_inside_the_window_does_not_write_again(self):
		user = make_user("builder")

		self.assertTrue(record_seen(user))
		first = Profile.objects.get(user=user).last_seen

		self.assertFalse(record_seen(user))
		self.assertEqual(Profile.objects.get(user=user).last_seen, first)

	def test_the_window_expiring_writes_again(self):
		user = make_user("builder")
		record_seen(user)

		cache.clear()
		later = timezone.now() + timedelta(seconds=WRITE_EVERY + 1)
		with patch("atlantis_site.presence.timezone.now", return_value=later):
			self.assertTrue(record_seen(user))

		self.assertEqual(Profile.objects.get(user=user).last_seen, later)

	def test_a_second_day_adds_a_row_and_a_repeat_day_does_not(self):
		user = make_user("builder")
		today = timezone.localdate()
		yesterday = today - timedelta(days=1)

		ActiveDay.objects.create(user=user, day=yesterday)
		record_seen(user)
		cache.clear()
		record_seen(user)

		self.assertEqual(
			sorted(ActiveDay.objects.filter(user=user).values_list("day", flat=True)),
			[yesterday, today],
		)


@patch.dict(os.environ, DEFAULT_PFP_ENV)
class MetricsActivityTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		cache.clear()
		self.organizer = grant_perms(make_user("organizer", slack_id="U0ORG"), "organizer")
		self.client.force_login(self.organizer)

	def _page(self):
		"""Load the metrics page once.

		Once, because the presence middleware records this request too: the
		numbers are computed before it runs, so a second load would count the
		organizer as active today and move the figures under the assertions.
		"""
		response = self.client.get(reverse("metrics"))
		self.assertEqual(response.status_code, 200)
		return response.context

	def test_counts_who_is_here(self):
		now = timezone.now()
		today = timezone.localdate(now)
		here_now = make_user("here-now", slack_id="U1")
		here_today = make_user("here-today", slack_id="U2")
		here_earlier = make_user("here-earlier", slack_id="U3")

		Profile.objects.filter(user=here_now).update(last_seen=now - timedelta(minutes=1))
		Profile.objects.filter(user=here_today).update(last_seen=now - timedelta(hours=3))
		Profile.objects.filter(user=here_earlier).update(last_seen=now - timedelta(days=9))
		ActiveDay.objects.create(user=here_now, day=today)
		ActiveDay.objects.create(user=here_today, day=today)
		ActiveDay.objects.create(user=here_earlier, day=today - timedelta(days=9))
		# Outside the 30-day window entirely.
		ActiveDay.objects.create(user=here_earlier, day=today - timedelta(days=40))

		activity = self._page()["activity"]

		self.assertEqual(activity["active_now"], 1)
		self.assertEqual(activity["active_today"], 2)
		self.assertEqual(activity["active_in_window"], 3)
		self.assertEqual(activity["total_users"], 4)
		# Three user-days inside the window, over the whole thirty: presence
		# has been on record since well before it started.
		self.assertEqual(activity["dau_days"], 30)
		self.assertEqual(activity["avg_dau"], 0.1)

	def test_the_dau_average_only_divides_by_the_days_on_record(self):
		user = make_user("builder", slack_id="U9")
		today = timezone.localdate()
		# Nothing older: tracking, as far as this site knows, started four days
		# ago, and dividing by thirty would read as a collapse in traffic.
		for offset in range(4):
			ActiveDay.objects.create(user=user, day=today - timedelta(days=offset))

		activity = self._page()["activity"]

		self.assertEqual(activity["dau_days"], 4)
		self.assertEqual(activity["avg_dau"], 1.0)

	def test_signups_today_and_the_daily_average(self):
		now = timezone.now()
		for offset in (0, 1, 5, 40):
			user = make_user(f"joiner-{offset}", slack_id=f"US{offset}")
			user.date_joined = now - timedelta(days=offset)
			user.save(update_fields=["date_joined"])

		activity = self._page()["activity"]

		# The organizer joined today too.
		self.assertEqual(activity["signups_today"], 2)
		self.assertEqual(activity["signups_last_7"], 4)
		self.assertEqual(activity["signups_last_30"], 4)
		self.assertEqual(activity["avg_signups_window"], round(4 / 30, 1))
		self.assertEqual(activity["signup_days"], 41)
		self.assertEqual(activity["avg_signups_all_time"], round(5 / 41, 1))
		self.assertEqual(len(activity["daily_signups"]), 14)
		self.assertEqual(activity["daily_signups"][-1]["value"], 2)

	def test_daily_charts_fill_in_the_quiet_days(self):
		user = make_user("builder", slack_id="U9")
		today = timezone.localdate()
		ActiveDay.objects.create(user=user, day=today)
		ActiveDay.objects.create(user=user, day=today - timedelta(days=2))

		rows = self._page()["activity"]["daily_active"]

		self.assertEqual(len(rows), 14)
		self.assertEqual([row["value"] for row in rows[-3:]], [1, 0, 1])


@patch.dict(os.environ, DEFAULT_PFP_ENV)
class MetricsHoursTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		cache.clear()
		self.organizer = grant_perms(make_user("organizer", slack_id="U0ORG"), "organizer")
		self.client.force_login(self.organizer)

	def _hours(self):
		response = self.client.get(reverse("metrics"))
		self.assertEqual(response.status_code, 200)
		return response.context["hours"]

	def test_hours_logged_pending_and_approved(self):
		owner = make_user("builder", slack_id="U1")
		project = make_project(owner)
		reviewed = make_journal(project, time_spent=120)
		approve_timelapse(
			reviewed,
			removals=[(reviewed.timelapses.get(), 0, 30 * 60)],
		)
		make_journal(project, time_spent=60)

		hours = self._hours()

		self.assertEqual(hours["today"], 3.0)
		self.assertEqual(hours["devlogs_today"], 2)
		# Only the lapse nobody has signed off is waiting on a reviewer.
		self.assertEqual(hours["pending"], 1.0)
		self.assertEqual(hours["pending_devlogs"], 1)
		self.assertEqual(hours["window"], 3.0)
		# Two of the reviewed lapse's hours survived the half-hour cut.
		self.assertEqual(hours["approved_window"], 1.5)

	def test_averages_are_per_day_per_builder_and_per_lapse(self):
		for name in ("one", "two"):
			project = make_project(make_user(f"builder-{name}", slack_id=f"U{name}"))
			make_journal(project, time_spent=180)

		hours = self._hours()

		self.assertEqual(hours["window"], 6.0)
		self.assertEqual(hours["builders_window"], 2)
		self.assertEqual(hours["avg_per_builder"], 3.0)
		self.assertEqual(hours["avg_per_day"], 0.2)
		self.assertEqual(hours["avg_per_devlog_display"], "3h 0m")
		self.assertEqual(hours["daily_hours"][-1]["value"], 6.0)

	def test_builders_per_week_counts_each_builder_once_per_calendar_week(self):
		busy = make_project(make_user("busy", slack_id="U-busy"))
		quit = make_project(make_user("quit", slack_id="U-quit"))
		make_journal(busy, time_spent=60)
		make_journal(busy, time_spent=60)
		last_week = timezone.now() - timedelta(weeks=1)
		for project in (busy, quit):
			stale = make_journal(project, time_spent=60)
			Journal.objects.filter(pk=stale.pk).update(created_at=last_week)

		rows = self._hours()["weekly_builders"]

		self.assertEqual(len(rows), 8)
		self.assertEqual([row["value"] for row in rows[-2:]], [2, 1])
		self.assertEqual(rows[-1]["sub"], "so far")
		self.assertNotIn("sub", rows[-2])

	def test_seven_day_average_ignores_older_lapses(self):
		project = make_project(make_user("builder", slack_id="U1"))
		make_journal(project, time_spent=420)
		stale = make_journal(project, time_spent=600)
		Journal.objects.filter(pk=stale.pk).update(
			created_at=timezone.now() - timedelta(days=10)
		)

		hours = self._hours()

		# The ten-day-old lapse is inside the 30-day window but outside the
		# short one, so only the recent seven hours divide by seven.
		self.assertEqual(hours["last_7"], 7.0)
		self.assertEqual(hours["devlogs_last_7"], 1)
		self.assertEqual(hours["avg_per_day_7"], 1.0)
		self.assertEqual(hours["window"], 17.0)

	def test_this_week_counts_from_monday_midnight_local(self):
		project = make_project(make_user("builder", slack_id="U1"))
		make_journal(project, time_spent=120)
		last_week = make_journal(project, time_spent=300)
		with timezone.override(settings.CHALLENGE_TIMEZONE):
			today = timezone.localdate()
			monday = datetime.combine(
				today - timedelta(days=today.weekday()), time.min,
				tzinfo=timezone.get_current_timezone(),
			)
		# A minute before the week opened: last week's, however recent.
		Journal.objects.filter(pk=last_week.pk).update(created_at=monday - timedelta(minutes=1))

		hours = self._hours()

		self.assertEqual(hours["this_week"], 2.0)
		self.assertEqual(hours["devlogs_this_week"], 1)
		self.assertEqual(hours["builders_this_week"], 1)
		self.assertEqual(hours["week_start"], monday.date())

	def test_shipped_hours_count_every_shipped_lapse_and_finalized_apart(self):
		project = make_project(make_user("builder", slack_id="U1"))
		make_ship(project, status=Ship.ShipStatus.FINALIZED, journal_minutes=(120,))
		make_ship(project, status=Ship.ShipStatus.REJECTED, journal_minutes=(60,))
		make_ship(project, status=Ship.ShipStatus.T2_QUEUE, journal_minutes=(30, 30))
		# Logged but never shipped: not shipped hours.
		make_journal(project, time_spent=600)

		response = self.client.get(reverse("metrics"))
		ships = response.context["ships"]

		self.assertEqual(ships["shipped_hours"], 4.0)
		self.assertEqual(ships["shipped_devlogs"], 4)
		self.assertEqual(ships["finalized_hours"], 2.0)

	def test_airtable_hours_are_what_each_created_record_was_sent(self):
		project = make_project(make_user("builder", slack_id="U1"))
		reviewer = make_user("t3", slack_id="U-t3")

		def finalized(*t3s, record_id="rec1"):
			ship = make_ship(project, status=Ship.ShipStatus.FINALIZED, journal_minutes=(60,))
			for decision, minutes in t3s:
				T3.objects.create(
					ship=ship, reviewer=reviewer, decision=decision,
					payout_time=minutes, airtable_time=minutes,
				)
			AirtableSubmission.objects.create(ship=ship, record_id=record_id)

		# A later return doesn't displace the approval Airtable was sent.
		finalized((T3.Decision.APPROVE, 120), (T3.Decision.RETURN_T2, 600))
		finalized((T3.Decision.APPROVE, 30), (T3.Decision.APPROVE, 60), record_id="rec2")
		# Claimed but never created: nothing reached Airtable.
		finalized((T3.Decision.APPROVE, 900), record_id="")

		ships = self.client.get(reverse("metrics")).context["ships"]

		self.assertEqual(ships["airtable_hours"], 3.0)
		self.assertEqual(ships["airtable_ships"], 2)
		self.assertEqual(ships["airtable_unsent"], 1)

	def test_funnel_counts_each_step_out_of_the_one_before(self):
		make_user("signed-up-only", slack_id="U1")
		make_project(make_user("made-project", slack_id="U2"))
		# Two projects with time between them still count once.
		tracker = make_user("tracked", slack_id="U3")
		make_journal(make_project(tracker), time_spent=30)
		make_journal(make_project(tracker), time_spent=30)
		make_ship(make_project(make_user("shipped", slack_id="U4")), journal_minutes=(60,))
		# A ship with no tracked time behind it skips a step, so it stops there.
		skipper = make_user("skipper", slack_id="U5")
		Ship.objects.create(project=make_project(skipper))

		# Everyone signed up, the organizer and the base fixtures included.
		signups = get_user_model().objects.count()

		funnel = self.client.get(reverse("metrics")).context["funnel"]

		self.assertEqual(
			[(row["label"], row["value"]) for row in funnel["steps"]],
			[("Signed up", signups), ("Made a project", 4), ("Tracked some time", 2), ("Shipped a project", 1)],
		)
		self.assertEqual(funnel["steps"][3]["sub"].split(" · ")[1], "50.0% of previous")
		self.assertEqual(funnel["overall_rate"], round(100 / signups, 1))

	def test_a_site_with_nothing_on_it_does_not_divide_by_zero(self):
		hours = self._hours()

		self.assertEqual(hours["today"], 0)
		self.assertEqual(hours["avg_per_builder"], 0.0)
		self.assertEqual(hours["avg_per_devlog_display"], "0h 0m")
		self.assertEqual(hours["avg_per_day_7"], 0.0)

	def test_challenge_hours_only_count_the_program_weeks(self):
		project = make_project(make_user("builder", slack_id="U1"))
		inside = make_journal(project, time_spent=120)
		before = make_journal(project, time_spent=300)
		after = make_journal(project, time_spent=600)
		Journal.objects.filter(pk=inside.pk).update(created_at=weeks.starts_at())
		Journal.objects.filter(pk=before.pk).update(created_at=weeks.starts_at() - timedelta(minutes=1))
		# The end is exclusive: midnight after the last Sunday is already past it.
		Journal.objects.filter(pk=after.pk).update(created_at=weeks.ends_at())

		hours = self._hours()

		self.assertEqual(hours["challenge"], 2.0)
		self.assertEqual(hours["devlogs_challenge"], 1)
		self.assertEqual(hours["builders_challenge"], 1)
		self.assertEqual(hours["challenge_start"], weeks.start_date())
		self.assertEqual(
			hours["challenge_end"],
			weeks.start_date() + timedelta(weeks=weeks.week_count(), days=-1),
		)


@patch.dict(os.environ, DEFAULT_PFP_ENV)
class MetricsSnapshotTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		cache.clear()
		self.organizer = grant_perms(make_user("organizer", slack_id="U0ORG"), "organizer")
		self.client.force_login(self.organizer)

	def _run(self, at, *args):
		out = StringIO()
		with patch("atlantis_site.management.commands.snapshot_metrics.timezone.now", return_value=at):
			call_command("snapshot_metrics", *args, stdout=out)
		return out.getvalue()

	def test_a_run_at_2359_eastern_files_the_page_under_that_day(self):
		make_journal(make_project(make_user("builder", slack_id="U1")), time_spent=120)
		# 03:59 UTC on the 24th: still the 23rd in New York (EDT).
		at = datetime(2026, 9, 23, 23, 59, tzinfo=ZoneInfo("America/New_York"))

		self._run(at)

		snapshot = MetricsSnapshot.objects.get()
		self.assertEqual(snapshot.day, datetime(2026, 9, 23).date())
		self.assertEqual(snapshot.taken_at, at)
		self.assertEqual(snapshot.data["hours"]["all_time"], 2.0)

	def test_a_run_at_any_other_minute_does_nothing(self):
		# 23:59 UTC, and a run held up just past local midnight.
		self._run(datetime(2026, 9, 23, 23, 59, tzinfo=ZoneInfo("UTC")))
		self._run(datetime(2026, 9, 24, 0, 0, 30, tzinfo=ZoneInfo("America/New_York")))
		self.assertFalse(MetricsSnapshot.objects.exists())

	def test_force_ignores_the_clock_and_a_rerun_replaces_the_day(self):
		at = datetime(2026, 9, 23, 12, 0, tzinfo=ZoneInfo("America/New_York"))
		self._run(at, "--force")
		make_journal(make_project(make_user("builder", slack_id="U1")), time_spent=60)
		self._run(at + timedelta(hours=1), "--force")

		snapshot = MetricsSnapshot.objects.get()
		self.assertEqual(snapshot.data["hours"]["all_time"], 1.0)

	def test_the_page_shows_a_past_day_with_its_dates_intact(self):
		make_journal(make_project(make_user("builder", slack_id="U1")), time_spent=180)
		at = datetime(2026, 9, 23, 23, 59, tzinfo=ZoneInfo("America/New_York"))
		self._run(at)
		# Live has moved on since the snapshot.
		make_journal(make_project(make_user("builder2", slack_id="U2")), time_spent=600)

		response = self.client.get(reverse("metrics"), {"day": "2026-09-23"})

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["hours"]["all_time"], 3.0)
		self.assertEqual(response.context["hours"]["challenge_start"], weeks.start_date())
		self.assertEqual(response.context["generated_at"], at)
		self.assertContains(response, "Back to live")

		live = self.client.get(reverse("metrics"))
		self.assertEqual(live.context["hours"]["all_time"], 13.0)
		self.assertEqual(live.context["snapshot_days"], [datetime(2026, 9, 23).date()])

	def test_a_day_with_no_snapshot_or_a_bad_date_is_a_404(self):
		self.assertEqual(self.client.get(reverse("metrics"), {"day": "2026-01-01"}).status_code, 404)
		self.assertEqual(self.client.get(reverse("metrics"), {"day": "nonsense"}).status_code, 404)
		self.assertEqual(self.client.get(reverse("metrics"), {"day": "2026-02-31"}).status_code, 404)


# A fixed calendar, so how far through the week "now" is — and so who is on
# pace — doesn't depend on the day the suite runs.
FIXED_CHALLENGE = {"CHALLENGE_START_DATE": "2026-09-21", "CHALLENGE_WEEKS": 8}
EASTERN = ZoneInfo("America/New_York")


@override_settings(**FIXED_CHALLENGE, CHALLENGE_TIMEZONE="America/New_York")
class MetricsStreakTests(BaseTestCase):
	def _builder(self, name, *minutes_at):
		user = make_user(name, slack_id=f"U-{name}")
		project = make_project(user)
		for minutes, when in minutes_at:
			journal = make_journal(project, time_spent=0)
			make_timelapse(project, journal=journal, minutes=minutes, recorded_at=when)
		return user

	def test_the_live_week_splits_into_done_on_pace_and_behind(self):
		# Thursday noon: half the week gone, so on pace is 2h 30m so far.
		now = datetime(2026, 9, 24, 12, tzinfo=EASTERN)
		monday = datetime(2026, 9, 22, 10, tzinfo=EASTERN)
		self._builder("done", (300, monday))
		self._builder("on-pace", (200, monday))
		self._builder("behind", (60, monday))
		saved = self._builder("saved", (100, monday))
		SaverCredit.objects.bulk_create([SaverCredit(user=saved, week_index=1) for _ in range(1)])
		# Prep time only: a participant, but nothing on this week.
		self._builder("prepper", (500, datetime(2026, 9, 15, 10, tzinfo=EASTERN)))
		# Signed up and never logged anything: not a participant at all.
		make_user("lurker", slack_id="U-lurker")

		stats = build_streak_stats(now)

		self.assertEqual(stats["live_week"], 1)
		self.assertEqual(stats["elapsed_pct"], 50)
		self.assertEqual(stats["participants"], 5)
		self.assertEqual(stats["still_in"], 5)
		self.assertEqual(stats["done"], 1)
		self.assertEqual(stats["short"], 4)
		# on-pace (200m); saved is 100m + 1h saver = 160m, also ahead of 150m.
		self.assertEqual(stats["on_pace"], 2)
		self.assertEqual(stats["behind_pace"], 2)
		self.assertEqual(stats["nothing_yet"], 1)
		self.assertEqual(stats["saver_hours_this_week"], 1)
		self.assertEqual(
			[row["value"] for row in stats["this_week"]],
			[1, 0, 1, 1, 1, 0, 1],
		)

	def test_closed_weeks_count_who_is_out_and_who_was_rescued(self):
		# Wednesday of week 3: weeks 1 and 2, judged as a pair, have closed.
		now = datetime(2026, 10, 7, 12, tzinfo=EASTERN)
		week_1 = datetime(2026, 9, 23, 10, tzinfo=EASTERN)
		week_2 = datetime(2026, 9, 30, 10, tzinfo=EASTERN)
		self._builder("survivor", (300, week_1), (300, week_2))
		self._builder("dropped", (60, week_1))
		rescued = self._builder("rescued", (120, week_1), (300, week_2))
		SaverCredit.objects.bulk_create([SaverCredit(user=rescued, week_index=1) for _ in range(3)])

		stats = build_streak_stats(now)

		self.assertEqual(stats["live_week"], 3)
		self.assertEqual(stats["participants"], 3)
		self.assertEqual(stats["out"], 1)
		self.assertEqual(stats["still_in"], 2)
		self.assertEqual(stats["weeks_rescued"], 1)
		self.assertEqual(stats["survived_by_week"][0]["value"], 2)
		self.assertEqual(stats["survived_by_week"][0]["sub"], "1 missed")
		# Whoever is out isn't counted against this week.
		self.assertEqual(stats["short"], 2)

	def test_completed_by_week_counts_logged_hours_and_averages_them(self):
		# Wednesday of week 2: week 1 has closed, week 2 is live.
		now = datetime(2026, 10, 1, 12, tzinfo=EASTERN)
		week_1 = datetime(2026, 9, 23, 10, tzinfo=EASTERN)
		week_2 = datetime(2026, 9, 30, 10, tzinfo=EASTERN)
		self._builder("steady", (300, week_1), (360, week_2))
		self._builder("keen", (420, week_1))
		self._builder("short", (200, week_1))
		# Five hours credited, but three of them bought: not completed.
		saved = self._builder("saved", (120, week_1))
		SaverCredit.objects.bulk_create([SaverCredit(user=saved, week_index=1) for _ in range(3)])

		stats = build_streak_stats(now)

		rows = stats["completed_by_week"]
		self.assertEqual([row["label"] for row in rows], ["Week 1", "Week 2"])
		self.assertEqual([row["value"] for row in rows], [2, 1])
		# (300 + 420) / 2 = 360m.
		self.assertEqual(rows[0]["sub"], "avg 6h 0m")
		self.assertEqual(rows[1]["sub"], "avg 6h 0m so far")
		self.assertEqual(stats["completed_this_week"], 1)
		self.assertEqual(stats["avg_completed_display"], "6h 0m")

	def test_the_grace_pair_counts_ten_between_the_weeks_until_it_closes(self):
		# Wednesday of week 2.
		now = datetime(2026, 9, 30, 12, tzinfo=EASTERN)
		week_1 = datetime(2026, 9, 23, 10, tzinfo=EASTERN)
		week_2 = datetime(2026, 9, 29, 10, tzinfo=EASTERN)
		self._builder("split", (420, week_1), (180, week_2))
		self._builder("front-loaded", (600, week_1))
		# Five this week counts as done, but week 1 is still short.
		self._builder("late", (300, week_2))
		self._builder("short", (240, week_1), (300, week_2))

		stats = build_streak_stats(now)

		self.assertEqual(stats["done"], 4)
		self.assertTrue(stats["grace_live"])
		self.assertEqual(stats["grace_label"], "1 & 2")
		self.assertEqual(stats["grace_hours"], 10)
		self.assertEqual(stats["grace_secured"], 2)
		self.assertEqual(stats["grace_secured_rate"], 50.0)

		# Week 3: the pair has closed and the card goes.
		stats = build_streak_stats(datetime(2026, 10, 7, 12, tzinfo=EASTERN))
		self.assertNotIn("grace_live", stats)

	def test_the_bulk_standings_agree_with_one_at_a_time(self):
		now = datetime(2026, 9, 30, 12, tzinfo=EASTERN)
		users = [
			self._builder("a", (300, datetime(2026, 9, 23, 10, tzinfo=EASTERN))),
			self._builder("b", (60, datetime(2026, 9, 23, 10, tzinfo=EASTERN)), (90, now)),
		]
		SaverCredit.objects.create(user=users[1], week_index=1)

		bulk = challenge.standings(now)

		for user in users:
			self.assertEqual(bulk[user.id], challenge.standing(user, now))

	def test_before_the_weeks_open_there_is_no_live_week(self):
		stats = build_streak_stats(datetime(2026, 9, 18, 12, tzinfo=EASTERN))

		self.assertFalse(stats["started"])
		self.assertIsNone(stats["live_week"])
		self.assertNotIn("done", stats)
