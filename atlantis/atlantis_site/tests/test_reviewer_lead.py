from django.test import override_settings
from django.urls import reverse

from ..models import AuditLog, InternalComment, Ship, T1, T2, T3
from .base import (
	BaseTestCase,
	grant_perms,
	make_project,
	make_ship,
	make_user,
	message_texts,
)


def decided(ship, reviewer, approved=True, changes_requested=False, **kwargs):
	"""A ship as a T1 decision leaves it."""
	if approved:
		ship.status = Ship.ShipStatus.T2_QUEUE
	elif changes_requested:
		ship.status = Ship.ShipStatus.CHANGES_REQUESTED
	else:
		ship.status = Ship.ShipStatus.REJECTED
	ship.save()
	return T1.objects.create(
		ship=ship, reviewer=reviewer, approved=approved, changes_requested=changes_requested,
		feedback=kwargs.get("feedback", "looks good"),
		internal_notes=kwargs.get("internal_notes", "checked it all"),
	)


class T1RollbackTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.lead = grant_perms(make_user("lead", slack_id="U0LEAD"), "reviewer_lead")
		self.reviewer = grant_perms(make_user("t1rev", slack_id="U0REV"), "t1_review")
		self.project = make_project(make_user("author", slack_id="U0AUTHOR"), shippable=True)
		self.ship = make_ship(self.project)
		self.t1 = decided(self.ship, self.reviewer)
		self.client.force_login(self.lead)

	def _roll_back(self, t1=None, reason="didn't check the model"):
		t1 = t1 or self.t1
		return self.client.post(reverse("t1_rollback", args=[t1.id]), {"reason": reason})

	def test_approval_rolled_back_to_t1_queue(self):
		self._roll_back()
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T1_QUEUE)
		self.assertFalse(T1.objects.exists())

	def test_rejection_rolled_back_to_t1_queue(self):
		self.t1.delete()
		t1 = decided(self.ship, self.reviewer, approved=False)
		self._roll_back(t1)
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T1_QUEUE)
		self.assertFalse(T1.objects.exists())

	def test_request_for_changes_rolled_back_to_t1_queue(self):
		self.t1.delete()
		t1 = decided(self.ship, self.reviewer, approved=False, changes_requested=True)
		self._roll_back(t1)
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T1_QUEUE)
		self.assertIn("request for changes", InternalComment.objects.get(ship=self.ship).text)

	def test_request_for_changes_cannot_be_rolled_back_once_resubmitted(self):
		self.t1.delete()
		t1 = decided(self.ship, self.reviewer, approved=False, changes_requested=True)
		self.ship.status = Ship.ShipStatus.T1_QUEUE
		self.ship.save()
		response = self._roll_back(t1)
		self.assertTrue(T1.objects.filter(id=t1.id).exists())
		self.assertIn(
			"That T1 review can't be rolled back: the ship is now under t1 review.",
			message_texts(response),
		)

	def test_leaves_an_internal_comment_with_the_reason(self):
		self._roll_back(reason="approved a ship with no Printables page")
		comment = InternalComment.objects.get(ship=self.ship)
		self.assertEqual(comment.author, self.lead)
		self.assertIn("approval", comment.text)
		self.assertIn("approved a ship with no Printables page", comment.text)

	def test_audit_keeps_what_the_review_said(self):
		self._roll_back()
		log = AuditLog.objects.get(action="t1_rollback")
		self.assertEqual(log.actor, self.lead)
		self.assertEqual(log.metadata["t1_id"], self.t1.id)
		self.assertEqual(log.metadata["reviewer"], "t1rev")
		self.assertEqual(log.metadata["feedback"], "looks good")
		self.assertEqual(log.metadata["internal_notes"], "checked it all")
		self.assertEqual(log.metadata["previous_ship_status"], Ship.ShipStatus.T2_QUEUE)

	@override_settings(REVIEW_CHECKPOINT_ID="C0CHECK")
	def test_shipper_told_in_checkpoint_channel(self):
		self._roll_back()
		self.slack_dm_mocks["review"].assert_not_called()
		content, channel = self.slack_message_mocks["review"].call_args.args
		self.assertEqual(channel, "C0CHECK")
		self.assertIn("<@U0AUTHOR>", content)
		self.assertIn("rolled back", content)

	def test_reason_required(self):
		self._roll_back(reason="   ")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T2_QUEUE)
		self.assertTrue(T1.objects.exists())

	def test_reason_length_limited(self):
		self._roll_back(reason="x" * 801)
		self.assertTrue(T1.objects.exists())

	def test_get_not_allowed(self):
		self.assertEqual(self.client.get(reverse("t1_rollback", args=[self.t1.id])).status_code, 405)

	def test_unknown_review_404(self):
		self.assertEqual(
			self.client.post(reverse("t1_rollback", args=[99999]), {"reason": "x"}).status_code, 404
		)

	def test_organizer_can_roll_back(self):
		self.client.force_login(grant_perms(make_user("org"), "organizer"))
		self._roll_back()
		self.assertFalse(T1.objects.exists())

	def test_reviewers_cannot_roll_back(self):
		for perm in ("t1_review", "t2_review", "t3_review"):
			with self.subTest(perm=perm):
				self.client.force_login(grant_perms(make_user(f"only_{perm}"), perm))
				self.assertEqual(self._roll_back().status_code, 302)
				self.assertTrue(T1.objects.filter(id=self.t1.id).exists())

	def test_refused_once_a_later_tier_has_reviewed(self):
		T2.objects.create(ship=self.ship, reviewer=self.reviewer, feedback="", justification="")
		self.ship.status = Ship.ShipStatus.T3_QUEUE
		self.ship.save()
		response = self._roll_back()
		self.assertTrue(T1.objects.exists())
		self.assertTrue(any("can't be rolled back" in m for m in message_texts(response)))

	def test_refused_when_a_later_tier_sent_it_back_to_t2(self):
		T2.objects.create(ship=self.ship, reviewer=self.reviewer, feedback="", justification="")
		T3.objects.create(
			ship=self.ship, reviewer=self.reviewer, decision=T3.Decision.RETURN_T2,
			payout_time=0, airtable_time=0,
		)
		# Back in T2, as the T1 left it — but not because of the T1.
		self._roll_back()
		self.assertTrue(T1.objects.exists())

	def test_refused_when_a_newer_t1_replaced_it(self):
		newer = decided(self.ship, self.reviewer)
		self._roll_back()
		self.assertTrue(T1.objects.filter(id=self.t1.id).exists())
		self._roll_back(newer)
		self.assertFalse(T1.objects.filter(id=newer.id).exists())

	def test_refused_after_a_rejected_project_is_reshipped(self):
		self.t1.delete()
		t1 = decided(self.ship, self.reviewer, approved=False)
		make_ship(self.project)
		self._roll_back(t1)
		self.assertTrue(T1.objects.exists())
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.REJECTED)


class ReviewerLeadDeskTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.lead = grant_perms(make_user("lead"), "reviewer_lead")
		self.reviewer = grant_perms(make_user("t1rev"), "t1_review")
		self.project = make_project(make_user("author"), shippable=True)
		self.ship = make_ship(self.project)
		self.t1 = decided(self.ship, self.reviewer)

	def test_lead_can_read_the_t1_desk_and_ship_page(self):
		self.client.force_login(self.lead)
		self.assertEqual(self.client.get(reverse("review_dash")).status_code, 200)
		self.assertEqual(self.client.get(reverse("review_project", args=[self.ship.id])).status_code, 200)
		self.assertEqual(self.client.get(reverse("admin_dash")).status_code, 200)

	def test_lead_cannot_decide_or_reach_higher_tiers(self):
		self.client.force_login(self.lead)
		pending = make_ship(make_project(make_user("other"), shippable=True))
		self.client.post(reverse("t1_decision", args=[pending.id]), {"approved": "denied"})
		pending.refresh_from_db()
		self.assertEqual(pending.status, Ship.ShipStatus.T1_QUEUE)
		self.assertEqual(self.client.get(reverse("ysws_review_dash")).status_code, 302)
		self.assertEqual(self.client.get(reverse("fraud_review_dash")).status_code, 302)
		self.assertEqual(self.client.get(reverse("audit_log")).status_code, 302)

	def test_desk_offers_rollback_to_a_lead(self):
		self.client.force_login(self.lead)
		response = self.client.get(reverse("review_dash"))
		self.assertContains(response, reverse("t1_rollback", args=[self.t1.id]))

	def test_desk_explains_a_review_that_cannot_be_rolled_back(self):
		self.ship.status = Ship.ShipStatus.T3_QUEUE
		self.ship.save()
		self.client.force_login(self.lead)
		response = self.client.get(reverse("review_dash"))
		self.assertNotContains(response, reverse("t1_rollback", args=[self.t1.id]))
		self.assertContains(response, "Can't: the ship is now under fraud review")

	def test_desk_hides_rollback_from_reviewers(self):
		self.client.force_login(self.reviewer)
		response = self.client.get(reverse("review_dash"))
		self.assertNotContains(response, "data-rollback-url")
		self.assertNotContains(response, "rq-rollback")

	def test_desk_shows_project_cover(self):
		pending = make_ship(make_project(
			make_user("other"), shippable=True, image_url="https://example.com/cover.png",
		))
		self.client.force_login(self.reviewer)
		response = self.client.get(reverse("review_dash"))
		self.assertContains(response, 'src="https://example.com/cover.png"')


class ReviewAuditTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.lead = grant_perms(make_user("lead"), "reviewer_lead")
		self.actor = make_user("someone")
		AuditLog.objects.create(actor=self.actor, action="t1_decision", target="Ship #1 (Boat)", ip_address="10.1.2.3")
		AuditLog.objects.create(actor=self.actor, action="t1_rollback", target="Ship #1 (Boat)")
		AuditLog.objects.create(actor=self.actor, action="view_order_address", target="Order #7")
		AuditLog.objects.create(actor=self.actor, action="edit_user", target="User #3 (x)")

	def test_lead_sees_review_actions_only(self):
		self.client.force_login(self.lead)
		response = self.client.get(reverse("review_audit"))
		self.assertEqual(response.status_code, 200)
		actions = {log.action for log in response.context["logs"]}
		self.assertEqual(actions, {"t1_decision", "t1_rollback"})
		self.assertNotIn("view_order_address", list(response.context["actions"]))
		self.assertNotContains(response, "Order #7")

	def test_ip_addresses_not_shown(self):
		self.client.force_login(self.lead)
		self.assertNotContains(self.client.get(reverse("review_audit")), "10.1.2.3")

	def test_filter_cannot_widen_the_scope(self):
		self.client.force_login(self.lead)
		response = self.client.get(reverse("review_audit"), {"action": "view_order_address"})
		self.assertEqual(list(response.context["logs"]), [])

	def test_reviewers_cannot_audit(self):
		self.client.force_login(grant_perms(make_user("t1rev"), "t1_review"))
		self.assertEqual(self.client.get(reverse("review_audit")).status_code, 302)

	def test_organizer_can_audit_and_full_log_still_works(self):
		self.client.force_login(grant_perms(make_user("org"), "organizer"))
		self.assertEqual(self.client.get(reverse("review_audit")).status_code, 200)
		response = self.client.get(reverse("audit_log"))
		self.assertEqual(response.context["page"].paginator.count, 4)
		self.assertContains(response, "10.1.2.3")
