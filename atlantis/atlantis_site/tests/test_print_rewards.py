from django.urls import reverse
from django.utils import timezone

from ..models import AuditLog, Item, Order, Print, Ship
from ..views.helpers import grant_print_rewards, finalized_print_grams
from .base import (
	BaseTestCase,
	grant_perms,
	make_project,
	make_ship,
	make_user,
	message_texts,
)


def make_reward_item(name="Filament spool"):
	return Item.objects.create(
		name=name, description="reward", cost=5, is_print_reward=True
	)


def make_print(printer, ship, weight, **kwargs):
	return Print.objects.create(
		printer=printer, ship=ship, weight=weight, finished_time=timezone.now(), **kwargs
	)


class FinalizedPrintGramsTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.printer = make_user("printer")
		self.project = make_project(make_user("author"))

	def test_counts_only_finalized_ships(self):
		final = make_ship(self.project, status=Ship.ShipStatus.FINALIZED, journal_minutes=())
		pending = make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE, journal_minutes=())
		make_print(self.printer, final, 600)
		make_print(self.printer, pending, 900)
		self.assertEqual(finalized_print_grams(self.printer), 600)

	def test_sums_multiple_finalized_prints(self):
		s1 = make_ship(self.project, status=Ship.ShipStatus.FINALIZED, journal_minutes=())
		s2 = make_ship(self.project, status=Ship.ShipStatus.FINALIZED, journal_minutes=())
		make_print(self.printer, s1, 400)
		make_print(self.printer, s2, 700)
		self.assertEqual(finalized_print_grams(self.printer), 1100)

	def test_ignores_prints_without_weight(self):
		final = make_ship(self.project, status=Ship.ShipStatus.FINALIZED, journal_minutes=())
		Print.objects.create(printer=self.printer, ship=final)
		self.assertEqual(finalized_print_grams(self.printer), 0)


class GrantPrintRewardsHelperTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.printer = make_user("printer")
		self.project = make_project(make_user("author"))
		self.item = make_reward_item()

	def _finalized_print(self, weight):
		ship = make_ship(self.project, status=Ship.ShipStatus.FINALIZED, journal_minutes=())
		return make_print(self.printer, ship, weight)

	def _reward_kg(self):
		self.printer.hackclub_profile.refresh_from_db()
		return self.printer.hackclub_profile.print_reward_kg

	def test_below_threshold_no_order(self):
		self._finalized_print(999)
		result = grant_print_rewards(self.printer)
		self.assertEqual(result["created"], 0)
		self.assertEqual(Order.objects.count(), 0)
		self.assertEqual(self._reward_kg(), 0)

	def test_crossing_one_kg_creates_pending_order(self):
		self._finalized_print(1000)
		result = grant_print_rewards(self.printer)
		self.assertEqual(result["created"], 1)
		order = Order.objects.get()
		self.assertEqual(order.owner, self.printer)
		self.assertEqual(order.item, self.item)
		self.assertEqual(order.quantity, 1)
		self.assertEqual(order.cost, 0)
		self.assertEqual(order.status, Order.OrderStatus.PENDING)
		self.assertEqual(self._reward_kg(), 1)

	def test_multiple_kg_at_once_uses_quantity(self):
		self._finalized_print(2500)
		result = grant_print_rewards(self.printer)
		self.assertEqual(result["created"], 2)
		order = Order.objects.get()
		self.assertEqual(order.quantity, 2)
		self.assertEqual(self._reward_kg(), 2)

	def test_idempotent_no_duplicate_orders(self):
		self._finalized_print(1200)
		grant_print_rewards(self.printer)
		grant_print_rewards(self.printer)
		self.assertEqual(Order.objects.count(), 1)
		self.assertEqual(self._reward_kg(), 1)

	def test_incremental_reward_after_more_printing(self):
		self._finalized_print(1000)
		grant_print_rewards(self.printer)
		self._finalized_print(1100)
		result = grant_print_rewards(self.printer)
		self.assertEqual(result["created"], 1)
		self.assertEqual(Order.objects.count(), 2)
		self.assertEqual(self._reward_kg(), 2)

	def test_limited_stock_reward_item_decremented(self):
		self.item.stock = 5
		self.item.save(update_fields=["stock"])
		self._finalized_print(2500)
		grant_print_rewards(self.printer)
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 3)

	def test_reward_stock_never_goes_negative(self):
		self.item.stock = 1
		self.item.save(update_fields=["stock"])
		self._finalized_print(2500)  # owes 2, only 1 in stock
		result = grant_print_rewards(self.printer)
		self.assertEqual(result["created"], 2)
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 0)

	def test_no_reward_item_leaves_counter_untouched(self):
		self.item.delete()
		self._finalized_print(1500)
		result = grant_print_rewards(self.printer)
		self.assertTrue(result["no_item"])
		self.assertEqual(result["owed"], 1)
		self.assertEqual(Order.objects.count(), 0)
		self.assertEqual(self._reward_kg(), 0)

	def test_backlog_granted_once_item_exists(self):
		self.item.is_print_reward = False
		self.item.save()
		self._finalized_print(1500)
		grant_print_rewards(self.printer)  # no item yet
		self.item.is_print_reward = True
		self.item.save()
		result = grant_print_rewards(self.printer)
		self.assertEqual(result["created"], 1)
		self.assertEqual(Order.objects.count(), 1)

	def test_audit_logged_when_request_given(self):
		self._finalized_print(1000)
		organizer = grant_perms(make_user("org"), "organizer")
		self.client.force_login(organizer)
		self.client.post(reverse("grant_print_reward", args=[self.printer.id]))
		self.assertTrue(AuditLog.objects.filter(action="grant_print_reward").exists())


