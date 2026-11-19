"""The public /queue/ page: rounded queue lengths and wait estimates, and
nothing that identifies a project."""

from datetime import timedelta

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from ..models import Ship, T1, T2, TimelapseReview
from ..views.client.queue_status import (
	SHIP_GOAL_SINCE, _clip_hours, estimate_hours, rounded_count, ship_goal_progress, total_hours, wait_display,
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

	def test_estimate_is_backlog_and_own_review_over_busy_rate(self):
		# 14 decided in 28 busy hours is one every 2 hours; 4 ahead plus your
		# own is 10 hours.
		self.assertEqual(estimate_hours(4, 14, 28), 10)
		# An empty desk that has been working still takes one review's time.
		self.assertEqual(estimate_hours(0, 14, 28), 2)
		self.assertEqual(estimate_hours(0, 0, 0), 0)
		self.assertIsNone(estimate_hours(5, 0, 0))

	def test_busy_hours_merge_overlaps_and_clip_to_the_window(self):
		since = timezone.now()
		h = lambda n: since + timedelta(hours=n)
		now = h(100)
		intervals = [
			(h(-10), h(2)),   # started before the window: 2 hours count
			(h(10), h(14)),
			(h(12), h(16)),   # overlaps the last: 10..16 is 6 hours
			(h(50), h(50)),   # nothing waiting
			(h(98), h(120)),  # still waiting: 2 hours to now
		]
		self.assertEqual(_clip_hours(intervals, since, now), 10)

	def test_total_is_the_sum_unless_a_desk_is_unknown(self):
		self.assertEqual(total_hours([1, 2, 0, 3]), 6)
		self.assertIsNone(total_hours([1, None, 2, 3]))

	def test_waits_round_to_the_nearest_hour(self):
		self.assertEqual(wait_display(None), "—")
		self.assertEqual(wait_display(0), "No wait")
		self.assertEqual(wait_display(0.3), "< 1 hr")
		self.assertEqual(wait_display(0.6), "~1 hr")
		self.assertEqual(wait_display(5.5), "~6 hrs")
		self.assertEqual(wait_display(47), "~47 hrs")

	def test_long_waits_read_in_days(self):
		self.assertEqual(wait_display(48), "~2 days")
		self.assertEqual(wait_display(83), "~3 days")
		self.assertEqual(wait_display(24 * 21), "~21 days")


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

	def _approved_at_t1(self, project, joined, decided):
		"""A ship that waited at T1 from `joined` until `decided`, now in T2."""
		ship = make_ship(project, status=Ship.ShipStatus.T2_QUEUE, journal_minutes=(60,))
		Ship.objects.filter(pk=ship.pk).update(created_at=joined)
		TimelapseReview.objects.filter(journal__ship=ship).update(reviewed_at=joined)
		ship.journals.update(created_at=joined)
		review = T1.objects.create(ship=ship, reviewer=self.reviewer, feedback="", internal_notes="", approved=True)
		T1.objects.filter(pk=review.pk).update(reviewed_at=decided)
		return ship

	def test_wait_comes_from_the_pace_while_the_desk_was_busy(self):
		now = timezone.now()
		project = make_project(self.owner, shippable=True)
		# A deadline pile-up: 12 ships joined five days ago and were all
		# decided within 24 hours. The desk then sat empty for four days.
		pile = now - timedelta(days=5)
		for i in range(12):
			self._approved_at_t1(project, pile, pile + timedelta(hours=2 * (i + 1)))
		# Outside the window: neither its decision nor its wait counts.
		self._approved_at_t1(project, now - timedelta(days=12), now - timedelta(days=9))

		# Five just joined. Busy for 24 of the last 168 hours, 12 decided: one
		# every 2 hours, so 5 ahead plus yours is about 12 hours. Spreading
		# those 12 decisions over the whole week would have said 84.
		for _ in range(5):
			ship = make_ship(project, journal_minutes=(60,))
			Ship.objects.filter(pk=ship.pk).update(created_at=now)

		_response, queues = self._queues()
		self.assertEqual(queues["Project review"]["wait"], "~12 hrs")

	def test_t2_wait_starts_when_t1_passes_the_ship_on(self):
		now = timezone.now()
		project = make_project(self.owner, shippable=True)
		# Created long ago, but only reached T2 when T1 approved it 10 hours
		# ago; T2 decided it 4 hours later. Busy 4 hours, 1 decided.
		ship = self._approved_at_t1(project, now - timedelta(days=30), now - timedelta(hours=10))
		ship.status = Ship.ShipStatus.T3_QUEUE
		ship.save()
		review = T2.objects.create(ship=ship, reviewer=self.reviewer, feedback="", justification="")
		T2.objects.filter(pk=review.pk).update(reviewed_at=now - timedelta(hours=6))

		_response, queues = self._queues()
		self.assertEqual(queues["YSWS review"]["wait"], "~4 hrs")

	def test_timelapse_pace_ignores_footage_nobody_was_waiting_on(self):
		now = timezone.now()
		project = make_project(self.owner, shippable=True)
		ship = make_ship(project, journal_minutes=(60,), timelapse_approved=False)
		Ship.objects.filter(pk=ship.pk).update(created_at=now - timedelta(hours=3))
		ship.journals.update(created_at=now - timedelta(hours=3))
		# Lapses on projects that never shipped get reviewed too; they used to
		# count as cleared and make the desk look far faster than it was.
		for _ in range(20):
			journal = make_journal(make_project(self.owner))
			TimelapseReview.objects.create(journal=journal, reviewer=self.reviewer)

		_response, queues = self._queues()
		self.assertEqual(queues["Timelapse review"]["wait"], "—")

	def test_total_wait_sums_the_desks(self):
		now = timezone.now()
		project = make_project(self.owner, shippable=True)
		ship = self._approved_at_t1(project, now - timedelta(hours=5), now - timedelta(hours=2))
		Ship.objects.filter(pk=ship.pk).update(status=Ship.ShipStatus.FINALIZED)

		response, _queues = self._queues()
		# Only T1 has a pace (one decision in 3 busy hours) and nothing is
		# waiting anywhere: 3 hours at T1, nothing known to hold it elsewhere.
		self.assertEqual(response.context["total_wait"], "~3 hrs")
		self.assertContains(response, "to get through all four")

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
		self.assertContains(response, "mihi fish costume progress")
