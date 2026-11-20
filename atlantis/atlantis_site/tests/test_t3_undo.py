"""Undoing a T3 approval: the ship back in the T3 queue, the payout taken back."""

from unittest.mock import patch

from django.urls import reverse

from ..models import AirtableSubmission, AuditLog, InternalComment, Ship, T3
from .base import (
	BaseTestCase, grant_perms, make_project, make_ship, make_user, message_texts, t3_checklist,
)


class T3UndoTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t3rev"), "t3_review")
		self.client.force_login(self.reviewer)
		self.author = make_user("author", slack_id="U0AUTHOR", layers=10)
		self.ship = make_ship(make_project(self.author, shippable=True), status=Ship.ShipStatus.T3_QUEUE)

	def _approve(self):
		self.client.post(reverse("t3_decision", args=[self.ship.id]), {
			"decision": T3.Decision.APPROVE,
			"internal_notes": "clean",
			"payout_time": "120",
			"airtable_time": "120",
			"justification": "Hours match the journals.",
			**t3_checklist(),
		})
		return T3.objects.get(ship=self.ship)

	def _undo(self, t3, reason="Paid the wrong project"):
		return self.client.post(
			reverse("t3_undo", args=[t3.id]), {"reason": reason},
			HTTP_REFERER=reverse("fraud_review_dash"), follow=True,
		)

	def _layers(self):
		self.author.hackclub_profile.refresh_from_db()
		return self.author.hackclub_profile.layers

	def test_undo_requeues_and_takes_the_pearls_back(self):
		t3 = self._approve()
		self.assertEqual(self._layers(), 26)

		response = self._undo(t3)

		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T3_QUEUE)
		self.assertEqual(self._layers(), 10)
		self.assertFalse(T3.objects.exists())
		self.assertIn("Paid the wrong project", InternalComment.objects.get(ship=self.ship).text)
		log = AuditLog.objects.get(action="t3_undo")
		self.assertEqual(log.metadata["payout_layers"], 16)
		self.assertEqual(log.metadata["balance_after"], 10)
		self.assertTrue(any("Undid the T3 approval" in text for text in message_texts(response)))

	def test_approving_again_sends_a_second_record(self):
		with patch("atlantis_site.submissions.build_fields", return_value={}), \
				patch("atlantis_site.submissions.airtable.create_record", side_effect=["recFIRST", "recSECOND"]) as create:
			t3 = self._approve()
			self.assertEqual(AirtableSubmission.objects.get(ship=self.ship).record_id, "recFIRST")

			self._undo(t3)
			# The first record stays in Airtable; only our row of it goes, and
			# the audit entry says which record it was.
			self.assertFalse(AirtableSubmission.objects.filter(ship=self.ship).exists())
			log = AuditLog.objects.get(action="t3_undo")
			self.assertEqual(log.metadata["airtable_submission"]["record_id"], "recFIRST")

			self._approve()
		self.assertEqual(create.call_count, 2)
		self.assertEqual(AirtableSubmission.objects.get(ship=self.ship).record_id, "recSECOND")

	def test_spent_pearls_leave_the_shipper_in_debt(self):
		t3 = self._approve()
		profile = self.author.hackclub_profile
		profile.layers = 0
		profile.save()
		self._undo(t3)
		self.assertEqual(self._layers(), -16)

	def test_reason_is_required(self):
		t3 = self._approve()
		self._undo(t3, reason="  ")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.FINALIZED)
		self.assertEqual(self._layers(), 26)

	def test_returns_cannot_be_undone(self):
		self.client.post(reverse("t3_decision", args=[self.ship.id]), {
			"decision": T3.Decision.RETURN_T2, "payout_time": "0", "airtable_time": "0",
		})
		t3 = T3.objects.get()
		response = self._undo(t3)
		self.assertTrue(T3.objects.exists())
		self.assertTrue(any("only an approval" in text for text in message_texts(response)))

	def test_undoing_twice_does_nothing_more(self):
		t3 = self._approve()
		self._undo(t3)
		response = self.client.post(reverse("t3_undo", args=[t3.id]), {"reason": "again"})
		self.assertEqual(response.status_code, 404)
		self.assertEqual(self._layers(), 10)

	def test_approval_without_a_recorded_breakdown_is_refused(self):
		t3 = self._approve()
		AuditLog.objects.filter(action="t3_decision").delete()
		self._undo(t3)
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.FINALIZED)

	def test_needs_t3_permission(self):
		t3 = self._approve()
		self.client.force_login(grant_perms(make_user("t1rev"), "t1_review"))
		self.client.post(reverse("t3_undo", args=[t3.id]), {"reason": "x"})
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.FINALIZED)

	def test_desk_offers_undo_on_approvals(self):
		t3 = self._approve()
		response = self.client.get(reverse("fraud_review_dash"))
		self.assertContains(response, reverse("t3_undo", args=[t3.id]))
		self.assertContains(response, "Undo a T3 approval")