class T3AutoRewardTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t3rev"), "t3_review")
		self.client.force_login(self.reviewer)
		self.printer = make_user("printer", slack_id="U0PRINTER")
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.item = make_reward_item()

	def _decide(self, ship):
		return self.client.post(reverse("t3_decision", args=[ship.id]), {
			"decision": "A",
			"internal_notes": "ok",
			"payout_time": "60",
			"airtable_time": "60",
		})

	def test_finalizing_grants_reward_to_printer(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE)
		make_print(self.printer, ship, 1000)
		self._decide(ship)
		order = Order.objects.get(owner=self.printer)
		self.assertEqual(order.item, self.item)
		self.assertEqual(order.quantity, 1)
		self.printer.hackclub_profile.refresh_from_db()
		self.assertEqual(self.printer.hackclub_profile.print_reward_kg, 1)

	def test_light_print_no_reward(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE)
		make_print(self.printer, ship, 200)
		self._decide(ship)
		self.assertFalse(Order.objects.filter(owner=self.printer).exists())

	def test_missing_reward_item_warns(self):
		self.item.delete()
		ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE)
		make_print(self.printer, ship, 1000)
		response = self._decide(ship)
		self.assertTrue(any("no reward item is set" in m.lower() for m in message_texts(response)))
		self.assertFalse(Order.objects.filter(owner=self.printer).exists())


class PrintRewardsViewAccessTests(BaseTestCase):
	def test_non_organizer_blocked(self):
		for user in (make_user("pleb"), grant_perms(make_user("printer_only"), "printer")):
			self.client.force_login(user)
			with self.subTest(user=user.username):
				self.assertEqual(self.client.get(reverse("print_rewards")).status_code, 302)
				self.assertEqual(self.client.post(reverse("grant_all_print_rewards")).status_code, 302)
				self.assertEqual(self.client.post(reverse("grant_print_reward", args=[1])).status_code, 302)

	def test_organizer_allowed(self):
		self.client.force_login(grant_perms(make_user("org"), "organizer"))
		self.assertEqual(self.client.get(reverse("print_rewards")).status_code, 200)


class PrintRewardsManualGrantTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("org"), "organizer")
		self.client.force_login(self.organizer)
		self.printer = make_user("printer", slack_id="U0PRINTER")
		self.project = make_project(make_user("author"))
		self.item = make_reward_item()

	def _finalized_print(self, printer, weight):
		ship = make_ship(self.project, status=Ship.ShipStatus.FINALIZED, journal_minutes=())
		return make_print(printer, ship, weight)

	def test_dashboard_lists_owed_printers(self):
		self._finalized_print(self.printer, 1500)
		response = self.client.get(reverse("print_rewards"))
		rows = response.context["rows"]
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["owed"], 1)

	def test_manual_grant_creates_order(self):
		self._finalized_print(self.printer, 1000)
		self.client.post(reverse("grant_print_reward", args=[self.printer.id]))
		self.assertEqual(Order.objects.filter(owner=self.printer).count(), 1)

	def test_grant_all_grants_every_owed_printer(self):
		other = make_user("printer2")
		self._finalized_print(self.printer, 1000)
		self._finalized_print(other, 2000)
		self.client.post(reverse("grant_all_print_rewards"))
		self.assertEqual(Order.objects.get(owner=self.printer).quantity, 1)
		self.assertEqual(Order.objects.get(owner=other).quantity, 2)


class RewardItemDesignationTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.admin = grant_perms(make_user("shopadmin"), "organizer")
		self.client.force_login(self.admin)

	def _create(self, **overrides):
		data = {
			"name": "Spool",
			"description": "reward",
			"cost": "5",
			"imageUrl": "https://example.com/i.png",
			"category": "Rewards",
		}
		data.update(overrides)
		return self.client.post(reverse("create_item"), data)

	def test_create_sets_reward_flag(self):
		self._create(is_print_reward="on")
		item = Item.objects.get()
		self.assertTrue(item.is_print_reward)

	def test_designating_new_reward_clears_previous(self):
		old = Item.objects.create(name="Old", description="x", cost=1, is_print_reward=True)
		self._create(name="New", is_print_reward="on")
		old.refresh_from_db()
		new = Item.objects.get(name="New")
		self.assertFalse(old.is_print_reward)
		self.assertTrue(new.is_print_reward)

	def test_edit_can_toggle_reward_off(self):
		item = Item.objects.create(name="R", description="x", cost=1, is_print_reward=True)
		self.client.post(reverse("edit_item", args=[item.id]), {
			"name": "R",
			"description": "x",
			"cost": "1",
			"imageUrl": "https://example.com/i.png",
			"category": "Other",
		})
		item.refresh_from_db()
		self.assertFalse(item.is_print_reward)
