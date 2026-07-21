from django.urls import reverse

from ..models import AuditLog, Ship, T1, T2, T3
from .base import (
	BaseTestCase,
	grant_perms,
	make_journal,
	make_project,
	make_ship,
	make_user,
	message_texts,
)


class ReviewAccessControlTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.ship = make_ship(make_project(make_user("author"), shippable=True))

	def _urls(self):
		sid = self.ship.id
		return [
			reverse("review_dash"),
			reverse("review_project", args=[sid]),
			reverse("ysws_review_dash"),
			reverse("ysws_review_project", args=[sid]),
			reverse("fraud_review_dash"),
			reverse("fraud_review_project", args=[sid]),
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

	def test_t1_reviewer_cannot_access_higher_tiers(self):
		t1_reviewer = grant_perms(make_user("t1only"), "t1_review")
		self.client.force_login(t1_reviewer)
		self.assertEqual(self.client.get(reverse("review_dash")).status_code, 200)
		self.assertEqual(self.client.get(reverse("ysws_review_dash")).status_code, 302)
		self.assertEqual(self.client.get(reverse("fraud_review_dash")).status_code, 302)

	def test_t2_reviewer_cannot_access_fraud_review(self):
		t2_reviewer = grant_perms(make_user("t2only"), "t2_review")
		self.client.force_login(t2_reviewer)
		self.assertEqual(self.client.get(reverse("ysws_review_dash")).status_code, 200)
		self.assertEqual(self.client.get(reverse("fraud_review_dash")).status_code, 302)

	def test_organizer_can_access_everything(self):
		organizer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(organizer)
		for url in self._urls():
			with self.subTest(url=url):
				self.assertEqual(self.client.get(url).status_code, 200)


class ReviewDashTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t1rev"), "t1_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)

	def test_lists_only_t1_queue_ships(self):
		t1_ship = make_ship(self.project, status=Ship.ShipStatus.T1_QUEUE)
		make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)
		make_ship(self.project, status=Ship.ShipStatus.FINALIZED)

		response = self.client.get(reverse("review_dash"))
		self.assertEqual(list(response.context["ships"]), [t1_ship])

	def test_time_spent_display_annotated(self):
		make_ship(self.project, status=Ship.ShipStatus.T1_QUEUE, journal_minutes=(90, 40))
		response = self.client.get(reverse("review_dash"))
		self.assertEqual(response.context["ships"][0].time_spent_display, "2h 10m")


class T1DecisionTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t1rev"), "t1_review")
		self.client.force_login(self.reviewer)
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project)

	def _decide(self, ship=None, **overrides):
		data = {"feedback": "nice", "internal_notes": "ok", "approved": "approved"}
		data.update(overrides)
		data = {k: v for k, v in data.items() if v is not None}
		ship = ship or self.ship
		return self.client.post(reverse("t1_decision", args=[ship.id]), data)

	def test_get_not_allowed(self):
		self.assertEqual(
			self.client.get(reverse("t1_decision", args=[self.ship.id])).status_code, 405
		)

	def test_approve_with_print_goes_to_print_queue(self):
		self._decide(print="on")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.PRINT_QUEUE)

		t1 = T1.objects.get()
		self.assertTrue(t1.approved)
		self.assertTrue(t1.print)
		self.assertEqual(t1.reviewer, self.reviewer)
		self.assertEqual(t1.feedback, "nice")

	def test_approve_without_print_goes_to_t2(self):
		self._decide()
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T2_QUEUE)
		self.assertFalse(T1.objects.get().print)

	def test_deny_rejects_ship(self):
		self._decide(approved="denied")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.REJECTED)
		self.assertFalse(T1.objects.get().approved)

	def test_invalid_approved_value_rejected(self):
		self._decide(approved="maybe")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T1_QUEUE)
		self.assertEqual(T1.objects.count(), 0)

	def test_missing_approved_value_rejected(self):
		self._decide(approved=None)
		self.assertEqual(T1.objects.count(), 0)

	def test_feedback_and_notes_length_limits(self):
		for overrides in ({"feedback": "x" * 101}, {"internal_notes": "x" * 101}):
			with self.subTest(**overrides):
				self._decide(**overrides)
				self.assertEqual(T1.objects.count(), 0)

	def test_ship_must_be_in_t1_queue(self):
		for status in (Ship.ShipStatus.T2_QUEUE, Ship.ShipStatus.FINALIZED,
					   Ship.ShipStatus.REJECTED, Ship.ShipStatus.PRINT_QUEUE):
			with self.subTest(status=status):
				ship = make_ship(self.project, status=status, journal_minutes=())
				response = self._decide(ship=ship)
				ship.refresh_from_db()
				self.assertEqual(ship.status, status)
				self.assertIn("ship not in T1 queue", message_texts(response))

	def test_slack_dm_sent_to_owner(self):
		self._decide()
		self.slack_dm_mocks["review"].assert_called_once()
		self.assertEqual(self.slack_dm_mocks["review"].call_args.args[1], "U0AUTHOR")

	def test_no_slack_dm_when_owner_has_no_slack_id(self):
		profile = self.author.hackclub_profile
		profile.slack_id = ""
		profile.save()
		self._decide()
		self.slack_dm_mocks["review"].assert_not_called()

	def test_audit_log_recorded(self):
		self._decide()
		log = AuditLog.objects.get(action="t1_decision")
		self.assertEqual(log.actor, self.reviewer)
		self.assertEqual(log.metadata["ship_id"], self.ship.id)
		self.assertTrue(log.metadata["approved"])

	def test_unknown_ship_404(self):
		response = self.client.post(
			reverse("t1_decision", args=[99999]),
			{"feedback": "", "internal_notes": "", "approved": "approved"},
		)
		self.assertEqual(response.status_code, 404)


