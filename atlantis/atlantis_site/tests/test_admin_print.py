from django.urls import reverse
from django.utils import timezone

from ..models import AuditLog, Print, Ship
from .base import (
	BaseTestCase,
	grant_perms,
	make_project,
	make_ship,
	make_user,
	message_texts,
)


class PrintAccessControlTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.ship = make_ship(
			make_project(make_user("author"), shippable=True),
			status=Ship.ShipStatus.PRINT_QUEUE,
		)

	def test_non_printer_cannot_access(self):
		for user in (make_user("pleb"), grant_perms(make_user("t1only"), "t1_review")):
			self.client.force_login(user)
			with self.subTest(user=user.username):
				self.assertEqual(self.client.get(reverse("print_dash")).status_code, 302)
				response = self.client.post(reverse("claim_print", args=[self.ship.id]))
				self.assertEqual(response.status_code, 302)
				self.assertEqual(self.ship.prints.count(), 0)

	def test_printer_and_organizer_can_access(self):
		for codename in ("printer", "organizer"):
			user = grant_perms(make_user(f"user_{codename}"), codename)
			self.client.force_login(user)
			with self.subTest(perm=codename):
				self.assertEqual(self.client.get(reverse("print_dash")).status_code, 200)


class PrintDashTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.printer = grant_perms(make_user("printer"), "printer")
		self.client.force_login(self.printer)
		self.project = make_project(make_user("author"), shippable=True)

	def test_lists_queued_ships_and_own_claimed_prints(self):
		queued = make_ship(self.project, status=Ship.ShipStatus.PRINT_QUEUE)
		claimed_ship = make_ship(self.project, status=Ship.ShipStatus.BEING_PRINTED)
		my_print = Print.objects.create(ship=claimed_ship, printer=self.printer)

		other_printer = grant_perms(make_user("other_printer"), "printer")
		other_ship = make_ship(self.project, status=Ship.ShipStatus.BEING_PRINTED)
		Print.objects.create(ship=other_ship, printer=other_printer)

		response = self.client.get(reverse("print_dash"))
		self.assertEqual(list(response.context["ships"]), [queued])
		self.assertEqual(list(response.context["claimed_prints"]), [my_print])

	def test_finished_prints_not_listed_as_claimed(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)
		Print.objects.create(ship=ship, printer=self.printer, finished_time=timezone.now())
		response = self.client.get(reverse("print_dash"))
		self.assertEqual(list(response.context["claimed_prints"]), [])


class ClaimPrintTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.printer = grant_perms(make_user("printer"), "printer")
		self.client.force_login(self.printer)
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project, status=Ship.ShipStatus.PRINT_QUEUE)

	def _claim(self, ship=None):
		ship = ship or self.ship
		return self.client.post(reverse("claim_print", args=[ship.id]))

	def test_claim_creates_print_and_updates_status(self):
		response = self._claim()
		self.assertRedirects(response, reverse("print_project", args=[self.ship.id]))

		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.BEING_PRINTED)

		print_record = self.ship.prints.get()
		self.assertEqual(print_record.printer, self.printer)
		self.assertEqual(print_record.decision, Print.Decision.PRINTING)
		self.assertIsNone(print_record.finished_time)

		self.assertTrue(AuditLog.objects.filter(action="claim_print").exists())
		self.slack_dm_mocks["print"].assert_called_once()

	def test_cannot_claim_ship_not_in_print_queue(self):
		for status in (Ship.ShipStatus.T1_QUEUE, Ship.ShipStatus.BEING_PRINTED,
					   Ship.ShipStatus.FINALIZED):
			with self.subTest(status=status):
				ship = make_ship(self.project, status=status, journal_minutes=())
				response = self._claim(ship)
				self.assertEqual(ship.prints.count(), 0)
				self.assertIn("print not in print queue", message_texts(response))

	def test_cannot_double_claim(self):
		Print.objects.create(ship=self.ship, printer=self.printer)
		response = self._claim()
		self.assertEqual(self.ship.prints.count(), 1)
		self.assertIn("already claimed", message_texts(response))


class UnclaimPrintTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.printer = grant_perms(make_user("printer"), "printer")
		self.client.force_login(self.printer)
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project, status=Ship.ShipStatus.BEING_PRINTED)
		self.print_record = Print.objects.create(ship=self.ship, printer=self.printer)

	def _unclaim(self, ship=None):
		ship = ship or self.ship
		return self.client.post(reverse("unclaim_print", args=[ship.id]))

	def test_unclaim_returns_ship_to_queue(self):
		self._unclaim()
		self.ship.refresh_from_db()
		self.print_record.refresh_from_db()

		self.assertEqual(self.ship.status, Ship.ShipStatus.PRINT_QUEUE)
		self.assertEqual(self.print_record.decision, Print.Decision.UNCLAIMED)
		self.assertIsNotNone(self.print_record.unclaimed_time)
		self.assertTrue(AuditLog.objects.filter(action="unclaim_print").exists())

	def test_only_claimer_can_unclaim(self):
		other_printer = grant_perms(make_user("other_printer"), "printer")
		self.client.force_login(other_printer)
		response = self._unclaim()
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.BEING_PRINTED)
		self.assertIn("this print isn't claimed by you!", message_texts(response))

	def test_cannot_unclaim_ship_not_being_printed(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.PRINT_QUEUE, journal_minutes=())
		response = self._unclaim(ship)
		self.assertIn("print is not being printed", message_texts(response))

	def test_no_active_print(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.BEING_PRINTED, journal_minutes=())
		response = self._unclaim(ship)
		self.assertIn("no active print found", message_texts(response))


class PrintProjectViewTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.printer = grant_perms(make_user("printer"), "printer")
		self.client.force_login(self.printer)
		self.project = make_project(make_user("author"), shippable=True)

	def test_can_claim_flag(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.PRINT_QUEUE)
		response = self.client.get(reverse("print_project", args=[ship.id]))
		self.assertTrue(response.context["can_claim"])
		self.assertIsNone(response.context["current_print"])

	def test_current_print_is_latest(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.BEING_PRINTED)
		Print.objects.create(ship=ship, printer=self.printer, unclaimed_time=timezone.now())
		latest = Print.objects.create(ship=ship, printer=self.printer)
		response = self.client.get(reverse("print_project", args=[ship.id]))
		self.assertEqual(response.context["current_print"], latest)
		self.assertFalse(response.context["can_claim"])


class PrintDecisionTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.printer = grant_perms(make_user("printer"), "printer")
		self.client.force_login(self.printer)
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project, status=Ship.ShipStatus.BEING_PRINTED)
		self.print_record = Print.objects.create(ship=self.ship, printer=self.printer)

	def _decide(self, ship=None, **overrides):
		data = {
			"decision": Print.Decision.APPROVE,
			"weight": "42",
			"image_url": "https://example.com/print.png",
			"feedback": "came out great",
			"internal_notes": "",
		}
		data.update(overrides)
		ship = ship or self.ship
		return self.client.post(reverse("print_decision", args=[ship.id]), data)

	def test_approve_moves_to_t2_and_completes_print(self):
		self._decide()
		self.ship.refresh_from_db()
		self.print_record.refresh_from_db()

		self.assertEqual(self.ship.status, Ship.ShipStatus.T2_QUEUE)
		self.assertEqual(self.print_record.decision, Print.Decision.APPROVE)
		self.assertEqual(self.print_record.weight, 42)
		self.assertEqual(self.print_record.image_url, "https://example.com/print.png")
		self.assertEqual(self.print_record.feedback, "came out great")
		self.assertIsNotNone(self.print_record.finished_time)
		self.assertTrue(AuditLog.objects.filter(action="print_decision").exists())

	def test_return_to_t1(self):
		self._decide(decision=Print.Decision.RETURN_T1)
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.T1_QUEUE)

	def test_invalid_decision_rejected(self):
		for decision in ("", "Z", Print.Decision.UNCLAIMED, Print.Decision.PRINTING):
			with self.subTest(decision=decision):
				self._decide(decision=decision)
				self.ship.refresh_from_db()
				self.assertEqual(self.ship.status, Ship.ShipStatus.BEING_PRINTED)
				self.print_record.refresh_from_db()
				self.assertIsNone(self.print_record.finished_time)

	def test_invalid_image_url_rejected(self):
		self.image_url_mocks["print"].return_value = False
		response = self._decide()
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.BEING_PRINTED)
		self.assertIn("Invalid image URL", message_texts(response))

	def test_non_integer_weight_rejected(self):
		self._decide(weight="heavy")
		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.BEING_PRINTED)

	def test_feedback_length_limit(self):
		for overrides in ({"feedback": "x" * 101}, {"internal_notes": "x" * 101}):
			with self.subTest(**overrides):
				self._decide(**overrides)
				self.ship.refresh_from_db()
				self.assertEqual(self.ship.status, Ship.ShipStatus.BEING_PRINTED)

	def test_ship_must_be_being_printed(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.PRINT_QUEUE, journal_minutes=())
		response = self._decide(ship=ship)
		self.assertIn("print not being printed", message_texts(response))

	def test_no_active_print_errors(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.BEING_PRINTED, journal_minutes=())
		response = self._decide(ship=ship)
		self.assertIn("no active print found", message_texts(response))
