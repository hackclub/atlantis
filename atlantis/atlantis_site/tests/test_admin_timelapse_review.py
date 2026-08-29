from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.urls import reverse

from ..models import (
	AuditLog, Journal, Ship, TimelapseRemoval, TimelapseReview, first_overlap,
	format_timecode, parse_timecode, tracked_to_video, video_to_tracked,
)
from ..views.admin.timelapse_review import _locked_pending_lapses
from .base import (
	BaseTestCase,
	approve_timelapse,
	grant_perms,
	make_journal,
	make_project,
	make_ship,
	make_timelapse,
	make_user,
	message_texts,
)


class TimecodeTests(BaseTestCase):
	def test_parses_the_formats_reviewers_type(self):
		cases = {
			"0:05": 5,
			"0:30": 30,
			"1:05": 65,
			"90:00": 5400,
			"1:30:00": 5400,
			"  2:00  ": 120,
			"45": 45,
		}
		for raw, expected in cases.items():
			with self.subTest(raw=raw):
				self.assertEqual(parse_timecode(raw), expected)

	def test_rejects_unreadable_ranges(self):
		for raw in ("", "abc", "1:90", "1:2:3:4", "-1:00", "1:-5", "٣:٠٠", "1.5", None):
			with self.subTest(raw=raw):
				self.assertIsNone(parse_timecode(raw))

	def test_formats_with_hours_only_when_needed(self):
		self.assertEqual(format_timecode(5), "0:05")
		self.assertEqual(format_timecode(65), "1:05")
		self.assertEqual(format_timecode(5400), "1:30:00")

	def test_video_offsets_convert_to_the_minutes_they_stand_for(self):
		# One second of compiled video is one recorded minute, so the range a
		# reviewer reads off the player is sixty times shorter than its cost.
		self.assertEqual(video_to_tracked(1), 60)
		self.assertEqual(video_to_tracked(parse_timecode("1:11")) - video_to_tracked(
			parse_timecode("0:56")
		), 15 * 60)

	def test_tracked_seconds_convert_back_rounding_up(self):
		self.assertEqual(tracked_to_video(3600), 60)
		# A part-minute still occupies a whole second of footage.
		self.assertEqual(tracked_to_video(3540 + 1), 60)
		self.assertEqual(tracked_to_video(0), 0)
		self.assertEqual(tracked_to_video(-5), 0)

	def test_first_overlap_ignores_adjacent_ranges(self):
		self.assertIsNone(first_overlap([(0, 30), (30, 60)]))
		self.assertIsNone(first_overlap([]))
		self.assertEqual(first_overlap([(0, 40), (30, 60)]), (30, 60))
		# Order of the input doesn't matter.
		self.assertEqual(first_overlap([(30, 60), (0, 40)]), (30, 60))


class TimelapseReviewAccessControlTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.project = make_project(make_user("author"))
		self.journal = make_journal(self.project)

	def _urls(self):
		return [
			reverse("timelapse_review_dash"),
			reverse("timelapse_review_project", args=[self.project.id]),
		]

	def test_anonymous_redirected(self):
		for url in self._urls():
			with self.subTest(url=url):
				self.assertEqual(self.client.get(url).status_code, 302)

	def test_regular_user_redirected(self):
		self.client.force_login(make_user("pleb"))
		for url in self._urls():
			with self.subTest(url=url):
				self.assertEqual(self.client.get(url).status_code, 302)

	def test_staff_without_perms_redirected(self):
		staff = make_user("staffonly")
		staff.is_staff = True
		staff.save()
		self.client.force_login(staff)
		for url in self._urls():
			with self.subTest(url=url):
				self.assertEqual(self.client.get(url).status_code, 302)

	def test_project_reviewers_do_not_inherit_it(self):
		"""The whole point of a separate permission — a T1/T2/T3 reviewer is not
		a timelapse reviewer unless somebody says so."""
		for codename in ("t1_review", "t2_review", "t3_review"):
			with self.subTest(codename=codename):
				self.client.force_login(grant_perms(make_user(f"{codename}-only"), codename))
				for url in self._urls():
					self.assertEqual(self.client.get(url).status_code, 302)

	def test_timelapse_reviewer_cannot_reach_the_project_queues(self):
		ship = make_ship(make_project(make_user("shipper"), shippable=True))
		self.client.force_login(grant_perms(make_user("tlonly"), "timelapse_review"))
		for url in (
			reverse("review_dash"),
			reverse("ysws_review_dash"),
			reverse("fraud_review_dash"),
			reverse("review_project", args=[ship.id]),
		):
			with self.subTest(url=url):
				self.assertEqual(self.client.get(url).status_code, 302)

	def test_timelapse_reviewer_allowed(self):
		self.client.force_login(grant_perms(make_user("tlrev"), "timelapse_review"))
		for url in self._urls():
			with self.subTest(url=url):
				self.assertEqual(self.client.get(url).status_code, 200)

	def test_timelapse_reviewer_can_reach_the_admin_home(self):
		self.client.force_login(grant_perms(make_user("tlrev2"), "timelapse_review"))
		self.assertEqual(self.client.get(reverse("admin_dash")).status_code, 200)

	def test_organizer_allowed(self):
		self.client.force_login(grant_perms(make_user("organizer"), "organizer"))
		for url in self._urls():
			with self.subTest(url=url):
				self.assertEqual(self.client.get(url).status_code, 200)

	def test_decision_rejects_get(self):
		self.client.force_login(grant_perms(make_user("tlrev3"), "timelapse_review"))
		response = self.client.get(reverse("timelapse_decision", args=[self.project.id]))
		self.assertEqual(response.status_code, 405)

	def test_decision_needs_the_permission(self):
		self.client.force_login(grant_perms(make_user("t1only"), "t1_review"))
		self.client.post(reverse("timelapse_decision", args=[self.project.id]), {})
		self.assertFalse(TimelapseReview.objects.exists())


class TimelapseReviewQueueTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("tlrev"), "timelapse_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)

	def test_a_project_enters_the_queue_once_carrying_all_its_lapses(self):
		first = make_journal(self.project)
		second = make_journal(self.project)
		response = self.client.get(reverse("timelapse_review_dash"))

		self.assertEqual([p.id for p in response.context["projects"]], [self.project.id])
		self.assertEqual(
			[lapse.id for lapse in response.context["projects"][0].lapses],
			[first.id, second.id],
		)

	def test_a_project_stays_queued_while_any_lapse_is_unreviewed(self):
		reviewed = make_journal(self.project)
		pending = make_journal(self.project)
		approve_timelapse(reviewed, reviewer=self.reviewer)

		response = self.client.get(reverse("timelapse_review_dash"))
		self.assertEqual([p.id for p in response.context["projects"]], [self.project.id])
		self.assertEqual(
			[lapse.id for lapse in response.context["projects"][0].lapses], [pending.id]
		)
		self.assertEqual(list(response.context["reviewed"]), [reviewed])

	def test_a_fully_reviewed_project_leaves_the_queue(self):
		approve_timelapse(make_journal(self.project), reviewer=self.reviewer)
		response = self.client.get(reverse("timelapse_review_dash"))
		self.assertEqual(list(response.context["projects"]), [])

	def test_deleted_projects_are_not_queued(self):
		make_journal(make_project(make_user("gone"), deleted=True))
		response = self.client.get(reverse("timelapse_review_dash"))
		self.assertEqual(list(response.context["projects"]), [])


