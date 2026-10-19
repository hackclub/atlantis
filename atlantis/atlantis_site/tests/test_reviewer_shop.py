from datetime import timedelta

from django.core.cache import cache
from django.urls import reverse

from .. import reviewer_shop
from ..models import AuditLog, Item, Order, Ship, T1, T2, TimelapseReview
from ..views.client.shop import shoppable
from .base import (
	BaseTestCase, grant_perms, make_journal, make_project, make_user, message_texts,
)

Currency = Item.ReviewCurrency


def reviewer_item(cost=10, currency=Currency.T1_REVIEWS, **kwargs):
	return Item.objects.create(
		name=kwargs.pop("name", "$10 Snack Grant"), description="snacks", cost=cost,
		kind=Item.Kind.REVIEWER, review_currency=currency, **kwargs,
	)


class ReviewWorkMixin:
	def make_work(self):
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = Ship.objects.create(project=self.project)

	def t1s(self, n, reviewer=None):
		T1.objects.bulk_create([
			T1(ship=self.ship, reviewer=reviewer or self.reviewer, feedback="", internal_notes="", approved=True)
			for _ in range(n)
		])

	def t2s(self, n):
		T2.objects.bulk_create([
			T2(ship=self.ship, reviewer=self.reviewer, feedback="", justification="")
			for _ in range(n)
		])

	def lapse_minutes(self, *minutes):
		for m in minutes:
			journal = make_journal(self.project, time_spent=m)
			TimelapseReview.objects.create(journal=journal, reviewer=self.reviewer)


