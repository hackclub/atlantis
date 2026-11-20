"""The reviewer streak badge and the celebration after each decision."""

import json
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from ..views.admin.streak import SESSION_KEY, STREAK_GAP, streak_tier
from .base import BaseTestCase, grant_perms, make_project, make_ship, make_user, t1_checklist


def celebration(response):
	"""The payload celebrate.js would play on this page, or None."""
	html = response.content.decode()
	marker = '<script id="review-celebration" type="application/json">'
	if marker not in html:
		return None
	return json.loads(html.split(marker, 1)[1].split("</script>", 1)[0])


class StreakTierTests(BaseTestCase):
	def test_badge_warms_then_catches_fire(self):
		self.assertIsNone(streak_tier(0))
		self.assertIsNone(streak_tier(1))
		self.assertEqual(streak_tier(2), "warm")
		self.assertEqual(streak_tier(3), "fire")
		self.assertEqual(streak_tier(10), "inferno")


class ReviewStreakTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t1rev"), "t1_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)
		self.ships = [make_ship(self.project) for _ in range(5)]

	def _decide(self, ship, verdict="approved"):
		data = {"feedback": "nice", "internal_notes": "", "approved": verdict}
		if verdict == "approved":
			data.update(t1_checklist())
		return self.client.post(reverse("t1_decision", args=[ship.id]), data, follow=True)

	def test_approval_gets_confetti_once(self):
		response = self._decide(self.ships[0])
		self.assertEqual(
			celebration(response),
			{"approved": True, "streak": 1, "onFire": False, "milestone": False},
		)
		# Only on the page the decision led to, not every one after.
		again = self.client.get(response.request["PATH_INFO"])
		self.assertIsNone(celebration(again))

	def test_rejection_gets_no_confetti(self):
		response = self._decide(self.ships[0], verdict="denied")
		self.assertFalse(celebration(response)["approved"])

	def test_streak_builds_and_catches_fire(self):
		self._decide(self.ships[0])
		second = self._decide(self.ships[1], verdict="changes")
		self.assertContains(second, "rv-streak--warm")
		third = self._decide(self.ships[2], verdict="denied")
		self.assertContains(third, "rv-streak--fire")
		self.assertContains(third, 'data-review-streak="3"')
		self.assertTrue(celebration(third)["onFire"])

	def test_first_decision_shows_no_badge(self):
		self.assertNotContains(self._decide(self.ships[0]), "data-review-streak")

	def test_stopping_breaks_the_streak(self):
		self._decide(self.ships[0])
		self._decide(self.ships[1])
		session = self.client.session
		session[SESSION_KEY] = {
			"count": 2,
			"last": (timezone.now() - STREAK_GAP - timedelta(minutes=1)).isoformat(),
		}
		session.save()
		self.assertEqual(celebration(self._decide(self.ships[2]))["streak"], 1)

	def test_milestone_is_called_out(self):
		session = self.client.session
		session[SESSION_KEY] = {"count": 4, "last": timezone.now().isoformat()}
		session.save()
		payload = celebration(self._decide(self.ships[0]))
		self.assertEqual(payload["streak"], 5)
		self.assertTrue(payload["milestone"])
