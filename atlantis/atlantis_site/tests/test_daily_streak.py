"""The daily review streak: DAILY_GOAL reviews a day, in the reviewer's own zone."""

from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.urls import reverse

from ..daily_streak import DAILY_GOAL, daily_streak, streak_from_counts
from ..models import T1, T2, T3, Profile, ReviewStreakReminder
from .base import BaseTestCase, grant_perms, make_project, make_ship, make_user, t1_checklist
from .test_review_streak import celebration

LA = "America/Los_Angeles"


def at(zone, when):
	"""An aware datetime for a wall-clock time in `zone`."""
	return datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(zone))


class StreakFromCountsTests(BaseTestCase):
	def test_today_unfinished_keeps_yesterdays_run(self):
		today = date(2026, 11, 20)
		counts = {
			today: 3,
			today - timedelta(days=1): DAILY_GOAL,
			today - timedelta(days=2): DAILY_GOAL + 4,
		}
		self.assertEqual(streak_from_counts(counts, today), 2)

	def test_today_done_counts(self):
		today = date(2026, 11, 20)
		counts = {today: DAILY_GOAL, today - timedelta(days=1): DAILY_GOAL}
		self.assertEqual(streak_from_counts(counts, today), 2)

	def test_a_short_day_ends_the_run(self):
		today = date(2026, 11, 20)
		counts = {
			today: DAILY_GOAL,
			today - timedelta(days=1): DAILY_GOAL - 1,
			today - timedelta(days=2): DAILY_GOAL,
		}
		self.assertEqual(streak_from_counts(counts, today), 1)


class DailyStreakTestCase(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("rev", slack_id="U0REV"), "t1_review")
		Profile.objects.filter(user=self.reviewer).update(timezone=LA)
		self.reviewer.refresh_from_db()
		self.ship = make_ship(make_project(make_user("author"), shippable=True))

	def _review(self, when, n=1, model=T1):
		"""`n` decisions by the reviewer, all stamped `when`."""
		for _ in range(n):
			if model is T1:
				row = T1.objects.create(
					ship=self.ship, reviewer=self.reviewer, feedback="", internal_notes="", approved=True,
				)
			elif model is T2:
				row = T2.objects.create(
					ship=self.ship, reviewer=self.reviewer, feedback="", justification="",
				)
			else:
				row = T3.objects.create(
					ship=self.ship, reviewer=self.reviewer, payout_time=60, airtable_time=60,
				)
			# reviewed_at is auto_now_add, so it's set after the fact.
			model.objects.filter(pk=row.pk).update(reviewed_at=when)


class DailyStreakTests(DailyStreakTestCase):
	def test_every_tier_counts(self):
		now = at(LA, "2026-11-20 12:00")
		self._review(now, n=DAILY_GOAL - 2)
		self._review(now, model=T2)
		self._review(now, model=T3)
		streak = daily_streak(self.reviewer, now)
		self.assertEqual(streak.today, DAILY_GOAL)
		self.assertTrue(streak.done_today)
		self.assertEqual(streak.days, 1)

	def test_days_are_cut_at_the_reviewers_midnight(self):
		# 10pm in Los Angeles is already tomorrow in New York.
		self._review(at(LA, "2026-11-19 22:00"), n=DAILY_GOAL)
		streak = daily_streak(self.reviewer, at(LA, "2026-11-19 23:30"))
		self.assertEqual(streak.date, date(2026, 11, 19))
		self.assertTrue(streak.done_today)

		Profile.objects.filter(user=self.reviewer).update(timezone="America/New_York")
		self.reviewer.refresh_from_db()
		eastern = daily_streak(self.reviewer, at(LA, "2026-11-19 23:30"))
		self.assertEqual(eastern.date, date(2026, 11, 20))
		self.assertTrue(eastern.done_today)

	def test_an_undone_review_stops_counting(self):
		now = at(LA, "2026-11-20 12:00")
		self._review(now, n=DAILY_GOAL)
		T1.objects.filter(reviewer=self.reviewer).first().delete()
		self.assertFalse(daily_streak(self.reviewer, now).done_today)

	def test_unknown_zone_reads_on_the_program_clock(self):
		Profile.objects.filter(user=self.reviewer).update(timezone="")
		self.reviewer.refresh_from_db()
		self.assertEqual(daily_streak(self.reviewer).zone_name, "America/New_York")


