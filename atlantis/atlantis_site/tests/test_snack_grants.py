from datetime import timedelta

from django.urls import reverse

from .. import snack_grants
from ..models import Item, Order, Ship, SnackGrant, T1, T2, TimelapseReview
from ..views.client.shop import shoppable
from .base import (
	BaseTestCase, grant_perms, make_journal, make_project, make_ship, make_user,
	t1_checklist,
)


class SnackGrantAwardTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = make_user("reviewer", slack_id="U0REV")
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)
		self.ship = Ship.objects.create(project=self.project)

	def _t1s(self, n):
		T1.objects.bulk_create([
			T1(ship=self.ship, reviewer=self.reviewer, feedback="", internal_notes="", approved=True)
			for _ in range(n)
		])

	def _t2s(self, n):
		T2.objects.bulk_create([
			T2(ship=self.ship, reviewer=self.reviewer, feedback="", justification="")
			for _ in range(n)
		])

	def _lapses(self, *hours):
		for h in hours:
			journal = make_journal(self.project, time_spent=h * 60)
			TimelapseReview.objects.create(journal=journal, reviewer=self.reviewer)

	def test_nothing_below_any_threshold(self):
		self._t1s(64)
		self._t2s(24)
		self._lapses(99)
		self.assertEqual(snack_grants.award_snack_grants(self.reviewer), [])
		self.assertFalse(Order.objects.exists())

	def test_65_t1_reviews_earn_a_pending_zero_cost_order(self):
		self._t1s(65)
		[grant] = snack_grants.award_snack_grants(self.reviewer)

		self.assertEqual(grant.desk, SnackGrant.Desk.T1)
		order = grant.order
		self.assertEqual(order.owner, self.reviewer)
		self.assertEqual(order.status, Order.OrderStatus.PENDING)
		self.assertEqual(order.cost, 0)
		self.assertEqual(order.item.kind, Item.Kind.SNACK_GRANT)
		self.assertIn("65 T1", order.admin_notes)
		self.slack_dm_mocks["snack_grants"].assert_called_once()

	def test_25_t2_reviews_earn_one(self):
		self._t2s(25)
		[grant] = snack_grants.award_snack_grants(self.reviewer)
		self.assertEqual(grant.desk, SnackGrant.Desk.T2)

	def test_100_hours_of_lapses_earn_one(self):
		self._lapses(60, 40)
		[grant] = snack_grants.award_snack_grants(self.reviewer)
		self.assertEqual(grant.desk, SnackGrant.Desk.LAPSE)

	def test_desks_are_counted_separately_and_can_pay_several(self):
		self._t1s(130)
		self._t2s(25)
		grants = snack_grants.award_snack_grants(self.reviewer)
		self.assertEqual(sorted(g.desk for g in grants), ["t1", "t1", "t2"])
		self.assertEqual(Order.objects.filter(owner=self.reviewer).count(), 3)

	def test_awarding_again_issues_nothing_new(self):
		self._t1s(65)
		snack_grants.award_snack_grants(self.reviewer)
		self._t1s(64)
		self.assertEqual(snack_grants.award_snack_grants(self.reviewer), [])
		self._t1s(1)
		self.assertEqual(len(snack_grants.award_snack_grants(self.reviewer)), 1)
		self.assertEqual(SnackGrant.objects.count(), 2)

	def test_reviews_from_before_launch_do_not_count(self):
		self._t1s(65)
		self._lapses(100)
		before = snack_grants.launched_at() - timedelta(minutes=1)
		T1.objects.update(reviewed_at=before)
		TimelapseReview.objects.update(reviewed_at=before)

		self.assertEqual(snack_grants.award_snack_grants(self.reviewer), [])
		self._t1s(65)
		self.assertEqual(len(snack_grants.award_snack_grants(self.reviewer)), 1)

	def test_other_reviewers_work_is_not_counted(self):
		other = make_user("other", slack_id="U0OTHER")
		T1.objects.bulk_create([
			T1(ship=self.ship, reviewer=other, feedback="", internal_notes="", approved=True)
			for _ in range(65)
		])
		self.assertEqual(snack_grants.award_snack_grants(self.reviewer), [])

	def test_the_grant_item_stays_off_the_shop(self):
		self._t1s(65)
		snack_grants.award_snack_grants(self.reviewer)
		self.assertFalse(shoppable().filter(kind=Item.Kind.SNACK_GRANT).exists())


class SnackGrantViewTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("rev", slack_id="U0REV"), "t1_review", "t2_review")
		self.client.force_login(self.reviewer)
		self.author = make_user("author", slack_id="U0AUTHOR")
		self.project = make_project(self.author, shippable=True)

	def test_the_65th_t1_decision_issues_a_grant(self):
		old = Ship.objects.create(project=self.project)
		T1.objects.bulk_create([
			T1(ship=old, reviewer=self.reviewer, feedback="", internal_notes="", approved=True)
			for _ in range(64)
		])
		ship = make_ship(self.project)
		self.client.post(reverse("t1_decision", args=[ship.id]), {
			"feedback": "nice", "internal_notes": "ok", "approved": "approved", **t1_checklist(),
		})
		self.assertEqual(T1.objects.filter(reviewer=self.reviewer).count(), 65)
		self.assertEqual(SnackGrant.objects.get().desk, SnackGrant.Desk.T1)

	def test_the_25th_t2_decision_issues_a_grant(self):
		old = Ship.objects.create(project=self.project)
		T2.objects.bulk_create([
			T2(ship=old, reviewer=self.reviewer, feedback="", justification="")
			for _ in range(24)
		])
		ship = make_ship(self.project, status=Ship.ShipStatus.T2_QUEUE)
		self.client.post(reverse("t2_decision", args=[ship.id]), {
			"decision": T2.Decision.APPROVE, "deductions": "0",
			"feedback": "good", "justification": "solid work",
		})
		self.assertEqual(SnackGrant.objects.get().desk, SnackGrant.Desk.T2)
