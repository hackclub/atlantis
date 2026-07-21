from django.urls import reverse

from ..models import Item, Order
from .base import BaseTestCase, make_user, message_texts


class ShopListTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("shopper")
		self.client.force_login(self.user)

	def test_login_required(self):
		self.client.logout()
		self.assertEqual(self.client.get(reverse("shop")).status_code, 302)

	def test_lists_only_non_deleted_items(self):
		visible = Item.objects.create(name="Filament", description="PLA", cost=10)
		Item.objects.create(name="Gone", description="x", cost=5, deleted=True)

		response = self.client.get(reverse("shop"))
		self.assertEqual(list(response.context["items"]), [visible])

	def test_items_ordered_by_category_then_id(self):
		b = Item.objects.create(name="B", description="x", cost=1, category="Tools")
		a = Item.objects.create(name="A", description="x", cost=1, category="Filament")
		c = Item.objects.create(name="C", description="x", cost=1, category="Tools")

		response = self.client.get(reverse("shop"))
		self.assertEqual(list(response.context["items"]), [a, b, c])


class ItemDetailTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("shopper")
		self.item = Item.objects.create(name="Filament", description="PLA", cost=10)
		self.client.force_login(self.user)

	def test_shows_item(self):
		response = self.client.get(reverse("item_detail", args=[self.item.id]))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["item"], self.item)

	def test_unknown_item_404(self):
		self.assertEqual(self.client.get(reverse("item_detail", args=[9999])).status_code, 404)


class OrderItemTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("buyer", layers=100)
		self.item = Item.objects.create(name="Filament", description="PLA", cost=25)
		self.client.force_login(self.user)

	def _order(self, quantity="1", user_notes="", item=None):
		item = item or self.item
		return self.client.post(
			reverse("order_item", args=[item.id]),
			{"quantity": quantity, "user_notes": user_notes},
		)

	def _layers(self):
		self.user.hackclub_profile.refresh_from_db()
		return self.user.hackclub_profile.layers

	def test_get_redirects_without_ordering(self):
		self.client.get(reverse("order_item", args=[self.item.id]))
		self.assertEqual(Order.objects.count(), 0)

	def test_successful_order_deducts_layers(self):
		response = self._order(quantity="2", user_notes="red please")
		self.assertIn("Successfully ordered 2x Filament!", message_texts(response))

		order = Order.objects.get()
		self.assertEqual(order.owner, self.user)
		self.assertEqual(order.item, self.item)
		self.assertEqual(order.quantity, 2)
		self.assertEqual(order.cost, 25)
		self.assertEqual(order.status, Order.OrderStatus.PENDING)
		self.assertEqual(order.user_notes, "red please")
		self.assertEqual(self._layers(), 50)

	def test_exact_balance_allowed(self):
		self._order(quantity="4")
		self.assertEqual(Order.objects.count(), 1)
		self.assertEqual(self._layers(), 0)

	def test_insufficient_layers_rejected(self):
		response = self._order(quantity="5")
		self.assertEqual(Order.objects.count(), 0)
		self.assertEqual(self._layers(), 100)
		self.assertIn(
			"You do not have enough layers to purchase this item.", message_texts(response)
		)

	def test_quantity_validation(self):
		for quantity in ("", "0", "-1", "abc", "1.5"):
			with self.subTest(quantity=quantity):
				self._order(quantity=quantity)
				self.assertEqual(Order.objects.count(), 0)
				self.assertEqual(self._layers(), 100)

	def test_unknown_item_404(self):
		response = self.client.post(reverse("order_item", args=[9999]), {"quantity": "1"})
		self.assertEqual(response.status_code, 404)

	def test_unlimited_stock_not_decremented(self):
		# Default stock of -1 means unlimited; ordering must not touch it.
		self._order(quantity="2")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, -1)

	def test_limited_stock_decremented_on_order(self):
		self.item.stock = 3
		self.item.save(update_fields=["stock"])
		self._order(quantity="2")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 1)
		self.assertEqual(Order.objects.count(), 1)

	def test_order_exceeding_stock_rejected(self):
		self.item.stock = 1
		self.item.save(update_fields=["stock"])
		response = self._order(quantity="2")
		self.item.refresh_from_db()
		self.assertEqual(self.item.stock, 1)
		self.assertEqual(Order.objects.count(), 0)
		self.assertEqual(self._layers(), 100)
		self.assertIn("Only 1 of this item is left in stock.", message_texts(response))

	def test_out_of_stock_rejected(self):
		self.item.stock = 0
		self.item.save(update_fields=["stock"])
		response = self._order(quantity="1")
		self.assertEqual(Order.objects.count(), 0)
		self.assertEqual(self._layers(), 100)
		self.assertIn("This item is out of stock.", message_texts(response))