class T2DecisionTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t2rev"), "t2_review")
		self.client.force_login(self.reviewer)
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)

	def _decide(self, ship=None, **overrides):
		data = {
			"decision": T2.Decision.APPROVE,
			"deductions": "0",
			"feedback": "good",
			"justification": "solid work",
		}
		data.update(overrides)
		ship = ship or self.ship
		return self.client.post(reverse("t2_decision", args=[ship.id]), data)

	def test_approve_moves_to_t3_queue(self):
		self._decide()
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T3_QUEUE)

		t2 = T2.objects.get()
		self.assertEqual(t2.decision, T2.Decision.APPROVE)
		self.assertEqual(t2.reviewer, self.reviewer)

	def test_return_to_printers(self):
		self._decide(decision=T2.Decision.RETURN_PRINT)
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.PRINT_QUEUE)

	def test_return_to_t1(self):
		self._decide(decision=T2.Decision.RETURN_T1)
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T1_QUEUE)

	def test_invalid_decision_rejected(self):
		self._decide(decision="X")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T2_QUEUE)
		self.assertEqual(T2.objects.count(), 0)

	def test_deductions_recorded(self):
		self._decide(deductions="30")
		self.assertEqual(T2.objects.get().deductions, 30)

	def test_negative_deductions_rejected(self):
		self._decide(deductions="-5")
		self.assertEqual(T2.objects.count(), 0)

	def test_non_integer_deductions_rejected(self):
		self._decide(deductions="lots")
		self.assertEqual(T2.objects.count(), 0)

	def test_blank_deductions_default_to_zero(self):
		self._decide(deductions="")
		self.assertEqual(T2.objects.get().deductions, 0)

	def test_deductions_must_be_less_than_logged_time(self):
		response = self._decide(deductions="240")
		self.assertEqual(T2.objects.count(), 0)
		self.assertTrue(any("Deduction too large" in m for m in message_texts(response)))
		self._decide(deductions="239")
		self.assertEqual(T2.objects.count(), 1)

	def test_feedback_and_justification_length_limits(self):
		for overrides in ({"feedback": "x" * 101}, {"justification": "x" * 401}):
			with self.subTest(**overrides):
				self._decide(**overrides)
				self.assertEqual(T2.objects.count(), 0)

	def test_ship_must_be_in_t2_queue(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T1_QUEUE)
		response = self._decide(ship=ship)
		ship.refresh_from_db()
		self.assertEqual(ship.status, Ship.ShipStatus.T1_QUEUE)
		self.assertIn("ship not in T2 queue", message_texts(response))

	def test_audit_log_recorded(self):
		self._decide(deductions="15")
		log = AuditLog.objects.get(action="t2_decision")
		self.assertEqual(log.metadata["deductions"], 15)
		self.assertEqual(log.metadata["new_ship_status"], Ship.ShipStatus.T3_QUEUE)


