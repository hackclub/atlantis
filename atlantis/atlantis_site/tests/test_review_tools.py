"""Flags, desk filters, locking from T1, T2 requests for changes, and the
project page's way onto a project's review."""

from django.test import override_settings
from django.urls import reverse

from ..models import AuditLog, Project, Ship, T2
from .base import (
	BaseTestCase,
	grant_perms,
	make_journal,
	make_project,
	make_ship,
	make_user,
	message_texts,
	ship_checklist,
)


class FlagTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project)

	def _flag(self, reason="looks copied from thingiverse"):
		return self.client.post(
			reverse("flag_project", args=[self.project.id]), {"reason": reason},
		)

	def test_t1_and_t2_reviewers_can_flag(self):
		for name, perm in (("t1rev", "t1_review"), ("t2rev", "t2_review")):
			reviewer = grant_perms(make_user(name, slack_id=f"U-{name}"), perm)
			self.client.force_login(reviewer)
			self._flag(f"flagged by {name}")
			self.project.refresh_from_db()
			self.assertTrue(self.project.flagged)
			self.assertEqual(self.project.flag_reason, f"flagged by {name}")
			self.assertEqual(self.project.flagged_by, reviewer)
			self.assertIsNotNone(self.project.flagged_at)

		self.assertEqual(AuditLog.objects.filter(action="flag_project").count(), 2)

	def test_a_flag_needs_a_reason(self):
		self.client.force_login(grant_perms(make_user("t1rev", slack_id="U-t1"), "t1_review"))
		response = self._flag("   ")
		self.project.refresh_from_db()
		self.assertFalse(self.project.flagged)
		self.assertIn("Say why the project is being flagged.", message_texts(response))

	def test_a_timelapse_reviewer_cannot_flag(self):
		self.client.force_login(grant_perms(make_user("tl", slack_id="U-tl"), "timelapse_review"))
		self._flag()
		self.project.refresh_from_db()
		self.assertFalse(self.project.flagged)

	def test_unflag_clears_it_and_keeps_the_reason_in_the_audit(self):
		reviewer = grant_perms(make_user("t1rev", slack_id="U-t1"), "t1_review")
		self.client.force_login(reviewer)
		self._flag("suspicious hours")
		self.client.post(reverse("unflag_project", args=[self.project.id]))

		self.project.refresh_from_db()
		self.assertFalse(self.project.flagged)
		self.assertEqual(self.project.flag_reason, "")
		self.assertIsNone(self.project.flagged_by)
		entry = AuditLog.objects.get(action="unflag_project")
		self.assertEqual(entry.metadata["reason"], "suspicious hours")

	def test_flagging_does_not_tell_the_owner(self):
		self.client.force_login(grant_perms(make_user("t1rev", slack_id="U-t1"), "t1_review"))
		self._flag()
		self.slack_dm_mocks["review"].assert_not_called()
		self.slack_message_mocks["review"].assert_not_called()

	def test_the_review_page_shows_the_flag_and_its_reason(self):
		self.client.force_login(grant_perms(make_user("t1rev", slack_id="U-t1"), "t1_review"))
		self._flag("model looks like a remix")
		response = self.client.get(reverse("review_project", args=[self.ship.id]))
		self.assertContains(response, "model looks like a remix")
		self.assertContains(response, "Remove flag")


class DeskFilterTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.client.force_login(grant_perms(make_user("t1rev", slack_id="U-t1"), "t1_review"))
		self.plain = make_ship(make_project(make_user("plain", slack_id="U-p"), shippable=True, title="Plain Gear"))
		self.flagged = make_ship(make_project(
			make_user("flaggy", slack_id="U-f"), shippable=True, title="Odd Bracket",
			flagged=True, flag_reason="hmm",
		))

	def _ids(self, query=""):
		response = self.client.get(reverse("review_dash") + query)
		return [ship.id for ship in response.context["ships"]], response

	def test_no_filter_shows_the_whole_queue(self):
		ids, _ = self._ids()
		self.assertEqual(ids, [self.plain.id, self.flagged.id])

	def test_the_flagged_filter(self):
		ids, response = self._ids("?filter=flagged")
		self.assertEqual(ids, [self.flagged.id])
		self.assertEqual(response.context["pending_count"], 1)
		# Still numbered by its place in the whole queue.
		self.assertEqual(response.context["ships"][0].queue_index, 2)
		counts = {option["key"]: option["count"] for option in response.context["filter_options"]}
		self.assertEqual(counts[""], 2)
		self.assertEqual(counts["flagged"], 1)

	def test_search_matches_title_and_owner(self):
		self.assertEqual(self._ids("?q=bracket")[0], [self.flagged.id])
		self.assertEqual(self._ids("?q=PLAIN")[0], [self.plain.id])
		self.assertEqual(self._ids("?q=nothing-like-this")[0], [])

	def test_an_unknown_filter_is_ignored(self):
		ids, response = self._ids("?filter=bogus")
		self.assertEqual(len(ids), 2)
		self.assertEqual(response.context["active_filter"], "")

	def test_the_reship_filter(self):
		project = self.plain.project
		self.plain.status = Ship.ShipStatus.REJECTED
		self.plain.save()
		again = make_ship(project)
		self.assertEqual(self._ids("?filter=reship")[0], [again.id])

	def test_the_t2_and_t3_desks_filter_too(self):
		for status, perm, dash in (
			(Ship.ShipStatus.T2_QUEUE, "t2_review", "ysws_review_dash"),
			(Ship.ShipStatus.T3_QUEUE, "t3_review", "fraud_review_dash"),
		):
			Ship.objects.update(status=status)
			self.client.force_login(grant_perms(make_user(perm, slack_id=f"U-{perm}"), perm))
			response = self.client.get(reverse(dash) + "?filter=flagged")
			self.assertEqual([ship.id for ship in response.context["ships"]], [self.flagged.id])

	def test_the_timelapse_desk_filters_on_flags(self):
		self.client.force_login(grant_perms(make_user("tl", slack_id="U-tl"), "timelapse_review"))
		flagged = make_project(make_user("lapser", slack_id="U-l"), flagged=True, flag_reason="x")
		make_journal(flagged)
		make_journal(make_project(make_user("other", slack_id="U-o")))
		response = self.client.get(reverse("timelapse_review_dash") + "?filter=flagged")
		self.assertEqual([p.id for p in response.context["projects"]], [flagged.id])


class LockFromT1Tests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.ship = make_ship(make_project(make_user("author", slack_id="U-a"), shippable=True))

	def _page(self, *perms):
		self.client.force_login(grant_perms(make_user("rev-" + "-".join(perms), slack_id="U-r"), *perms))
		return self.client.get(reverse("review_project", args=[self.ship.id]))

	def test_a_t1_only_reviewer_gets_no_lock_button(self):
		self.assertNotContains(self._page("t1_review"), "Lock project")

	def test_a_t1_reviewer_with_t2_gets_one(self):
		self.assertContains(self._page("t1_review", "t2_review"), "Lock project")

	def test_a_t1_only_reviewer_cannot_lock_by_posting(self):
		self.client.force_login(grant_perms(make_user("t1rev", slack_id="U-t1"), "t1_review"))
		self.client.post(reverse("lock_project", args=[self.ship.project.id]))
		self.ship.project.refresh_from_db()
		self.assertFalse(self.ship.project.locked)


