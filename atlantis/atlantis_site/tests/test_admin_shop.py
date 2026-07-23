from django.urls import reverse

from ..models import AuditLog, Item, Order
from .base import BaseTestCase, grant_perms, make_user, message_texts


class ShopAdminAccessTests(BaseTestCase):
	def test_fulfillment_perm_required(self):
		item = Item.objects.create(name="Thing", description="x", cost=1)
		urls = [
			reverse("shop_dash"),
			reverse("fulfillment_dash"),
		]
		for user in (make_user("pleb"), grant_perms(make_user("printer_only"), "printer")):
			self.client.force_login(user)
			for url in urls:
				with self.subTest(user=user.username, url=url):
					self.assertEqual(self.client.get(url).status_code, 302)

	def test_fulfillment_and_organizer_allowed(self):
		for codename in ("fulfillment", "organizer"):
			user = grant_perms(make_user(f"user_{codename}"), codename)
			self.client.force_login(user)
			with self.subTest(perm=codename):
				self.assertEqual(self.client.get(reverse("shop_dash")).status_code, 200)
				self.assertEqual(self.client.get(reverse("fulfillment_dash")).status_code, 200)


class ShopDashTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.admin = grant_perms(make_user("shopadmin"), "fulfillment")
		self.client.force_login(self.admin)

	def test_lists_all_items_including_deleted(self):
		active = Item.objects.create(name="Active", description="x", cost=1)
		deleted = Item.objects.create(name="Deleted", description="x", cost=1, deleted=True)
		response = self.client.get(reverse("shop_dash"))
		self.assertEqual(list(response.context["items"]), [active, deleted])

	def test_categories_only_from_active_items(self):
		Item.objects.create(name="A", description="x", cost=1, category="Tools")
		Item.objects.create(name="B", description="x", cost=1, category="Gone", deleted=True)
		response = self.client.get(reverse("shop_dash"))
		self.assertEqual(list(response.context["categories"]), ["Tools"])


class CreateItemTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.admin = grant_perms(make_user("shopadmin"), "fulfillment")
		self.client.force_login(self.admin)

	def _create(self, **overrides):
		data = {
			"name": "Filament",
			"description": "1kg PLA",
			"cost": "30",
			"imageUrl": "https://example.com/item.png",
			"category": "Materials",
		}
		data.update(overrides)
		return self.client.post(reverse("create_item"), data)

	def test_creates_item(self):
		self._create()
		item = Item.objects.get()
		self.assertEqual(item.name, "Filament")
		self.assertEqual(item.cost, 30)
		self.assertEqual(item.category, "Materials")
		self.assertTrue(AuditLog.objects.filter(action="create_item").exists())

	def test_blank_category_defaults_to_other(self):
		self._create(category="")
		self.assertEqual(Item.objects.get().category, "Other")

	def test_required_fields(self):
		for field in ("name", "description", "cost", "imageUrl"):
			with self.subTest(field=field):
				self._create(**{field: ""})
				self.assertEqual(Item.objects.count(), 0)

	def test_non_integer_cost_rejected(self):
		self._create(cost="cheap")
		self.assertEqual(Item.objects.count(), 0)

	def test_invalid_image_url_rejected(self):
		self.image_url_mocks["shop"].return_value = False
		self._create()
		self.assertEqual(Item.objects.count(), 0)

	def test_stock_defaults_to_unlimited(self):
		self._create()
		self.assertEqual(Item.objects.get().stock, -1)

	def test_stock_value_respected(self):
		self._create(stock="5")
		self.assertEqual(Item.objects.get().stock, 5)

	def test_invalid_stock_rejected(self):
		for value in ("abc", "-2", "1.5"):
			with self.subTest(value=value):
				self._create(stock=value)
				self.assertEqual(Item.objects.count(), 0)


class EditItemTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.admin = grant_perms(make_user("shopadmin"), "fulfillment")
		self.client.force_login(self.admin)
		self.item = Item.objects.create(
			name="Old", description="old desc", cost=10,
			imageUrl="https://example.com/old.png", category="Old",
		)

	def _edit(self, **overrides):
		data = {
			"name": "New",
			"description": "new desc",
			"cost": "20",
			"imageUrl": "https://example.com/new.png",
			"category": "New",
		}
		data.update(overrides)
		return self.client.post(reverse("edit_item", args=[self.item.id]), data)

	def test_edits_item_and_records_audit(self):
		self._edit()
		self.item.refresh_from_db()
		self.assertEqual(self.item.name, "New")
		self.assertEqual(self.item.cost, 20)

		log = AuditLog.objects.get(action="edit_item")
		self.assertEqual(log.metadata["previous"]["name"], "Old")
		self.assertEqual(log.metadata["new"]["name"], "New")

	def test_validation_failures_leave_item_unchanged(self):
		for overrides in ({"name": ""}, {"description": ""}, {"cost": ""}, {"cost": "abc"}):
			with self.subTest(**overrides):
				self._edit(**overrides)
				self.item.refresh_from_db()
				self.assertEqual(self.item.name, "Old")

	def test_edit_updates_stock(self):
		self._edit(stock="7")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 7)

	def test_edit_blank_stock_defaults_to_unlimited(self):
		self.item.stock = 4
		self.item.save(update_fields=["stock"])
		self._edit(stock="")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, -1)

	def test_edit_invalid_stock_leaves_item_unchanged(self):
		self.item.stock = 4
		self.item.save(update_fields=["stock"])
		self._edit(stock="-2")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 4)

	def test_unknown_item_404(self):
		response = self.client.post(reverse("edit_item", args=[9999]), {})
		self.assertEqual(response.status_code, 404)


class DeleteItemTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.admin = grant_perms(make_user("shopadmin"), "fulfillment")
		self.client.force_login(self.admin)
		self.item = Item.objects.create(name="Doomed", description="x", cost=1)

	def test_soft_deletes(self):
		self.client.post(reverse("delete_item", args=[self.item.id]))
		self.item.refresh_from_db()
		self.assertTrue(self.item.deleted)
		self.assertTrue(AuditLog.objects.filter(action="delete_item").exists())

	def test_get_not_allowed(self):
		response = self.client.get(reverse("delete_item", args=[self.item.id]))
		self.assertEqual(response.status_code, 405)
		self.item.refresh_from_db()
		self.assertFalse(self.item.deleted)


class FulfillmentDashTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.admin = grant_perms(make_user("fulfiller"), "fulfillment")
		self.client.force_login(self.admin)
		self.buyer = make_user("buyer")
		self.item = Item.objects.create(name="Thing", description="x", cost=5)

	def test_orders_split_by_status(self):
		pending = Order.objects.create(owner=self.buyer, item=self.item)
		fulfilled = Order.objects.create(
			owner=self.buyer, item=self.item, status=Order.OrderStatus.FULFILLED
		)
		response = self.client.get(reverse("fulfillment_dash"))
		self.assertEqual(list(response.context["pending_orders"]), [pending])
		self.assertEqual(list(response.context["other_orders"]), [fulfilled])


class UpdateOrderStatusTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.admin = grant_perms(make_user("fulfiller"), "fulfillment")
		self.client.force_login(self.admin)
		self.buyer = make_user("buyer", slack_id="U0BUYER", layers=0)
		self.item = Item.objects.create(name="Thing", description="x", cost=25)
		self.order = Order.objects.create(owner=self.buyer, item=self.item, quantity=2)

	def _update(self, action, order=None):
		order = order or self.order
		return self.client.post(
			reverse("update_order_status", args=[order.id]), {"action": action}
		)

	def _buyer_layers(self):
		self.buyer.hackclub_profile.refresh_from_db()
		return self.buyer.hackclub_profile.layers

	def test_fulfill_order(self):
		self._update("fulfilled")
		self.order.refresh_from_db()
		self.assertEqual(self.order.status, Order.OrderStatus.FULFILLED)
		self.assertEqual(self.order.fulfiller, self.admin)
		self.assertIsNotNone(self.order.fulfilled_at)
		self.assertTrue(AuditLog.objects.filter(action="update_order_status").exists())
		self.slack_dm_mocks["shop"].assert_called_once()

	def test_deny_order(self):
		self._update("denied")
		self.order.refresh_from_db()
		self.assertEqual(self.order.status, Order.OrderStatus.DENIED)
		self.assertEqual(self._buyer_layers(), 0)

	def test_only_fulfillment_stamps_fulfilled_at(self):
		# Denying (or reverting to pending) must not set a bogus fulfilled_at.
		self._update("denied")
		self.order.refresh_from_db()
		self.assertIsNone(self.order.fulfilled_at)

		fulfilled = Order.objects.create(owner=self.buyer, item=self.item)
		self._update("fulfilled", order=fulfilled)
		fulfilled.refresh_from_db()
		self.assertIsNotNone(fulfilled.fulfilled_at)

	def test_refund_restores_layers(self):
		self._update("refunded")
		self.order.refresh_from_db()
		self.assertEqual(self.order.status, Order.OrderStatus.REFUNDED)
		self.assertTrue(self.order.refunded)
		self.assertEqual(self._buyer_layers(), 50)

	def test_refunded_order_cannot_be_edited_again(self):
		self._update("refunded")
		response = self._update("fulfilled")
		self.order.refresh_from_db()
		self.assertEqual(self.order.status, Order.OrderStatus.REFUNDED)
		self.assertEqual(self._buyer_layers(), 50)
		self.assertIn(
			"This order has already been refunded and cannot be further edited.",
			message_texts(response),
		)

	def test_same_status_rejected(self):
		response = self._update("pending")
		self.order.refresh_from_db()
		self.assertEqual(self.order.status, Order.OrderStatus.PENDING)
		self.assertTrue(any("already" in m for m in message_texts(response)))

	def test_invalid_action_rejected(self):
		response = self._update("exploded")
		self.order.refresh_from_db()
		self.assertEqual(self.order.status, Order.OrderStatus.PENDING)
		self.assertIn("Invalid order action.", message_texts(response))

	def test_denied_order_can_be_marked_pending_again(self):
		self._update("denied")
		self._update("pending")
		self.order.refresh_from_db()
		self.assertEqual(self.order.status, Order.OrderStatus.PENDING)

	def test_deny_restores_stock(self):
		self.item.stock = 3
		self.item.save(update_fields=["stock"])
		self._update("denied")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 5)

	def test_refund_restores_stock(self):
		self.item.stock = 3
		self.item.save(update_fields=["stock"])
		self._update("refunded")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 5)

	def test_reverting_denied_to_pending_recommits_stock(self):
		self.item.stock = 3
		self.item.save(update_fields=["stock"])
		self._update("denied")
		self._update("pending")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 3)

	def test_fulfilling_pending_order_leaves_stock_unchanged(self):
		self.item.stock = 3
		self.item.save(update_fields=["stock"])
		self._update("fulfilled")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 3)

	def test_unlimited_stock_untouched_on_deny(self):
		self.assertTrue(self.item.unlimited_stock)
		self._update("denied")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, -1)