class LockedReadTests(BaseTestCase):
	"""The sign-off re-reads its lapses under FOR UPDATE. That read has to be lockable.

	Postgres refuses to lock the nullable side of an outer join, so filtering on
	`timelapse_review__isnull=True` — which joins — blew up every real pass with
	"FOR UPDATE cannot be applied to the nullable side of an outer join". Sqlite
	discards FOR UPDATE entirely, so no amount of exercising the view catches it
	here; the shape of the compiled query is what the suite can actually check.
	"""

	def setUp(self):
		super().setUp()
		self.project = make_project(make_user("author"), shippable=True)
		self.journal = make_journal(self.project, time_spent=60)

	def test_the_locked_read_joins_nothing(self):
		sql = str(_locked_pending_lapses(self.project).query).upper()
		self.assertNotIn("JOIN", sql)

	def test_the_locked_read_finds_the_unreviewed_lapses(self):
		lapse = make_journal(self.project, time_spent=30)
		approve_timelapse(lapse)
		waiting = make_journal(self.project, time_spent=30)
		self.assertEqual(
			{j.id for j in _locked_pending_lapses(self.project)},
			{self.journal.id, waiting.id},
		)


class TimelapseDecisionTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("tlrev"), "timelapse_review")
		self.client.force_login(self.reviewer)
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.journal = make_journal(self.project, time_spent=60)
		self.session = self.journal.timelapses.get()

	def _decide(self, project=None, ranges=(), **extra):
		"""POST a pass. `ranges` is (session, start, end, reason) tuples."""
		project = project or self.project
		data = {
			"internal_notes": "reviewed",
			"removal_session": [str(getattr(r[0], "id", r[0])) for r in ranges],
			"removal_start": [r[1] for r in ranges],
			"removal_end": [r[2] for r in ranges],
			"removal_reason": [r[3] for r in ranges],
		}
		data.update(extra)
		return self.client.post(reverse("timelapse_decision", args=[project.id]), data)

	def test_approve_with_no_removals(self):
		self._decide(internal_notes="looks real")

		review = TimelapseReview.objects.get()
		self.assertEqual(review.journal, self.journal)
		self.assertEqual(review.reviewer, self.reviewer)
		self.assertEqual(review.internal_notes, "looks real")
		self.assertFalse(review.removals.exists())
		self.assertEqual(self.journal.approved_seconds, self.journal.tracked_seconds)

	def test_removing_a_range_reduces_approved_time(self):
		# 0:05-0:30 on the player is 25 minutes of the hour it was stitched from.
		self._decide(ranges=[(self.session, "0:05", "0:30", "idle, no model changes")])

		removal = TimelapseRemoval.objects.get()
		self.assertEqual(removal.start_seconds, 300)
		self.assertEqual(removal.end_seconds, 1800)
		self.assertEqual(removal.duration_seconds, 1500)
		self.assertEqual(self.journal.tracked_seconds, 3600)
		self.assertEqual(self.journal.removed_seconds, 1500)
		self.assertEqual(self.journal.approved_seconds, 2100)
		self.assertEqual(self.journal.approved_display, "0h 35m")

	def test_a_range_costs_the_time_it_covers_in_the_video(self):
		"""15 seconds of footage is 15 minutes of somebody's afternoon."""
		journal = make_journal(self.project, time_spent=120)
		session = journal.timelapses.get()
		self.assertEqual(session.video_seconds, 120)

		self._decide(ranges=[(session, "0:56", "1:11", "afk")])

		removal = TimelapseRemoval.objects.get()
		self.assertEqual(removal.duration_seconds, 15 * 60)
		self.assertEqual(removal.duration_display, "15:00")
		self.assertEqual(journal.removed_seconds, 15 * 60)
		self.assertEqual(journal.approved_seconds, (120 - 15) * 60)

	def test_multiple_ranges_across_multiple_lookouts(self):
		second = make_timelapse(self.project, journal=self.journal, minutes=30)
		self._decide(ranges=[
			(self.session, "0:05", "0:30", "afk"),
			(self.session, "0:40", "0:42", "watching a video"),
			(second, "0:01", "0:02", "unrelated tab"),
		])

		self.assertEqual(TimelapseRemoval.objects.count(), 3)
		self.assertEqual(self.journal.removed_seconds, (25 + 2 + 1) * 60)
		self.assertEqual(self.journal.approved_seconds, 5400 - 28 * 60)
		self.assertEqual(self.session.removed_seconds, 27 * 60)
		self.assertEqual(second.removed_seconds, 60)

	def test_each_removed_range_keeps_its_own_reason(self):
		self._decide(ranges=[
			(self.session, "0:05", "0:30", "afk"),
			(self.session, "0:40", "0:42", "watching a video"),
		])
		self.assertEqual(
			[r.reason for r in TimelapseRemoval.objects.order_by("start_seconds")],
			["afk", "watching a video"],
		)

	def test_audit_history_survives_the_decision(self):
		self._decide(ranges=[(self.session, "0:05", "0:30", "afk")])

		review = TimelapseReview.objects.get()
		removal = review.removals.get()
		self.assertEqual(review.reviewer, self.reviewer)
		self.assertIsNotNone(review.reviewed_at)
		# Stored as the tracked time it cost, shown back as the range on the
		# player the reviewer typed.
		self.assertEqual(removal.range_display, "5:00-30:00")
		self.assertEqual(removal.video_range_display, "0:05-0:30")
		self.assertEqual(removal.reason, "afk")

		log = AuditLog.objects.get(action="timelapse_review")
		self.assertEqual(log.actor, self.reviewer)
		self.assertEqual(log.metadata["project_id"], self.project.id)
		self.assertEqual(log.metadata["journal_ids"], [self.journal.id])
		self.assertEqual(log.metadata["removed_seconds"], 1500)
		self.assertEqual(log.metadata["removals"][0]["reason"], "afk")

	def test_blank_rows_are_ignored(self):
		self.client.post(reverse("timelapse_decision", args=[self.project.id]), {
			"internal_notes": "reviewed",
			"removal_session": [str(self.session.id)],
			"removal_start": [""],
			"removal_end": [""],
			"removal_reason": [""],
		})
		self.assertTrue(TimelapseReview.objects.exists())
		self.assertFalse(TimelapseRemoval.objects.exists())

	def test_one_pass_signs_off_every_waiting_lapse_on_the_project(self):
		second = make_journal(self.project, time_spent=30)
		self._decide(internal_notes="watched both")

		self.assertEqual(TimelapseReview.objects.count(), 2)
		self.assertEqual(
			sorted(TimelapseReview.objects.values_list("journal_id", flat=True)),
			sorted([self.journal.id, second.id]),
		)
		# One pass, one justification, recorded against each lapse it covered.
		self.assertEqual(
			{r.internal_notes for r in TimelapseReview.objects.all()}, {"watched both"}
		)

	def test_a_lapse_is_only_reviewed_once(self):
		self._decide()
		response = self._decide(ranges=[(self.session, "0:05", "0:30", "afk")])

		self.assertEqual(TimelapseReview.objects.count(), 1)
		self.assertFalse(TimelapseRemoval.objects.exists())
		self.assertIn(
			"already been reviewed", " ".join(message_texts(response))
		)

	def test_a_lapse_added_after_the_pass_queues_the_project_again(self):
		self._decide()
		later = make_journal(self.project, time_spent=45)

		response = self.client.get(reverse("timelapse_review_dash"))
		self.assertEqual([p.id for p in response.context["projects"]], [self.project.id])
		self.assertEqual(
			[lapse.id for lapse in response.context["projects"][0].lapses], [later.id]
		)

	def test_justification_is_required(self):
		response = self._decide(internal_notes="")
		self.assertIn("needs a justification", " ".join(message_texts(response)))
		self.assertFalse(TimelapseReview.objects.exists())

	def test_nothing_is_sent_to_the_shipper(self):
		with patch("atlantis_site.views.helpers.send_slack_dm") as dm:
			self._decide(ranges=[(self.session, "0:05", "0:30", "afk")])
		dm.assert_not_called()
		for mock in self.slack_dm_mocks.values():
			mock.assert_not_called()


class TimelapseRemovalValidationTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("tlrev"), "timelapse_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)
		self.journal = make_journal(self.project, time_spent=60)
		self.session = self.journal.timelapses.get()

	def _post(self, ranges, project=None):
		project = project or self.project
		return self.client.post(reverse("timelapse_decision", args=[project.id]), {
			"internal_notes": "reviewed",
			"removal_session": [str(getattr(r[0], "id", r[0])) for r in ranges],
			"removal_start": [r[1] for r in ranges],
			"removal_end": [r[2] for r in ranges],
			"removal_reason": [r[3] for r in ranges],
		})

	def _assert_rejected(self, response, fragment):
		self.assertIn(fragment, " ".join(message_texts(response)))
		self.assertFalse(TimelapseReview.objects.exists())
		self.assertFalse(TimelapseRemoval.objects.exists())

	def test_reason_is_required_for_every_range(self):
		response = self._post([
			(self.session, "0:05", "0:30", "afk"),
			(self.session, "0:40", "0:42", "   "),
		])
		self._assert_rejected(response, "needs a justification")

	def test_overlapping_ranges_rejected(self):
		response = self._post([
			(self.session, "0:00", "0:10", "afk"),
			(self.session, "0:05", "0:12", "still afk"),
		])
		self._assert_rejected(response, "overlaps another removed range")

	def test_overlap_is_reported_in_the_typed_timecodes(self):
		response = self._post([
			(self.session, "0:00", "0:10", "afk"),
			(self.session, "0:05", "0:12", "still afk"),
		])
		self.assertIn("0:05-0:12 overlaps", " ".join(message_texts(response)))

	def test_ranges_touching_at_the_edge_are_allowed(self):
		self._post([
			(self.session, "0:00", "0:10", "afk"),
			(self.session, "0:10", "0:12", "still afk"),
		])
		self.assertEqual(TimelapseRemoval.objects.count(), 2)

	def test_same_range_on_different_lookouts_is_not_an_overlap(self):
		second = make_timelapse(self.project, journal=self.journal, minutes=30)
		self._post([
			(self.session, "0:00", "0:10", "afk"),
			(second, "0:00", "0:10", "afk"),
		])
		self.assertEqual(TimelapseRemoval.objects.count(), 2)

	def test_range_past_the_end_of_the_video_rejected(self):
		"""The guard against a negative adjusted duration.

		An hour of tracking compiles to a minute of video, so 1:30 on the
		player is footage this Lookout doesn't have.
		"""
		response = self._post([(self.session, "0:00", "1:30", "everything")])
		self._assert_rejected(response, "runs past the end of that Lookout's video")

	def test_whole_lookout_may_be_removed(self):
		self._post([(self.session, "0:00", "1:00", "screen recording of someone else")])
		self.assertEqual(self.journal.approved_seconds, 0)

	def test_the_last_second_of_video_cannot_remove_untracked_time(self):
		"""A session's tracked time is whole minutes minus its first bucket.

		9 minutes of tracking still fills 9 seconds of video, so cutting all
		of it must clamp to the 9 minutes rather than claim 10.
		"""
		journal = make_journal(self.project, time_spent=0)
		session = make_timelapse(self.project, journal=journal, minutes=0)
		session.tracked_seconds = 9 * 60
		session.save(update_fields=["tracked_seconds"])

		self.assertEqual(session.video_seconds, 9)
		self._post([(session, "0:00", "0:09", "nothing on screen")])

		removal = TimelapseRemoval.objects.get()
		self.assertEqual(removal.end_seconds, 9 * 60)
		self.assertEqual(journal.removed_seconds, 9 * 60)
		self.assertEqual(journal.approved_seconds, 0)

	def test_backwards_range_rejected(self):
		response = self._post([(self.session, "30:00", "5:00", "afk")])
		self._assert_rejected(response, "has to end after it starts")

	def test_empty_range_rejected(self):
		response = self._post([(self.session, "5:00", "5:00", "afk")])
		self._assert_rejected(response, "has to end after it starts")

	def test_unreadable_timecode_rejected(self):
		response = self._post([(self.session, "five minutes", "0:30", "afk")])
		self._assert_rejected(response, "couldn't read that range")

	def test_a_lookout_on_a_sibling_lapse_is_part_of_the_same_pass(self):
		"""The pass covers the project, so its other waiting lapses are in scope."""
		other = make_journal(self.project, time_spent=60)
		self._post([(other.timelapses.get(), "0:05", "0:30", "afk")])
		self.assertEqual(TimelapseRemoval.objects.get().review.journal, other)

	def test_lookout_from_another_project_rejected(self):
		elsewhere = make_journal(make_project(make_user("stranger")), time_spent=60)
		response = self._post([(elsewhere.timelapses.get(), "0:05", "0:30", "afk")])
		self._assert_rejected(response, "isn't on a Lookout attached to this project")

	def test_mismatched_row_lengths_rejected(self):
		response = self.client.post(reverse("timelapse_decision", args=[self.project.id]), {
			"internal_notes": "reviewed",
			"removal_session": [str(self.session.id), str(self.session.id)],
			"removal_start": ["0:05"],
			"removal_end": ["0:30"],
			"removal_reason": ["afk"],
		})
		self._assert_rejected(response, "didn't come through cleanly")

	def test_over_long_reason_rejected(self):
		response = self._post([(self.session, "0:05", "0:30", "x" * 1001)])
		self._assert_rejected(response, "justification is too long")

	def test_over_long_internal_notes_rejected(self):
		response = self.client.post(
			reverse("timelapse_decision", args=[self.project.id]),
			{"internal_notes": "x" * 1001},
		)
		self._assert_rejected(response, "Internal notes too long")

	def test_database_refuses_a_backwards_range(self):
		review = approve_timelapse(self.journal, reviewer=self.reviewer)
		with self.assertRaises(IntegrityError), transaction.atomic():
			TimelapseRemoval.objects.create(
				review=review, session=self.session,
				start_seconds=60, end_seconds=30, reason="afk",
			)

	def test_database_refuses_an_unjustified_range(self):
		review = approve_timelapse(self.journal, reviewer=self.reviewer)
		with self.assertRaises(IntegrityError), transaction.atomic():
			TimelapseRemoval.objects.create(
				review=review, session=self.session,
				start_seconds=0, end_seconds=30, reason="",
			)


class RegularQueueGatingTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.t1 = grant_perms(make_user("t1rev"), "t1_review")
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)

	def test_ship_is_held_out_of_t1_until_its_timelapses_clear(self):
		ship = make_ship(self.project, timelapse_approved=False)
		self.client.force_login(self.t1)

		self.assertEqual(list(self.client.get(reverse("review_dash")).context["ships"]), [])

		for journal in ship.journals.all():
			approve_timelapse(journal)

		self.assertEqual(list(self.client.get(reverse("review_dash")).context["ships"]), [ship])

	def test_one_unreviewed_journal_holds_the_whole_ship(self):
		ship = make_ship(self.project, timelapse_approved=False)
		approve_timelapse(ship.journals.first())
		self.client.force_login(self.t1)
		self.assertEqual(list(self.client.get(reverse("review_dash")).context["ships"]), [])

	def test_review_project_page_refuses_a_held_ship(self):
		ship = make_ship(self.project, timelapse_approved=False)
		self.client.force_login(self.t1)

		response = self.client.get(reverse("review_project", args=[ship.id]), follow=True)
		self.assertIn("timelapses haven't finished internal review", " ".join(message_texts(response)))

	def test_t1_decision_refuses_a_held_ship(self):
		ship = make_ship(self.project, timelapse_approved=False)
		self.client.force_login(self.t1)

		self.client.post(reverse("t1_decision", args=[ship.id]), {
			"feedback": "nice", "internal_notes": "ok", "approved": "approved",
		})

		ship.refresh_from_db()
		self.assertEqual(ship.status, Ship.ShipStatus.T1_QUEUE)
		self.assertFalse(ship.t1_reviews.exists())

	def test_ship_with_no_journals_is_not_held(self):
		"""The DEBUG-only ship bypass makes these; there's no footage to review."""
		ship = Ship.objects.create(project=self.project, status=Ship.ShipStatus.T1_QUEUE)
		self.client.force_login(self.t1)
		self.assertEqual(list(self.client.get(reverse("review_dash")).context["ships"]), [ship])

	def test_t1_decision_goes_through_once_cleared(self):
		ship = make_ship(self.project)
		self.client.force_login(self.t1)

		self.client.post(reverse("t1_decision", args=[ship.id]), {
			"feedback": "nice", "internal_notes": "ok", "approved": "approved",
		})

		ship.refresh_from_db()
		self.assertEqual(ship.status, Ship.ShipStatus.T2_QUEUE)


class ShippingIsUnaffectedTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.client.force_login(self.author)

	def test_can_ship_while_timelapse_review_is_pending(self):
		make_journal(self.project, time_spent=200)

		self.client.post(reverse("ship_project", args=[self.project.id]))

		self.assertEqual(self.project.ships.count(), 1)
		self.assertEqual(self.project.ships.get().status, Ship.ShipStatus.T1_QUEUE)

	def test_removed_time_does_not_move_the_ship_gate(self):
		"""Ship gates run on tracked time. A removal the shipper can't see must
		not silently take their ability to ship away."""
		journal = make_journal(self.project, time_spent=200)
		approve_timelapse(journal, removals=[(journal.timelapses.get(), 0, 3600, "afk")])

		self.client.post(reverse("ship_project", args=[self.project.id]))

		self.assertEqual(self.project.ships.count(), 1)


class InternalOnlyTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.journal = make_journal(self.project, time_spent=60, title="My lapse")
		approve_timelapse(
			self.journal,
			removals=[(self.journal.timelapses.get(), 300, 1800, "clearly afk here")],
		)

	def test_owner_sees_tracked_time_and_no_reviewer_reasons(self):
		self.client.force_login(self.author)
		response = self.client.get(reverse("project_detail", args=[self.project.id]))
		body = response.content.decode()

		self.assertEqual(response.context["time_spent"], "1h 0m")
		self.assertNotIn("clearly afk here", body)
		self.assertNotIn("timelapse review", body.lower())

	def test_a_visitors_copy_shows_nothing_either(self):
		self.client.force_login(make_user("onlooker"))
		response = self.client.get(reverse("project_detail", args=[self.project.id]))
		body = response.content.decode()

		self.assertEqual(response.context["time_spent"], "1h 0m")
		self.assertNotIn("clearly afk here", body)

	def test_reviewers_see_the_adjusted_figure(self):
		ship = Ship.objects.create(project=self.project, status=Ship.ShipStatus.T1_QUEUE)
		Journal.objects.filter(id=self.journal.id).update(ship=ship)
		self.client.force_login(grant_perms(make_user("t1rev"), "t1_review"))

		response = self.client.get(reverse("review_dash"))
		self.assertEqual(response.context["ships"][0].time_spent_display, "0h 35m")


class ApprovedTimeInDownstreamReviewTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(
			self.project, status=Ship.ShipStatus.T2_QUEUE, journal_minutes=(120,)
		)
		self.journal = self.ship.journals.get()

	def _remove(self, seconds):
		review = self.journal.timelapse_review
		TimelapseRemoval.objects.create(
			review=review,
			session=self.journal.timelapses.get(),
			start_seconds=0,
			end_seconds=seconds,
			reason="afk",
		)

	def test_t2_deduction_is_measured_against_approved_time(self):
		self._remove(60 * 60)  # 120 tracked minutes -> 60 approved
		self.client.force_login(grant_perms(make_user("t2rev"), "t2_review"))

		response = self.client.post(reverse("t2_decision", args=[self.ship.id]), {
			"decision": "A", "deductions": "90", "feedback": "hm", "justification": "x",
		}, follow=True)

		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T2_QUEUE)
		self.assertIn("Deduction too large", " ".join(message_texts(response)))

	def test_t3_payout_page_shows_approved_time(self):
		self._remove(30 * 60)
		self.ship.status = Ship.ShipStatus.T3_QUEUE
		self.ship.save()
		self.client.force_login(grant_perms(make_user("t3rev"), "t3_review"))

		response = self.client.get(reverse("fraud_review_project", args=[self.ship.id]))
		self.assertEqual(response.context["logged_time"], 90)
