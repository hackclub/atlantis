"""A reviewer can't open or decide on their own project at T1, T2 or Lookout."""

from django.urls import reverse

from ..models import Ship, T1, T2, TimelapseReview
from .base import (
	BaseTestCase,
	grant_perms,
	make_journal,
	make_project,
	make_ship,
	make_user,
	message_texts,
	t1_checklist,
)

OWN = "You can't review your own project."


class OwnProjectReviewTests(BaseTestCase):

	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(
			make_user("rev"), "t1_review", "t2_review", "timelapse_review",
		)
		self.client.force_login(self.reviewer)
		self.own = make_project(self.reviewer, shippable=True)
		self.other = make_project(make_user("author"), shippable=True)

	def test_t1_page_and_decision_refused(self):
		ship = make_ship(self.own)
		response = self.client.get(reverse("review_project", args=[ship.id]), follow=True)
		self.assertIn(OWN, message_texts(response))

		self.client.post(
			reverse("t1_decision", args=[ship.id]),
			{"approved": "approved", "feedback": "", **t1_checklist()},
		)
		self.assertFalse(T1.objects.exists())
		ship.refresh_from_db()
		self.assertEqual(ship.status, Ship.ShipStatus.T1_QUEUE)

	def test_t1_next_skips_own_ship(self):
		make_ship(self.own)
		theirs = make_ship(self.other)
		response = self.client.get(reverse("review_next"))
		self.assertRedirects(
			response, reverse("review_project", args=[theirs.id]), fetch_redirect_response=False,
		)

	def test_t2_page_and_decision_refused(self):
		ship = make_ship(self.own, status=Ship.ShipStatus.T2_QUEUE)
		response = self.client.get(reverse("ysws_review_project", args=[ship.id]), follow=True)
		self.assertIn(OWN, message_texts(response))

		self.client.post(reverse("t2_decision", args=[ship.id]), {"decision": T2.Decision.APPROVE})
		self.assertFalse(T2.objects.exists())
		ship.refresh_from_db()
		self.assertEqual(ship.status, Ship.ShipStatus.T2_QUEUE)

	def test_t2_next_with_only_own_ship_returns_to_desk(self):
		make_ship(self.own, status=Ship.ShipStatus.T2_QUEUE)
		response = self.client.get(reverse("ysws_review_next"))
		self.assertRedirects(response, reverse("ysws_review_dash"), fetch_redirect_response=False)

	def test_lookout_page_and_decision_refused(self):
		make_journal(self.own)
		response = self.client.get(
			reverse("timelapse_review_project", args=[self.own.id]), follow=True,
		)
		self.assertIn(OWN, message_texts(response))

		self.client.post(reverse("timelapse_decision", args=[self.own.id]), {"internal_notes": "x"})
		self.assertFalse(TimelapseReview.objects.exists())

	def test_lookout_next_skips_own_project(self):
		make_journal(self.own)
		make_journal(self.other)
		response = self.client.get(reverse("timelapse_review_next"))
		self.assertRedirects(
			response, reverse("timelapse_review_project", args=[self.other.id]),
			fetch_redirect_response=False,
		)
