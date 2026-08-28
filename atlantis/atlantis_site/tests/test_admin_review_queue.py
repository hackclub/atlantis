"""Walking a review queue: ordering, next, skip, and claims.

The desks and the review pages are covered by test_admin_review /
test_admin_timelapse_review. What's here is the movement between them — the
part that turns four separate pages into one queue a reviewer works through.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from ..models import Journal, Ship, T1, TimelapseReview
from ..views.admin.queue import (
	age_bucket, age_display, claim_holder, claim_review, next_item_id,
	release_claim,
)
from .base import (
	BaseTestCase,
	approve_timelapse,
	grant_perms,
	make_journal,
	make_project,
	make_ship,
	make_user,
	message_texts,
)


def ages(ship, **offsets):
	"""Backdate a ship. created_at is auto_now_add, so it needs an UPDATE."""
	Ship.objects.filter(pk=ship.pk).update(created_at=timezone.now() - timedelta(**offsets))
	ship.refresh_from_db()
	return ship


class QueueOrderTests(BaseTestCase):
	"""Oldest first. A queue worked newest-first starves the people who have
	been waiting longest, which is the whole population the queue exists for."""

	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t1rev"), "t1_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)

	def test_pending_is_oldest_first(self):
		newest = make_ship(self.project)
		oldest = ages(make_ship(self.project), days=9)
		middle = ages(make_ship(self.project), days=3)

		response = self.client.get(reverse("review_dash"))
		self.assertEqual(
			[ship.id for ship in response.context["ships"]],
			[oldest.id, middle.id, newest.id],
		)

	def test_rows_carry_their_wait(self):
		ages(make_ship(self.project), days=9)
		row = self.client.get(reverse("review_dash")).context["ships"][0]
		self.assertEqual(row.age_display, "9d")
		self.assertEqual(row.age_bucket, "overdue")

	def test_deleted_projects_are_not_in_the_queue(self):
		make_ship(self.project)
		self.project.deleted = True
		self.project.save()
		self.assertEqual(list(self.client.get(reverse("review_dash")).context["ships"]), [])

	def test_age_display_units(self):
		now = timezone.now()
		self.assertEqual(age_display(now - timedelta(days=2, hours=3)), "2d")
		self.assertEqual(age_display(now - timedelta(hours=5)), "5h")
		self.assertEqual(age_display(now - timedelta(minutes=20)), "20m")
		self.assertEqual(age_display(now), "new")

	def test_age_buckets_track_the_sla(self):
		now = timezone.now()
		self.assertEqual(age_bucket(now - timedelta(days=5), 4), "overdue")
		self.assertEqual(age_bucket(now - timedelta(days=3), 4), "aging")
		self.assertEqual(age_bucket(now - timedelta(hours=2), 4), "fresh")


class NextTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t1rev"), "t1_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)

	def test_next_opens_the_longest_waiting_ship(self):
		make_ship(self.project)
		oldest = ages(make_ship(self.project), days=6)
		response = self.client.get(reverse("review_next"))
		self.assertRedirects(
			response, reverse("review_project", args=[oldest.id]), fetch_redirect_response=False
		)

	def test_skip_passes_over_a_ship(self):
		oldest = ages(make_ship(self.project), days=6)
		second = ages(make_ship(self.project), days=2)
		response = self.client.get(reverse("review_next"), {"skip": str(oldest.id)})
		self.assertRedirects(
			response,
			f"{reverse('review_project', args=[second.id])}?skip={oldest.id}",
			fetch_redirect_response=False,
		)

	def test_skips_accumulate_down_the_queue(self):
		first = ages(make_ship(self.project), days=6)
		second = ages(make_ship(self.project), days=4)
		third = ages(make_ship(self.project), days=2)
		response = self.client.get(reverse("review_next"), {"skip": f"{first.id},{second.id}"})
		self.assertRedirects(
			response,
			f"{reverse('review_project', args=[third.id])}?skip={first.id},{second.id}",
			fetch_redirect_response=False,
		)

	def test_empty_queue_goes_back_to_the_desk(self):
		response = self.client.get(reverse("review_next"))
		self.assertRedirects(response, reverse("review_dash"), fetch_redirect_response=False)
		self.assertIn("Nothing else waiting in project review.", message_texts(response))

	def test_junk_in_the_skip_list_is_ignored(self):
		ship = make_ship(self.project)
		response = self.client.get(reverse("review_next"), {"skip": "abc,,-4, 9x"})
		self.assertRedirects(
			response, reverse("review_project", args=[ship.id]), fetch_redirect_response=False
		)

	def test_next_needs_the_queue_s_permission(self):
		self.client.force_login(grant_perms(make_user("t1only"), "t1_review"))
		self.assertEqual(self.client.get(reverse("fraud_review_next")).status_code, 302)
		self.assertEqual(self.client.get(reverse("ysws_review_next")).status_code, 302)


class DecisionAdvancesTests(BaseTestCase):
	"""A decision lands the reviewer on the next item, not back at the desk."""

	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t1rev"), "t1_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author", slack_id="U0A"), shippable=True)

	def _approve(self, ship, query=""):
		return self.client.post(
			reverse("t1_decision", args=[ship.id]) + query,
			{"feedback": "nice", "internal_notes": "ok", "approved": "approved"},
		)

	def test_decision_opens_the_next_ship(self):
		first = ages(make_ship(self.project), days=6)
		second = ages(make_ship(self.project), days=2)
		response = self._approve(first)
		self.assertRedirects(
			response,
			f"{reverse('review_project', args=[second.id])}?skip={first.id}",
			fetch_redirect_response=False,
		)

	def test_last_decision_returns_to_the_desk(self):
		only = make_ship(self.project)
		response = self._approve(only)
		self.assertRedirects(response, reverse("review_dash"), fetch_redirect_response=False)

	def test_skips_survive_the_decision(self):
		skipped = ages(make_ship(self.project), days=8)
		current = ages(make_ship(self.project), days=6)
		nxt = ages(make_ship(self.project), days=2)
		response = self._approve(current, query=f"?skip={skipped.id}")
		self.assertRedirects(
			response,
			f"{reverse('review_project', args=[nxt.id])}?skip={skipped.id},{current.id}",
			fetch_redirect_response=False,
		)

	def test_the_decision_itself_is_unchanged(self):
		ship = make_ship(self.project)
		self._approve(ship)
		ship.refresh_from_db()
		self.assertEqual(ship.status, Ship.ShipStatus.T2_QUEUE)
		self.assertTrue(T1.objects.get(ship=ship).approved)


class LookoutQueueTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("lapserev"), "timelapse_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)

	def test_next_opens_the_longest_waiting_lapse(self):
		first = make_journal(self.project)
		make_journal(self.project)
		response = self.client.get(reverse("timelapse_review_next"))
		self.assertRedirects(
			response,
			reverse("timelapse_review_journal", args=[first.id]),
			fetch_redirect_response=False,
		)

	def test_reviewed_lapses_leave_the_queue(self):
		reviewed = make_journal(self.project)
		approve_timelapse(reviewed)
		pending = make_journal(self.project)
		response = self.client.get(reverse("timelapse_review_next"))
		self.assertRedirects(
			response,
			reverse("timelapse_review_journal", args=[pending.id]),
			fetch_redirect_response=False,
		)

	def test_signing_off_opens_the_next_lapse(self):
		first = make_journal(self.project)
		second = make_journal(self.project)
		response = self.client.post(
			reverse("timelapse_decision", args=[first.id]), {"internal_notes": "clean"}
		)
		self.assertRedirects(
			response,
			f"{reverse('timelapse_review_journal', args=[second.id])}?skip={first.id}",
			fetch_redirect_response=False,
		)
		self.assertTrue(TimelapseReview.objects.filter(journal=first).exists())

	def test_last_sign_off_returns_to_the_desk(self):
		only = make_journal(self.project)
		response = self.client.post(
			reverse("timelapse_decision", args=[only.id]), {"internal_notes": "clean"}
		)
		self.assertRedirects(
			response, reverse("timelapse_review_dash"), fetch_redirect_response=False
		)

	def test_pending_is_oldest_first_across_projects(self):
		other = make_project(make_user("someone-else"), shippable=True)
		first = make_journal(self.project)
		second = make_journal(other)
		rows = self.client.get(reverse("timelapse_review_dash")).context["pending"]
		self.assertEqual([j.id for j in rows], [first.id, second.id])


class LookoutGroupingTests(BaseTestCase):
	"""The desk gathers lapses under their project, and `next` walks the same way.

	A project's lapses are the same footage and the same person, so they are
	reviewed together rather than met one at a time in whatever order the
	global clock happens to produce.
	"""

	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("lapserev"), "timelapse_review")
		self.client.force_login(self.reviewer)

	def _lapse(self, project, days_ago, **kwargs):
		journal = make_journal(project, **kwargs)
		Journal.objects.filter(pk=journal.pk).update(
			created_at=timezone.now() - timedelta(days=days_ago)
		)
		journal.refresh_from_db()
		return journal

	def test_projects_are_ordered_by_their_longest_waiting_lapse(self):
		newer = make_project(make_user("newer"), shippable=True)
		older = make_project(make_user("older"), shippable=True)
		self._lapse(newer, 2)
		self._lapse(older, 9)

		groups = self.client.get(reverse("timelapse_review_dash")).context["groups"]
		self.assertEqual([g["project"].id for g in groups], [older.id, newer.id])

	def test_a_project_s_lapses_stay_together_and_oldest_first(self):
		one = make_project(make_user("one"), shippable=True)
		two = make_project(make_user("two"), shippable=True)
		# Interleaved in time: without grouping these would alternate.
		one_old = self._lapse(one, 8)
		two_mid = self._lapse(two, 6)
		one_new = self._lapse(one, 4)
		two_new = self._lapse(two, 2)

		rows = self.client.get(reverse("timelapse_review_dash")).context["pending"]
		self.assertEqual(
			[j.id for j in rows], [one_old.id, one_new.id, two_mid.id, two_new.id]
		)

	def test_next_clears_a_project_before_moving_on(self):
		one = make_project(make_user("one"), shippable=True)
		two = make_project(make_user("two"), shippable=True)
		one_old = self._lapse(one, 8)
		two_mid = self._lapse(two, 6)
		one_new = self._lapse(one, 4)

		self.assertEqual(next_item_id("lookout"), one_old.id)
		self.assertEqual(next_item_id("lookout", skip_ids=[one_old.id]), one_new.id)
		self.assertEqual(
			next_item_id("lookout", skip_ids=[one_old.id, one_new.id]), two_mid.id
		)

	def test_numbering_matches_the_order_next_walks(self):
		one = make_project(make_user("one"), shippable=True)
		two = make_project(make_user("two"), shippable=True)
		self._lapse(one, 8)
		self._lapse(two, 6)
		self._lapse(one, 4)

		rows = self.client.get(reverse("timelapse_review_dash")).context["pending"]
		self.assertEqual([j.queue_index for j in rows], [1, 2, 3])

	def test_an_unshipped_project_is_on_the_desk(self):
		"""A journal has a project long before it has a ship."""
		unshipped = make_project(make_user("drafting"), shippable=True)
		journal = make_journal(unshipped)
		self.assertIsNone(journal.ship_id)

		groups = self.client.get(reverse("timelapse_review_dash")).context["groups"]
		self.assertEqual([g["project"].id for g in groups], [unshipped.id])
		self.assertEqual(groups[0]["held_ships"], [])

	def test_a_group_summarises_what_is_inside_it(self):
		project = make_project(make_user("busy"), shippable=True)
		ship = make_ship(project, journal_minutes=(), timelapse_approved=False)
		self._lapse(project, 3, ship=ship, time_spent=90)
		self._lapse(project, 1, ship=ship, time_spent=30)

		group = self.client.get(reverse("timelapse_review_dash")).context["groups"][0]
		self.assertEqual(group["count"], 2)
		self.assertEqual(group["tracked_display"], "2h 0m")
		self.assertEqual(group["lookouts"], 2)
		self.assertEqual(group["held_ships"], [ship.id])
		self.assertEqual(group["age_display"], "3d")
		self.assertEqual(group["owner"], "busy")

	def test_the_header_counts_projects_as_well_as_lapses(self):
		one = make_project(make_user("one"), shippable=True)
		two = make_project(make_user("two"), shippable=True)
		make_journal(one)
		make_journal(one)
		make_journal(two)

		stats = {s["label"]: s["value"] for s in
				 self.client.get(reverse("timelapse_review_dash")).context["queue_stats"]}
		self.assertEqual(stats["Waiting"], "3")
		self.assertEqual(stats["Projects"], "2")

	def test_a_reviewed_project_leaves_the_desk_entirely(self):
		project = make_project(make_user("done"), shippable=True)
		approve_timelapse(make_journal(project))
		context = self.client.get(reverse("timelapse_review_dash")).context
		self.assertEqual(list(context["pending"]), [])
		self.assertEqual(context["groups"], [])


class ClaimTests(BaseTestCase):
	"""One reviewer at a time, held in the cache and expiring on its own."""

	def setUp(self):
		super().setUp()
		self.one = grant_perms(make_user("rev-one"), "t1_review")
		self.two = grant_perms(make_user("rev-two"), "t1_review")
		self.project = make_project(make_user("author"), shippable=True)

	def test_opening_a_review_claims_it(self):
		ship = make_ship(self.project)
		self.client.force_login(self.one)
		self.client.get(reverse("review_project", args=[ship.id]))
		self.assertEqual(claim_holder("t1", ship.id)["user_id"], self.one.id)

	def test_a_claimed_review_is_skipped_by_next(self):
		claimed = ages(make_ship(self.project), days=6)
		free = ages(make_ship(self.project), days=2)
		claim_review("t1", claimed.id, self.one)

		self.client.force_login(self.two)
		response = self.client.get(reverse("review_next"))
		self.assertRedirects(
			response, reverse("review_project", args=[free.id]), fetch_redirect_response=False
		)

	def test_a_claim_of_your_own_is_not_skipped(self):
		mine = ages(make_ship(self.project), days=6)
		ages(make_ship(self.project), days=2)
		claim_review("t1", mine.id, self.one)
		self.assertEqual(next_item_id("t1", user=self.one), mine.id)

	def test_a_fully_claimed_queue_sends_you_back_to_the_desk(self):
		ship = make_ship(self.project)
		claim_review("t1", ship.id, self.one)
		self.client.force_login(self.two)
		self.assertRedirects(
			self.client.get(reverse("review_next")),
			reverse("review_dash"),
			fetch_redirect_response=False,
		)

	def test_visiting_a_claimed_review_directly_warns_instead_of_bouncing(self):
		ship = make_ship(self.project)
		claim_review("t1", ship.id, self.one)
		self.client.force_login(self.two)
		response = self.client.get(reverse("review_project", args=[ship.id]))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["claim_conflict"]["user_id"], self.one.id)

	def test_a_reviewer_only_ever_holds_one_claim(self):
		first = make_ship(self.project)
		second = make_ship(self.project)
		claim_review("t1", first.id, self.one)
		claim_review("t1", second.id, self.one)
		self.assertIsNone(claim_holder("t1", first.id))
		self.assertEqual(claim_holder("t1", second.id)["user_id"], self.one.id)

	def test_going_back_to_the_desk_releases_the_claim(self):
		ship = make_ship(self.project)
		self.client.force_login(self.one)
		self.client.get(reverse("review_project", args=[ship.id]))
		self.client.get(reverse("review_dash"))
		self.assertIsNone(claim_holder("t1", ship.id))

	def test_deciding_releases_the_claim(self):
		ship = make_ship(self.project)
		self.client.force_login(self.one)
		self.client.get(reverse("review_project", args=[ship.id]))
		self.client.post(
			reverse("t1_decision", args=[ship.id]),
			{"feedback": "nice", "internal_notes": "ok", "approved": "approved"},
		)
		self.assertIsNone(claim_holder("t1", ship.id))

	def test_a_settled_ship_is_not_claimed(self):
		"""Nothing can be decided on it, so a lease on it would only be in the way."""
		ship = make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)
		self.client.force_login(self.one)
		response = self.client.get(reverse("review_project", args=[ship.id]))
		self.assertIsNone(claim_holder("t1", ship.id))
		self.assertIsNone(response.context["queue_position"])

	def test_a_reviewed_lapse_is_not_claimed(self):
		journal = make_journal(self.project)
		approve_timelapse(journal)
		self.client.force_login(grant_perms(make_user("lapserev"), "timelapse_review"))
		self.client.get(reverse("timelapse_review_journal", args=[journal.id]))
		self.assertIsNone(claim_holder("lookout", journal.id))

	def test_release_is_a_no_op_without_a_claim(self):
		self.assertFalse(release_claim(self.one))


class HeartbeatTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.one = grant_perms(make_user("rev-one"), "t1_review")
		self.two = grant_perms(make_user("rev-two"), "t1_review")
		self.project = make_project(make_user("author"), shippable=True)
		self.ship = make_ship(self.project)

	def _beat(self, queue="t1", item_id=None):
		return self.client.post(
			reverse("review_heartbeat", args=[queue, item_id or self.ship.id])
		)

	def test_the_holder_keeps_the_claim(self):
		self.client.force_login(self.one)
		claim_review("t1", self.ship.id, self.one)
		response = self._beat()
		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.json()["ok"])

	def test_someone_else_s_claim_answers_409(self):
		claim_review("t1", self.ship.id, self.one)
		self.client.force_login(self.two)
		response = self._beat()
		self.assertEqual(response.status_code, 409)
		self.assertEqual(response.json()["holder"], "rev-one")

	def test_an_unclaimed_review_is_claimed_by_the_beat(self):
		self.client.force_login(self.one)
		self.assertEqual(self._beat().status_code, 200)
		self.assertEqual(claim_holder("t1", self.ship.id)["user_id"], self.one.id)

	def test_get_is_not_allowed(self):
		self.client.force_login(self.one)
		self.assertEqual(
			self.client.get(reverse("review_heartbeat", args=["t1", self.ship.id])).status_code,
			405,
		)

	def test_a_queue_you_can_t_review_is_refused(self):
		self.client.force_login(self.one)  # t1 only
		response = self.client.post(reverse("review_heartbeat", args=["t3", self.ship.id]))
		self.assertEqual(response.status_code, 403)

	def test_an_unknown_queue_is_a_404(self):
		self.client.force_login(self.one)
		response = self.client.post(reverse("review_heartbeat", args=["nope", self.ship.id]))
		self.assertEqual(response.status_code, 404)

	def test_anonymous_is_turned_away(self):
		self.assertEqual(self._beat().status_code, 302)


class ReviewContextTests(BaseTestCase):
	"""What the shell needs to place a review in its queue."""

	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)

	def test_position_counts_from_the_front_of_the_queue(self):
		ages(make_ship(self.project), days=6)
		second = ages(make_ship(self.project), days=4)
		ages(make_ship(self.project), days=2)
		response = self.client.get(reverse("review_project", args=[second.id]))
		self.assertEqual(response.context["queue_position"], 2)
		self.assertEqual(response.context["queue_total"], 3)
		self.assertEqual(response.context["queue_remaining"], 1)

	def test_skip_url_adds_this_review_to_the_skip_list(self):
		ship = make_ship(self.project)
		response = self.client.get(reverse("review_project", args=[ship.id]), {"skip": "42"})
		self.assertEqual(
			response.context["skip_url"], f"{reverse('review_next')}?skip=42,{ship.id}"
		)
		self.assertEqual(response.context["skip_value"], "42")

	def test_the_owner_snapshot_summarises_their_record(self):
		author = make_user("prolific", layers=140)
		project = make_project(author, shippable=True)
		make_ship(project, status=Ship.ShipStatus.FINALIZED)
		make_ship(project, status=Ship.ShipStatus.REJECTED)
		ship = make_ship(project)
		owner = self.client.get(reverse("review_project", args=[ship.id])).context["owner"]
		self.assertEqual(owner["pearls"], 140)
		self.assertEqual(owner["ships"], 3)
		self.assertEqual(owner["finalized"], 1)
		self.assertEqual(owner["rejected"], 1)
		self.assertTrue(owner["verified"])

	def test_the_ship_snapshot_separates_this_ship_from_the_project(self):
		make_journal(self.project)  # an older entry, not on this ship
		ship = make_ship(self.project, journal_minutes=(60, 30))
		subject = self.client.get(reverse("review_project", args=[ship.id])).context["subject"]
		self.assertEqual(subject["journals"], 3)
		self.assertEqual(subject["ship_journals"], 2)
		self.assertEqual(subject["attempt"], 1)
		self.assertEqual(subject["attempts"], 1)

	def test_preflight_flags_what_is_missing(self):
		bare = make_project(make_user("careless"))
		ship = make_ship(bare, journal_minutes=())
		preflight = self.client.get(reverse("review_project", args=[ship.id])).context["preflight"]
		labels = {check["label"]: check["state"] for check in preflight["checks"]}
		self.assertEqual(labels["Printables listing"], "fail")
		self.assertEqual(labels["Lookout footage"], "fail")
		self.assertEqual(labels["Editor model"], "warn")
		self.assertEqual(preflight["failed"], 2)

	def test_preflight_is_quiet_on_a_clean_ship(self):
		ship = make_ship(self.project)
		preflight = self.client.get(reverse("review_project", args=[ship.id])).context["preflight"]
		self.assertEqual(preflight["failed"], 0)
		self.assertEqual(preflight["warned"], 0)

	def test_the_desk_header_counts_the_queue(self):
		ages(make_ship(self.project), days=9)
		make_ship(self.project)
		stats = {stat["label"]: stat["value"] for stat in
				 self.client.get(reverse("review_dash")).context["queue_stats"]}
		self.assertEqual(stats["Waiting"], "2")
		self.assertEqual(stats["Oldest"], "9d")
		self.assertEqual(stats["Overdue"], "1")