class RemindReviewStreakTests(DailyStreakTestCase):
	def setUp(self):
		super().setUp()
		patcher = patch("atlantis_site.views.helpers.send_slack_dm", return_value=True)
		self.dm = patcher.start()
		self.addCleanup(patcher.stop)

	def _run(self, when):
		with patch("django.utils.timezone.now", return_value=when):
			call_command("remind_review_streak", stdout=StringIO())

	def _streak_with_today(self, today_count):
		"""Two full days behind today, and `today_count` reviews so far today."""
		self._review(at(LA, "2026-11-18 10:00"), n=DAILY_GOAL)
		self._review(at(LA, "2026-11-19 10:00"), n=DAILY_GOAL)
		if today_count:
			self._review(at(LA, "2026-11-20 10:00"), n=today_count)

	def test_dms_once_at_eleven_local(self):
		self._streak_with_today(3)
		self._run(at(LA, "2026-11-20 23:05"))
		self.dm.assert_called_once()
		text, slack_id = self.dm.call_args.args
		self.assertEqual(slack_id, "U0REV")
		self.assertIn("2-day review streak", text)
		self.assertIn("4 more reviews", text)
		self.assertTrue(
			ReviewStreakReminder.objects.filter(user=self.reviewer, day=date(2026, 11, 20)).exists()
		)

		self._run(at(LA, "2026-11-20 23:15"))
		self.dm.assert_called_once()

	def test_nothing_before_eleven(self):
		self._streak_with_today(3)
		# 11:59pm in New York, but only 8:59pm for this reviewer.
		self._run(at(LA, "2026-11-20 20:59"))
		self.dm.assert_not_called()

	def test_nothing_once_today_is_done(self):
		self._streak_with_today(DAILY_GOAL)
		self._run(at(LA, "2026-11-20 23:05"))
		self.dm.assert_not_called()

	def test_nothing_without_a_run_to_save(self):
		self._review(at(LA, "2026-11-20 10:00"), n=3)
		self._run(at(LA, "2026-11-20 23:05"))
		self.dm.assert_not_called()

	def test_banned_reviewers_are_left_alone(self):
		self._streak_with_today(3)
		Profile.objects.filter(user=self.reviewer).update(banned=True)
		self._run(at(LA, "2026-11-20 23:05"))
		self.dm.assert_not_called()

	def test_zone_is_backfilled_from_slack(self):
		Profile.objects.filter(user=self.reviewer).update(timezone="")
		tokyo = "Asia/Tokyo"
		self._review(at(tokyo, "2026-11-19 10:00"), n=DAILY_GOAL)
		with patch(
			"atlantis_site.views.helpers.slack_client.users_info",
			return_value={"user": {"tz": tokyo}},
		):
			self._run(at(tokyo, "2026-11-20 23:05"))
		self.assertEqual(Profile.objects.get(user=self.reviewer).timezone, tokyo)
		self.dm.assert_called_once()


class DailyStreakPageTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t1rev"), "t1_review")
		self.client.force_login(self.reviewer)
		project = make_project(make_user("author"), shippable=True)
		self.ships = [make_ship(project) for _ in range(2)]

	def _decide(self, ship):
		data = {"feedback": "nice", "internal_notes": "", "approved": "denied"}
		return self.client.post(reverse("t1_decision", args=[ship.id]), data, follow=True)

	def test_meeting_the_goal_is_celebrated(self):
		for _ in range(DAILY_GOAL - 2):
			T1.objects.create(
				ship=self.ships[0], reviewer=self.reviewer, feedback="", internal_notes="", approved=False,
			)
		self.assertNotIn("dailyStreak", celebration(self._decide(self.ships[0])))
		payload = celebration(self._decide(self.ships[1]))
		self.assertEqual(payload["dailyStreak"], 1)

	def test_home_shows_progress(self):
		T1.objects.create(
			ship=self.ships[0], reviewer=self.reviewer, feedback="", internal_notes="", approved=True,
		)
		response = self.client.get(reverse("admin_dash"))
		self.assertContains(response, 'data-daily-streak="0"')
		self.assertContains(response, f"1/{DAILY_GOAL}")
		self.assertContains(response, "start a daily review streak")

	def test_home_hides_it_from_non_reviewers(self):
		user = grant_perms(make_user("fulfiller"), "fulfillment")
		self.client.force_login(user)
		self.assertNotContains(self.client.get(reverse("admin_dash")), "data-daily-streak")
