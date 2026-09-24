"""Presence tracking, and the activity/time figures the metrics page draws."""

import os
from datetime import datetime, time, timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from ..models import ActiveDay, Journal, Profile
from ..presence import WRITE_EVERY, record_seen
from .base import (
	BaseTestCase,
	approve_timelapse,
	grant_perms,
	make_journal,
	make_project,
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

	def test_a_site_with_nothing_on_it_does_not_divide_by_zero(self):
		hours = self._hours()

		self.assertEqual(hours["today"], 0)
		self.assertEqual(hours["avg_per_builder"], 0.0)
		self.assertEqual(hours["avg_per_devlog_display"], "0h 0m")
		self.assertEqual(hours["avg_per_day_7"], 0.0)
