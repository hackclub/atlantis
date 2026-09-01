from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse

from .. import lapse
from .base import (
	BaseTestCase, connect_lapse, grant_perms, lapse_payload, make_project,
	make_timelapse, make_user,
)

User = get_user_model()


class IndexAndDashboardTests(BaseTestCase):
	def test_index_public(self):
		self.assertEqual(self.client.get(reverse("index")).status_code, 200)

	def test_dashboard_requires_login(self):
		response = self.client.get(reverse("dashboard"))
		self.assertEqual(response.status_code, 302)

	def test_dashboard_renders_for_logged_in_user(self):
		user = make_user()
		self.client.force_login(user)
		response = self.client.get(reverse("dashboard"))
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.context["profile"], user.hackclub_profile)


class UserProfileTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.owner = make_user("owner")
		self.visitor = make_user("visitor")
		self.public_project = make_project(self.owner, title="Public")
		self.locked_project = make_project(self.owner, title="Locked", locked=True)
		make_project(self.owner, title="Deleted", deleted=True)

	def _profile(self, user):
		return self.client.get(reverse("user_profile", args=[user.id]))

	def test_login_required(self):
		self.assertEqual(self._profile(self.owner).status_code, 302)

	def test_unknown_user_404(self):
		self.client.force_login(self.visitor)
		self.assertEqual(self.client.get(reverse("user_profile", args=[99999])).status_code, 404)

	def test_visitor_sees_only_unlocked_projects(self):
		self.client.force_login(self.visitor)
		response = self._profile(self.owner)
		self.assertEqual(list(response.context["projects"]), [self.public_project])
		self.assertFalse(response.context["is_self"])

	def test_owner_sees_own_locked_projects(self):
		self.client.force_login(self.owner)
		response = self._profile(self.owner)
		self.assertEqual(
			list(response.context["projects"]), [self.public_project, self.locked_project]
		)
		self.assertTrue(response.context["is_self"])

	def test_organizer_sees_locked_projects(self):
		organizer = grant_perms(make_user("organizer"), "organizer")
		self.client.force_login(organizer)
		response = self._profile(self.owner)
		self.assertEqual(
			list(response.context["projects"]), [self.public_project, self.locked_project]
		)

	def test_deleted_projects_never_shown(self):
		self.client.force_login(self.owner)
		titles = [p.title for p in self._profile(self.owner).context["projects"]]
		self.assertNotIn("Deleted", titles)

	def test_logout_button_only_on_own_profile(self):
		self.client.force_login(self.owner)
		self.assertContains(self._profile(self.owner), reverse("logout"))
		self.assertNotContains(self._profile(self.visitor), reverse("logout"))


class LapseFetchErrorTests(BaseTestCase):
	"""The picker must not relay Lapse's own error text to the client.

	LapseError messages embed the endpoint and up to 500 characters of the
	upstream response body, so they belong in the log, not the response.
	"""

	INTERNAL_DETAIL = (
		"Lapse timelapse/myPublishedTimelapses returned 500: "
		"{\"trace\": \"secret-internal-detail\"}"
	)

	def setUp(self):
		super().setUp()
		cache.clear()
		self.owner = make_user("recorder")
		self.project = make_project(self.owner)
		connect_lapse(self.owner)
		self.client.force_login(self.owner)

	def _list(self):
		return self.client.get(reverse("lapse_timelapses", args=[self.project.id]))

	@patch("atlantis_site.lapse.fetch_published_timelapses")
	def test_upstream_detail_is_not_returned(self, mock_fetch):
		mock_fetch.side_effect = lapse.LapseError(self.INTERNAL_DETAIL)
		response = self._list()
		self.assertEqual(response.status_code, 502)
		payload = response.json()
		self.assertFalse(payload["ok"])
		self.assertNotIn("secret-internal-detail", response.content.decode())
		self.assertEqual(payload["error"], "Couldn't reach Lapse right now.")

	@patch("atlantis_site.lapse.fetch_published_timelapses")
	def test_upstream_detail_is_logged(self, mock_fetch):
		mock_fetch.side_effect = lapse.LapseError(self.INTERNAL_DETAIL)
		with self.assertLogs("atlantis_site.views.client.lapse", level="WARNING") as logs:
			self._list()
		self.assertIn("secret-internal-detail", "\n".join(logs.output))

	@patch("atlantis_site.lapse.fetch_published_timelapses")
	def test_a_rejected_token_reads_as_disconnected(self, mock_fetch):
		"""There is no refresh grant, so the only fix is the shipper's."""
		mock_fetch.side_effect = lapse.LapseAuthError("nope")
		payload = self._list().json()
		self.assertTrue(payload["ok"])
		self.assertFalse(payload["connected"])
		self.assertTrue(payload["expired"])

	@patch("atlantis_site.lapse.fetch_published_timelapses")
	def test_successful_fetch_lists_what_can_be_picked(self, mock_fetch):
		mock_fetch.return_value = [lapse_payload("aaa", minutes=90)]
		payload = self._list().json()
		self.assertTrue(payload["connected"])
		self.assertEqual(len(payload["timelapses"]), 1)
		entry = payload["timelapses"][0]
		self.assertEqual(entry["id"], "aaa")
		self.assertEqual(entry["state"], "available")
		# duration is recorded seconds, straight through.
		self.assertEqual(entry["trackedSeconds"], 90 * 60)
		self.assertEqual(entry["trackedDisplay"], "1h 30m")
		self.assertEqual(entry["watchUrl"], "https://lapse.hackclub.com/timelapse/aaa")

	@patch("atlantis_site.lapse.fetch_published_timelapses")
	def test_footage_already_taped_in_is_listed_but_not_pickable(self, mock_fetch):
		make_timelapse(self.project, lapse_id="aaa")
		mock_fetch.return_value = [lapse_payload("aaa")]
		entry = self._list().json()["timelapses"][0]
		self.assertEqual(entry["state"], "attached")

	@patch("atlantis_site.lapse.fetch_published_timelapses")
	def test_unprocessed_footage_is_listed_but_not_pickable(self, mock_fetch):
		mock_fetch.return_value = [
			lapse_payload("aaa", playbackUrl=None),
			lapse_payload("bbb", visibility="FAILED_PROCESSING"),
		]
		states = [entry["state"] for entry in self._list().json()["timelapses"]]
		self.assertEqual(states, ["processing", "failed"])

	def test_a_user_with_no_connection_is_told_so(self):
		self.client.force_login(make_user("unconnected"))
		project = make_project(User.objects.get(username="unconnected"))
		payload = self.client.get(
			reverse("lapse_timelapses", args=[project.id])
		).json()
		self.assertTrue(payload["ok"])
		self.assertFalse(payload["connected"])

	def test_another_users_project_is_404(self):
		self.client.force_login(make_user("stranger"))
		self.assertEqual(self._list().status_code, 404)