class T2ChangesRequestedTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t2rev"), "t2_review")
		self.client.force_login(self.reviewer)
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)

	def _decide(self, **overrides):
		data = {"decision": T2.Decision.CHANGES, "deductions": "0", "feedback": "fix the hinge", "justification": ""}
		data.update(overrides)
		return self.client.post(reverse("t2_decision", args=[self.ship.id]), data)

	def test_sends_the_ship_back_to_the_shipper(self):
		self._decide()
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.CHANGES_REQUESTED)
		self.assertEqual(T2.objects.get().decision, T2.Decision.CHANGES)

	def test_needs_feedback(self):
		response = self._decide(feedback="")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T2_QUEUE)
		self.assertEqual(T2.objects.count(), 0)
		self.assertIn(
			"Say what needs changing in the feedback before requesting changes.",
			message_texts(response),
		)

	@override_settings(REVIEW_CHECKPOINT_ID="C0CHECK")
	def test_pings_the_checkpoint_channel_as_a_request_for_changes(self):
		self._decide()
		content, channel = self.slack_message_mocks["review"].call_args.args
		self.assertEqual(channel, "C0CHECK")
		self.assertIn("requested changes", content)
		self.assertIn("during T2 review", content)
		self.assertIn("fix the hinge", content)

	def test_the_resubmission_goes_back_to_t1(self):
		self._decide()
		self.client.force_login(self.author)
		self.client.post(reverse("ship_project", args=[self.project.id]), ship_checklist())
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T1_QUEUE)
		self.assertEqual(Ship.objects.count(), 1)

	def test_the_owner_sees_the_t2_feedback(self):
		self._decide(feedback="the hinge needs a gap")
		self.client.force_login(self.author)
		response = self.client.get(reverse("project_detail", args=[self.project.id]))
		self.assertContains(response, "the hinge needs a gap")


class ProjectPageReviewLinkTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.author = make_user("author", slack_id="U-a")
		self.project = make_project(self.author, shippable=True)

	def _page(self, user):
		self.client.force_login(user)
		return self.client.get(reverse("project_detail", args=[self.project.id]))

	def test_a_t1_reviewer_gets_a_link_to_the_t1_review(self):
		ship = make_ship(self.project)
		response = self._page(grant_perms(make_user("t1rev", slack_id="U-t1"), "t1_review"))
		self.assertContains(response, reverse("review_project", args=[ship.id]))

	def test_a_t2_ship_links_t2_reviewers_but_not_t1_ones(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)
		url = reverse("ysws_review_project", args=[ship.id])
		self.assertContains(self._page(grant_perms(make_user("t2rev", slack_id="U-t2"), "t2_review")), url)
		self.assertNotContains(self._page(grant_perms(make_user("t1rev", slack_id="U-t1"), "t1_review")), url)

	def test_a_project_with_waiting_lapses_links_timelapse_reviewers(self):
		make_journal(self.project)
		response = self._page(grant_perms(make_user("tl", slack_id="U-tl"), "timelapse_review"))
		self.assertContains(response, reverse("timelapse_review_project", args=[self.project.id]))

	def test_no_link_once_the_ship_has_left_the_queue(self):
		make_ship(self.project, status=Ship.ShipStatus.FINALIZED)
		response = self._page(grant_perms(make_user("org", slack_id="U-o"), "organizer"))
		self.assertEqual(response.context["review_links"], [])

	def test_a_ship_still_waiting_on_its_lapses_is_not_linked_for_t1(self):
		make_ship(self.project, timelapse_approved=False)
		response = self._page(grant_perms(make_user("t1rev", slack_id="U-t1"), "t1_review"))
		self.assertEqual(response.context["review_links"], [])

	def test_ordinary_visitors_and_the_owner_get_nothing(self):
		make_ship(self.project)
		self.assertEqual(self._page(make_user("visitor", slack_id="U-v")).context["review_links"], [])
		self.assertEqual(self._page(self.author).context["review_links"], [])


class FinalReviewWordingTests(BaseTestCase):
	def test_the_project_page_calls_t3_final_review(self):
		author = make_user("author", slack_id="U-a")
		project = make_project(author, shippable=True)
		make_ship(project, status=Ship.ShipStatus.T3_QUEUE)
		self.client.force_login(author)
		response = self.client.get(reverse("project_detail", args=[project.id]))
		self.assertContains(response, "under final review")
		self.assertNotContains(response, "fraud review")
