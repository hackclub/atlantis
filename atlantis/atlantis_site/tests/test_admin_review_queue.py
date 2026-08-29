"""Walking a review queue: ordering, next, skip, and claims.

The desks and the review pages are covered by test_admin_review /
test_admin_timelapse_review. What's here is the movement between them — the
part that turns four separate pages into one queue a reviewer works through.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from ..models import Journal, LookoutSession, Ship, T1, TimelapseReview
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
		# Reads as a duration mid-sentence, which is where the review pages put it.
		self.assertEqual(age_display(now), "<1m")

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
	"""The Lookout queue holds projects, not lapses.

	Every lapse on a project is the same person recording the same build, so
	the unit of work is the project: one page, one sitting, one decision.
	"""

	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("lapserev"), "timelapse_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)

	def _describe(self, project):
		"""A description for every Lookout waiting on the project.

		The decision view won't take a pass until each recording in it has one,
		so a test about where "next" lands has to supply them.
		"""
		sessions = LookoutSession.objects.filter(
			journal__project=project, journal__timelapse_review__isnull=True
		)
		return {f"description_{session.id}": "watched it" for session in sessions}

	def _lapse(self, project, days_ago=0, **kwargs):
		journal = make_journal(project, **kwargs)
		Journal.objects.filter(pk=journal.pk).update(
			created_at=timezone.now() - timedelta(days=days_ago)
		)
		journal.refresh_from_db()
		return journal

	def test_next_opens_the_project_not_the_lapse(self):
		self._lapse(self.project, 3)
		self._lapse(self.project, 1)
		response = self.client.get(reverse("timelapse_review_next"))
		self.assertRedirects(
			response,
			reverse("timelapse_review_project", args=[self.project.id]),
			fetch_redirect_response=False,
		)

	def test_projects_are_ordered_by_their_longest_waiting_lapse(self):
		newer = make_project(make_user("newer"), shippable=True)
		older = make_project(make_user("older"), shippable=True)
		self._lapse(newer, 2)
		self._lapse(older, 9)

		rows = self.client.get(reverse("timelapse_review_dash")).context["projects"]
		self.assertEqual([p.id for p in rows], [older.id, newer.id])

	def test_a_project_waits_from_its_oldest_lapse_not_its_creation(self):
		self._lapse(self.project, 9)
		self._lapse(self.project, 1)
		row = self.client.get(reverse("timelapse_review_dash")).context["projects"][0]
		self.assertEqual(row.age_display, "9d")
		self.assertEqual(row.age_bucket, "overdue")

	def test_a_row_totals_the_work_inside_it(self):
		ship = make_ship(self.project, journal_minutes=(), timelapse_approved=False)
		self._lapse(self.project, 3, ship=ship, time_spent=90)
		self._lapse(self.project, 1, ship=ship, time_spent=30)

		row = self.client.get(reverse("timelapse_review_dash")).context["projects"][0]
		self.assertEqual(row.lapse_count, 2)
		self.assertEqual(row.lookout_count, 2)
		self.assertEqual(row.tracked_label, "2h 0m")
		self.assertEqual(row.held_ships, [ship.id])

	def test_lapses_within_a_project_are_oldest_first(self):
		second = self._lapse(self.project, 4)
		first = self._lapse(self.project, 8)
		row = self.client.get(reverse("timelapse_review_dash")).context["projects"][0]
		self.assertEqual([lapse.id for lapse in row.lapses], [first.id, second.id])

	def test_the_desk_lists_a_few_lapses_and_counts_the_rest(self):
		for day in range(8):
			self._lapse(self.project, 8 - day)
		row = self.client.get(reverse("timelapse_review_dash")).context["projects"][0]
		self.assertEqual(len(row.preview_lapses), 5)
		self.assertEqual(row.more_lapses, 3)

	def test_an_unshipped_project_is_on_the_desk(self):
		"""A journal has a project long before it has a ship."""
		journal = make_journal(self.project)
		self.assertIsNone(journal.ship_id)

		row = self.client.get(reverse("timelapse_review_dash")).context["projects"][0]
		self.assertEqual(row.id, self.project.id)
		self.assertEqual(row.held_ships, [])

	def test_signing_off_opens_the_next_project(self):
		other = make_project(make_user("other"), shippable=True)
		self._lapse(self.project, 6)
		self._lapse(other, 2)

		response = self.client.post(
			reverse("timelapse_decision", args=[self.project.id]),
			{"internal_notes": "clean", **self._describe(self.project)},
		)
		self.assertRedirects(
			response,
			f"{reverse('timelapse_review_project', args=[other.id])}?skip={self.project.id}",
			fetch_redirect_response=False,
		)

	def test_the_last_pass_returns_to_the_desk(self):
		self._lapse(self.project)
		response = self.client.post(
			reverse("timelapse_decision", args=[self.project.id]),
			{"internal_notes": "clean", **self._describe(self.project)},
		)
		self.assertRedirects(
			response, reverse("timelapse_review_dash"), fetch_redirect_response=False
		)

	def test_the_header_counts_projects_and_the_lapses_under_them(self):
		other = make_project(make_user("other"), shippable=True)
		self._lapse(self.project)
		self._lapse(self.project)
		self._lapse(other)

		stats = {s["label"]: s["value"] for s in
				 self.client.get(reverse("timelapse_review_dash")).context["queue_stats"]}
		self.assertEqual(stats["Waiting"], "2")
		self.assertEqual(stats["Lapses"], "3")


class LookoutReviewPageTests(BaseTestCase):
	"""One page per project: everything waiting on it, and what came before."""

	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("lapserev"), "timelapse_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)

	def _page(self):
		return self.client.get(
			reverse("timelapse_review_project", args=[self.project.id])
		)

	def test_the_page_carries_every_waiting_lapse(self):
		first = make_journal(self.project, time_spent=60)
		second = make_journal(self.project, time_spent=30)
		context = self._page().context

		self.assertEqual([lapse.id for lapse in context["pending"]], [first.id, second.id])
		self.assertEqual(context["lapse_count"], 2)
		self.assertEqual(context["lookout_count"], 2)
		self.assertEqual(context["tracked_display"], "1h 30m")

	def test_already_signed_off_lapses_are_shown_read_only(self):
		done = make_journal(self.project, time_spent=60)
		approve_timelapse(done, reviewer=self.reviewer, internal_notes="watched it")
		make_journal(self.project, time_spent=30)

		context = self._page().context
		self.assertEqual([lapse.id for lapse in context["reviewed"]], [done.id])
		self.assertEqual(context["reviewed"][0].reviewer_name, "lapserev")

	def test_a_finished_project_offers_nothing_to_decide(self):
		approve_timelapse(make_journal(self.project), reviewer=self.reviewer)
		context = self._page().context
		self.assertEqual(list(context["pending"]), [])
		self.assertFalse(context["claim_held"])
		self.assertIsNone(context["queue_position"])

	def test_position_counts_projects(self):
		other = make_project(make_user("other"), shippable=True)
		journal = make_journal(other)
		Journal.objects.filter(pk=journal.pk).update(
			created_at=timezone.now() - timedelta(days=5)
		)
		make_journal(self.project)

		context = self._page().context
		self.assertEqual(context["queue_position"], 2)
		self.assertEqual(context["queue_total"], 2)

	def test_a_deleted_project_is_gone(self):
		make_journal(self.project)
		self.project.deleted = True
		self.project.save()
		self.assertEqual(self._page().status_code, 404)


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

	def test_a_finished_project_is_not_claimed(self):
		approve_timelapse(make_journal(self.project))
		self.client.force_login(grant_perms(make_user("lapserev"), "timelapse_review"))
		self.client.get(reverse("timelapse_review_project", args=[self.project.id]))
		self.assertIsNone(claim_holder("lookout", self.project.id))

	def test_opening_a_project_pass_claims_the_project(self):
		make_journal(self.project)
		reviewer = grant_perms(make_user("lapserev2"), "timelapse_review")
		self.client.force_login(reviewer)
		self.client.get(reverse("timelapse_review_project", args=[self.project.id]))
		self.assertEqual(claim_holder("lookout", self.project.id)["user_id"], reviewer.id)

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