class BalanceTests(ReviewWorkMixin, BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = make_user("reviewer", slack_id="U0REV")
		self.make_work()

	def test_each_desk_is_its_own_currency(self):
		self.t1s(7)
		self.t2s(3)
		self.lapse_minutes(90, 45)
		self.assertEqual(reviewer_shop.balances(self.reviewer), {
			Currency.T1_REVIEWS: 7,
			Currency.T2_REVIEWS: 3,
			Currency.LAPSE_HOURS: 135 * 60,
		})

	def test_spending_comes_off_the_matching_currency_only(self):
		self.t1s(10)
		self.lapse_minutes(5 * 60)
		Order.objects.create(owner=self.reviewer, item=reviewer_item(cost=4), quantity=2)
		Order.objects.create(owner=self.reviewer, item=reviewer_item(cost=2, currency=Currency.LAPSE_HOURS))
		balances = reviewer_shop.balances(self.reviewer)
		self.assertEqual(balances[Currency.T1_REVIEWS], 2)
		self.assertEqual(balances[Currency.LAPSE_HOURS], 3 * 60 * 60)

	def test_refunded_orders_give_the_work_back_but_denied_ones_do_not(self):
		self.t1s(10)
		item = reviewer_item(cost=4)
		Order.objects.create(owner=self.reviewer, item=item, status=Order.OrderStatus.REFUNDED, refunded=True)
		Order.objects.create(owner=self.reviewer, item=item, status=Order.OrderStatus.DENIED)
		self.assertEqual(reviewer_shop.balances(self.reviewer)[Currency.T1_REVIEWS], 6)

	def test_reviews_from_before_launch_do_not_count(self):
		self.t1s(5)
		T1.objects.update(reviewed_at=reviewer_shop.launched_at() - timedelta(minutes=1))
		self.t1s(2)
		self.assertEqual(reviewer_shop.balances(self.reviewer)[Currency.T1_REVIEWS], 2)

	def test_other_reviewers_work_is_not_counted(self):
		self.t1s(5, reviewer=make_user("other"))
		self.assertEqual(reviewer_shop.balances(self.reviewer)[Currency.T1_REVIEWS], 0)

	def test_lapse_hours_display_rounded_down_to_a_tenth(self):
		self.assertEqual(reviewer_shop.display_amount(Currency.LAPSE_HOURS, 3 * 3600 + 3599), "3.9")
		self.assertEqual(reviewer_shop.display_amount(Currency.LAPSE_HOURS, 12 * 3600), "12")
		self.assertEqual(reviewer_shop.display_amount(Currency.T2_REVIEWS, 4), "4")

	def test_reviewer_items_stay_off_the_public_shop(self):
		reviewer_item()
		self.assertFalse(shoppable().filter(kind=Item.Kind.REVIEWER).exists())


class ReviewerShopPageTests(ReviewWorkMixin, BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("rev", slack_id="U0REV"), "t1_review")
		self.client.force_login(self.reviewer)
		self.make_work()

	def test_needs_a_review_or_shop_permission(self):
		self.client.force_login(make_user("pleb"))
		self.assertNotEqual(self.client.get(reverse("reviewer_shop_dash")).status_code, 200)
		self.client.force_login(grant_perms(make_user("metrics_only"), "metrics"))
		self.assertNotEqual(self.client.get(reverse("reviewer_shop_dash")).status_code, 200)

	def test_shows_balances_and_items_but_no_management_to_reviewers(self):
		self.t1s(12)
		reviewer_item(name="Sticker pack", cost=5)
		reviewer_item(name="Retired", cost=5, deleted=True)
		response = self.client.get(reverse("reviewer_shop_dash"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(
			{card["label"]: card["value"] for card in response.context["stat_cards"]},
			{"Lapse review hours": "0", "T1 reviews": "12", "T2 reviews": "0"},
		)
		self.assertContains(response, "Sticker pack")
		self.assertNotContains(response, "Retired")
		self.assertNotContains(response, "Add item")

	def test_shopkeepers_see_the_management_controls(self):
		self.client.force_login(grant_perms(make_user("keeper"), "fulfillment"))
		reviewer_item()
		response = self.client.get(reverse("reviewer_shop_dash"))
		self.assertContains(response, "Add item")
		self.assertContains(response, "editReviewerItemModal")

	def test_nav_links_the_reviewer_shop(self):
		response = self.client.get(reverse("admin_dash"))
		self.assertContains(response, 'href="/root/reviewer-shop/"')


class BuyTests(ReviewWorkMixin, BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("rev", slack_id="U0REV", layers=100), "t1_review")
		self.client.force_login(self.reviewer)
		self.make_work()

	def _buy(self, item, **data):
		# Purchases are rate limited; each one here stands on its own.
		cache.clear()
		return self.client.post(reverse("buy_reviewer_item", args=[item.id]), {"quantity": "1", **data})

	def test_buying_places_a_pending_order_and_spends_reviews_not_pearls(self):
		self.t1s(10)
		item = reviewer_item(cost=4, stock=3)
		response = self._buy(item, quantity="2", user_notes="pretzels")
		self.assertRedirects(response, reverse("reviewer_shop_dash"), fetch_redirect_response=False)

		order = Order.objects.get()
		self.assertEqual(order.status, Order.OrderStatus.PENDING)
		self.assertEqual((order.quantity, order.cost, order.user_notes), (2, 4, "pretzels"))
		item.refresh_from_db()
		self.assertEqual(item.stock, 1)
		self.assertEqual(reviewer_shop.balances(self.reviewer)[Currency.T1_REVIEWS], 2)
		self.reviewer.hackclub_profile.refresh_from_db()
		self.assertEqual(self.reviewer.hackclub_profile.layers, 100)
		self.assertTrue(AuditLog.objects.filter(action="buy_reviewer_item").exists())

	def test_cannot_overspend(self):
		self.t1s(3)
		response = self._buy(reviewer_item(cost=4))
		self.assertFalse(Order.objects.exists())
		self.assertIn("costs 4 T1 reviews and you have 3 to spend", " ".join(message_texts(response)))

	def test_lapse_items_are_priced_in_hours(self):
		self.lapse_minutes(90)
		item = reviewer_item(cost=2, currency=Currency.LAPSE_HOURS)
		self._buy(item)
		self.assertFalse(Order.objects.exists())
		self.lapse_minutes(30)
		self._buy(item)
		self.assertTrue(Order.objects.exists())

	def test_cannot_buy_past_stock(self):
		self.t1s(10)
		self._buy(reviewer_item(cost=1, stock=1), quantity="2")
		self.assertFalse(Order.objects.exists())

	def test_cannot_buy_a_regular_or_deleted_item_here(self):
		self.t1s(10)
		regular = Item.objects.create(name="Pearl thing", description="x", cost=1)
		self.assertEqual(self._buy(regular).status_code, 404)
		self.assertEqual(self._buy(reviewer_item(cost=1, deleted=True)).status_code, 404)


class ManageItemTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.keeper = grant_perms(make_user("keeper"), "fulfillment")
		self.client.force_login(self.keeper)

	def _form(self, **overrides):
		return {
			"name": "$10 Snack Grant", "description": "buy snacks", "cost": "65",
			"review_currency": Currency.T1_REVIEWS, "stock": "-1", "imageUrl": "",
			**overrides,
		}

	def test_create(self):
		self.client.post(reverse("create_reviewer_item"), self._form())
		item = Item.objects.get(kind=Item.Kind.REVIEWER)
		self.assertEqual((item.cost, item.review_currency, item.stock), (65, Currency.T1_REVIEWS, -1))
		self.assertFalse(item.has_image)
		self.assertEqual(item.category, reviewer_shop.ITEM_CATEGORY)

	def test_create_rejects_a_bad_currency_or_cost(self):
		self.client.post(reverse("create_reviewer_item"), self._form(review_currency="pearls"))
		self.client.post(reverse("create_reviewer_item"), self._form(cost="0"))
		self.client.post(reverse("create_reviewer_item"), self._form(cost="lots"))
		self.assertFalse(Item.objects.filter(kind=Item.Kind.REVIEWER).exists())

	def test_edit_and_delete(self):
		item = reviewer_item()
		self.client.post(reverse("edit_reviewer_item", args=[item.id]), self._form(
			name="Hoodie", cost="40", review_currency=Currency.LAPSE_HOURS, stock="5",
		))
		item.refresh_from_db()
		self.assertEqual((item.name, item.cost, item.review_currency, item.stock), ("Hoodie", 40, Currency.LAPSE_HOURS, 5))

		self.client.post(reverse("delete_reviewer_item", args=[item.id]))
		item.refresh_from_db()
		self.assertTrue(item.deleted)

	def test_reviewers_cannot_manage_items(self):
		self.client.force_login(grant_perms(make_user("rev"), "t1_review", "timelapse_review"))
		item = reviewer_item()
		self.client.post(reverse("create_reviewer_item"), self._form(name="Free stuff"))
		self.client.post(reverse("edit_reviewer_item", args=[item.id]), self._form(cost="1"))
		self.client.post(reverse("delete_reviewer_item", args=[item.id]))
		item.refresh_from_db()
		self.assertEqual((item.cost, item.deleted), (10, False))
		self.assertFalse(Item.objects.filter(name="Free stuff").exists())
