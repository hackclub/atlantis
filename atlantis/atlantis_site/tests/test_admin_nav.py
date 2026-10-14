"""The pending counts in the admin rail.

The badges are the only thing on /root that claims a number without the page
that owns it being open, so what's tested here is that they can't drift from
those pages: same queue, same exclusions, and nothing shown for an empty desk.
"""

from unittest.mock import patch

from django.urls import reverse

from ..models import Item, Order, Ship
from ..templatetags import nav_badges
from ..templatetags.nav_badges import nav_badge, pending_count
from ..views.admin.queue import forget_pending_counts
from .base import (
	BaseTestCase,
	grant_perms,
	make_journal,
	make_project,
	make_ship,
	make_user,
)


class NavBadgeTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("boss"), "organizer")
		self.client.force_login(self.organizer)
		self.project = make_project(make_user("author"), shippable=True)

	def rail(self):
		return self.client.get(reverse("admin_dash")).content.decode()

	def test_counts_each_desk(self):
		make_ship(self.project)
		make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)
		make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)
		make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE)

		self.assertEqual(pending_count("t1"), 1)
		self.assertEqual(pending_count("t2"), 2)
		self.assertEqual(pending_count("t3"), 1)

	def test_badge_is_absent_when_a_desk_is_empty(self):
		self.assertEqual(nav_badge("t1"), "")
		self.assertNotIn("nav-badge", self.rail())

	def test_badge_renders_next_to_its_link(self):
		make_ship(self.project)
		self.assertIn('T1 review<span class="nav-badge" title="1 pending">1</span>', self.rail())

	def test_t1_badge_skips_ships_waiting_on_timelapse_review(self):
		"""Same exclusion review_dash makes: an unsigned-off ship isn't work yet."""
		make_ship(self.project, timelapse_approved=False)
		self.assertEqual(pending_count("t1"), 0)

	def test_lapse_badge_counts_projects_not_lapses(self):
		"""One project is one sitting at that desk, however many lapses it has."""
		make_journal(self.project)
		make_journal(self.project)
		make_journal(make_project(make_user("other"), shippable=True))

		self.assertEqual(pending_count("lookout"), 2)

	def test_fulfillment_badge_counts_only_pending_orders(self):
		item = Item.objects.create(name="Thing", description="x", cost=1)
		Order.objects.create(owner=self.organizer, item=item)
		Order.objects.create(owner=self.organizer, item=item)
		Order.objects.create(
			owner=self.organizer, item=item, status=Order.OrderStatus.FULFILLED
		)

		self.assertEqual(pending_count("fulfillment"), 2)
		self.assertIn('fulfillment<span class="nav-badge" title="2 pending">2</span>', self.rail())

	def test_large_counts_are_capped(self):
		item = Item.objects.create(name="Thing", description="x", cost=1)
		Order.objects.bulk_create(
			[Order(owner=self.organizer, item=item, cost=1) for _ in range(100)]
		)
		self.assertIn('title="100 pending">99+<', nav_badge("fulfillment"))

	def test_a_reviewer_sees_only_the_desks_they_hold(self):
		"""The rail renders badges inside the permission checks, so a T1-only
		reviewer never even counts the desks they can't open."""
		self.client.force_login(grant_perms(make_user("t1rev"), "t1_review"))
		make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)

		rail = self.rail()
		self.assertIn("T1 review", rail)
		self.assertNotIn("T2 review", rail)
		self.assertNotIn("nav-badge", rail)


class NavBadgeCacheTests(BaseTestCase):
	"""The rail is on every admin page, so its counts are cached briefly —
	but never across a decision the reviewer looking at it just made."""

	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(make_user("boss"), "organizer")
		self.client.force_login(self.organizer)
		self.project = make_project(make_user("author"), shippable=True)

	def rail(self):
		return self.client.get(reverse("admin_dash")).content.decode()

	def test_counts_are_reused_within_the_window(self):
		self.assertNotIn("nav-badge", self.rail())
		make_ship(self.project)
		# Somebody else's ship: up to PENDING_COUNT_TTL late, by design.
		self.assertNotIn("nav-badge", self.rail())
		forget_pending_counts()
		self.assertIn('T1 review<span class="nav-badge" title="1 pending">1</span>', self.rail())

	def test_a_warm_rail_counts_nothing(self):
		self.rail()
		with patch.object(nav_badges, "pending_count") as count:
			self.rail()
		count.assert_not_called()

	def test_moving_on_from_a_decision_recounts(self):
		ship = make_ship(self.project)
		self.assertIn('title="1 pending"', self.rail())
		Ship.objects.filter(pk=ship.pk).update(status=Ship.ShipStatus.T2_QUEUE)
		# Every decision view finishes in go_to_next; "next" is the bare path
		# through it.
		self.client.get(reverse("review_next"))
		rail = self.rail()
		self.assertNotIn('T1 review<span class="nav-badge"', rail)
		self.assertIn('T2 review<span class="nav-badge" title="1 pending">1</span>', rail)

	def test_an_order_status_change_recounts_fulfillment(self):
		item = Item.objects.create(name="Thing", description="x", cost=1)
		order = Order.objects.create(owner=self.organizer, item=item)
		self.assertIn('fulfillment<span class="nav-badge" title="1 pending">1</span>', self.rail())
		self.client.post(reverse("update_order_status", args=[order.id]), {"action": "fulfilled"})
		self.assertNotIn('fulfillment<span class="nav-badge"', self.rail())