class T3DecisionTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t3rev"), "t3_review")
		self.client.force_login(self.reviewer)
		self.author = make_user("author", slack_id="U0AUTHOR", layers=10)
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE)

	def _decide(self, ship=None, **overrides):
		data = {
			"decision": T3.Decision.APPROVE,
			"internal_notes": "clean",
			"payout_time": "120",
			"airtable_time": "120",
		}
		data.update(overrides)
		ship = ship or self.ship
		return self.client.post(reverse("t3_decision", args=[ship.id]), data)

	def _author_layers(self):
		self.author.hackclub_profile.refresh_from_db()
		return self.author.hackclub_profile.layers

	def test_approve_finalizes_and_pays_out(self):
		self._decide(payout_time="120")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.FINALIZED)
		self.assertEqual(self._author_layers(), 20)

		t3 = T3.objects.get()
		self.assertEqual(t3.payout_time, 120)
		self.assertEqual(t3.airtable_time, 120)
		self.assertEqual(t3.reviewer, self.reviewer)

	def test_returns_do_not_pay_out(self):
		cases = {
			T3.Decision.RETURN_T1: Ship.ShipStatus.T1_QUEUE,
			T3.Decision.RETURN_T2: Ship.ShipStatus.T2_QUEUE,
			T3.Decision.RETURN_PRINT: Ship.ShipStatus.PRINT_QUEUE,
		}
		for decision, expected_status in cases.items():
			with self.subTest(decision=decision):
				ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE, journal_minutes=())
				self._decide(ship=ship, decision=decision)
				ship.refresh_from_db()
				self.assertEqual(ship.status, expected_status)
		self.assertEqual(self._author_layers(), 10)

	def test_invalid_decision_rejected(self):
		self._decide(decision="??")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T3_QUEUE)
		self.assertEqual(T3.objects.count(), 0)
		self.assertEqual(self._author_layers(), 10)

	def test_non_integer_times_rejected(self):
		for overrides in ({"payout_time": "abc"}, {"airtable_time": "abc"}):
			with self.subTest(**overrides):
				self._decide(**overrides)
				self.assertEqual(T3.objects.count(), 0)

	def test_ship_must_be_in_t3_queue(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE, journal_minutes=())
		response = self._decide(ship=ship)
		ship.refresh_from_db()
		self.assertEqual(ship.status, Ship.ShipStatus.T2_QUEUE)
		self.assertIn("ship not in T3 queue", message_texts(response))

	def test_audit_log_records_payout(self):
		self._decide(payout_time="60")
		log = AuditLog.objects.get(action="t3_decision")
		self.assertEqual(log.metadata["payout_layers"], 5)

	def test_slack_dm_sent(self):
		self._decide()
		self.slack_dm_mocks["review"].assert_called_once()


class FraudReviewProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t3rev"), "t3_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)

	def test_deductions_subtracted_from_logged_time(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE, journal_minutes=(100, 100))
		T2.objects.create(
			ship=ship, reviewer=self.reviewer, decision=T2.Decision.APPROVE,
			deductions=50, feedback="", justification="",
		)
		response = self.client.get(reverse("fraud_review_project", args=[ship.id]))
		self.assertEqual(response.context["logged_time"], 200)
		self.assertEqual(response.context["deductions"], 50)
		self.assertEqual(response.context["total_time"], 150)

	def test_latest_t2_deductions_used(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE, journal_minutes=(100,))
		for deductions in (10, 30):
			T2.objects.create(
				ship=ship, reviewer=self.reviewer, decision=T2.Decision.APPROVE,
				deductions=deductions, feedback="", justification="",
			)
		response = self.client.get(reverse("fraud_review_project", args=[ship.id]))
		self.assertEqual(response.context["deductions"], 30)

	def test_total_time_clamped_at_zero(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE, journal_minutes=(30,))
		T2.objects.create(
			ship=ship, reviewer=self.reviewer, decision=T2.Decision.APPROVE,
			deductions=100, feedback="", justification="",
		)
		response = self.client.get(reverse("fraud_review_project", args=[ship.id]))
		self.assertEqual(response.context["total_time"], 0)


class LockUnlockProjectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t2rev"), "t2_review")
		self.client.force_login(self.reviewer)
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author)

	def test_lock_project(self):
		response = self.client.post(reverse("lock_project", args=[self.project.id]))
		self.assertEqual(response.status_code, 302)
		self.project.refresh_from_db()
		self.assertTrue(self.project.locked)
		self.assertTrue(AuditLog.objects.filter(action="lock_project").exists())
		self.slack_dm_mocks["review"].assert_called_once()

	def test_unlock_project(self):
		self.project.locked = True
		self.project.save()
		self.client.post(reverse("unlock_project", args=[self.project.id]))
		self.project.refresh_from_db()
		self.assertFalse(self.project.locked)
		self.assertTrue(AuditLog.objects.filter(action="unlock_project").exists())

	def test_redirects_to_referer(self):
		response = self.client.post(
			reverse("lock_project", args=[self.project.id]), HTTP_REFERER="/root/projects/"
		)
		self.assertEqual(response.url, "/root/projects/")

	def test_t1_reviewer_cannot_lock(self):
		t1_reviewer = grant_perms(make_user("t1only"), "t1_review")
		self.client.force_login(t1_reviewer)
		self.client.post(reverse("lock_project", args=[self.project.id]))
		self.project.refresh_from_db()
		self.assertFalse(self.project.locked)

	def test_regular_user_cannot_lock(self):
		self.client.force_login(make_user("pleb"))
		self.client.post(reverse("lock_project", args=[self.project.id]))
		self.project.refresh_from_db()
		self.assertFalse(self.project.locked)

	def test_deleted_project_404(self):
		self.project.deleted = True
		self.project.save()
		response = self.client.post(reverse("lock_project", args=[self.project.id]))
		self.assertEqual(response.status_code, 404)
