"""The public /queue/ page: rounded queue lengths and wait estimates, and
nothing that identifies a project."""

from datetime import timedelta

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from ..models import Ship, T1
from ..views.client.queue_status import (
	SHIP_GOAL_SINCE, estimate_hours, rounded_count, ship_goal_progress, wait_display,
)
from .base import BaseTestCase, make_journal, make_project, make_ship, make_user


class RoundingTests(BaseTestCase):
	def test_counts_round_to_the_nearest_ten(self):
		self.assertEqual(rounded_count(0), "0")
		self.assertEqual(rounded_count(1), "<10")
		self.assertEqual(rounded_count(9), "<10")
		self.assertEqual(rounded_count(14), "10")
		self.assertEqual(rounded_count(15), "20")
		self.assertEqual(rounded_count(1234), "1,230")

	def test_estimate_is_backlog_over_rate(self):
		# 14 cleared in a week is 1 every 12 hours; 4 waiting is 48 hours.
		self.assertEqual(estimate_hours(4, 14, timedelta(days=7)), 48)
		self.assertEqual(estimate_hours(0, 0), 0)
		self.assertIsNone(estimate_hours(5, 0))

	def test_waits_round_to_the_nearest_hour(self):
		self.assertEqual(wait_display(None), "—")
		self.assertEqual(wait_display(0), "No wait")
		self.assertEqual(wait_display(0.3), "< 1 hr")
		self.assertEqual(wait_display(0.6), "~1 hr")
		self.assertEqual(wait_display(5.5), "~6 hrs")
		self.assertEqual(wait_display(60), "~60 hrs")


class QueueStatusPageTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		cache.clear()
		self.owner = make_user("author")
		self.reviewer = make_user("rev", slack_id="U0REV")

	def _queues(self):
		response = self.client.get(reverse("queue_status"))
		self.assertEqual(response.status_code, 200)
		return response, {queue["label"]: queue for queue in response.context["queues"]}

	def test_public_to_anonymous_visitors(self):
		self.assertEqual(self.client.get(reverse("queue_status")).status_code, 200)

	def test_counts_each_queue_rounded(self):
		project = make_project(self.owner, shippable=True, title="Secret Submarine")
		for _ in range(14):
			make_ship(project, journal_minutes=(60,))
		for _ in range(3):
			make_ship(project, status=Ship.ShipStatus.T2_QUEUE, journal_minutes=(60,))

		response, queues = self._queues()
		self.assertEqual(queues["Project review"]["count"], "10")
		self.assertEqual(queues["YSWS review"]["count"], "<10")
		self.assertEqual(queues["Final review"]["count"], "0")
		self.assertEqual(queues["Final review"]["wait"], "No wait")
		self.assertNotContains(response, "Secret Submarine")

	def test_timelapse_queue_counts_only_shipped_projects(self):
		shipped = make_project(self.owner, shippable=True)
		make_ship(shipped, journal_minutes=(60,), timelapse_approved=False)
		make_journal(make_project(self.owner))  # never shipped: nobody waiting on it

		_response, queues = self._queues()
		self.assertEqual(queues["Timelapse review"]["count"], "<10")
		# Nothing decided this week, so there's no rate to estimate from.
		self.assertEqual(queues["Timelapse review"]["wait"], "—")

	def test_wait_comes_from_the_last_week_of_decisions(self):
		project = make_project(self.owner, shippable=True)
		for _ in range(4):
			make_ship(project, journal_minutes=(60,))
		for _ in range(14):
			ship = make_ship(project, status=Ship.ShipStatus.T2_QUEUE, journal_minutes=(60,))
			T1.objects.create(ship=ship, reviewer=self.reviewer, feedback="", internal_notes="", approved=True)
		# Outside the window: doesn't count towards the rate.
		old = T1.objects.create(
			ship=make_ship(project, status=Ship.ShipStatus.T2_QUEUE, journal_minutes=(60,)),
			reviewer=self.reviewer, feedback="", internal_notes="", approved=True,
		)
		T1.objects.filter(pk=old.pk).update(reviewed_at=timezone.now() - timedelta(days=8))

		_response, queues = self._queues()
		self.assertEqual(queues["Project review"]["wait"], "~48 hrs")

	def test_cached_between_requests(self):
		self._queues()
		make_ship(make_project(self.owner, shippable=True), journal_minutes=(60,))
		_response, queues = self._queues()
		self.assertEqual(queues["Project review"]["count"], "0")


class ShipGoalTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		cache.clear()
		self.owner = make_user("author")

	def _ship_at(self, project, at):
		ship = make_ship(project, journal_minutes=(60,))
		Ship.objects.filter(pk=ship.pk).update(created_at=at)

	def test_progress_caps_at_the_goal(self):
		self.assertEqual(ship_goal_progress(0)["percent"], 0)
		self.assertEqual(ship_goal_progress(150)["percent"], 50)
		self.assertFalse(ship_goal_progress(299)["reached"])
		self.assertEqual(ship_goal_progress(450)["percent"], 100)
		self.assertTrue(ship_goal_progress(450)["reached"])

	def test_counts_unique_projects_shipped_since_the_start(self):
		twice = make_project(self.owner, shippable=True)
		self._ship_at(twice, SHIP_GOAL_SINCE + timedelta(hours=1))
		self._ship_at(twice, SHIP_GOAL_SINCE + timedelta(hours=2))
		self._ship_at(make_project(self.owner, shippable=True), SHIP_GOAL_SINCE)
		self._ship_at(make_project(self.owner, shippable=True), SHIP_GOAL_SINCE - timedelta(seconds=1))
		gone = make_project(self.owner, shippable=True)
		self._ship_at(gone, SHIP_GOAL_SINCE + timedelta(hours=1))
		gone.deleted = True
		gone.save()

		response = self.client.get(reverse("queue_status"))
		self.assertEqual(response.context["ship_goal"]["shipped"], 2)
		self.assertContains(response, "Mihi fish costume progress")
