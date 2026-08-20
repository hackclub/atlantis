from unittest.mock import patch

from django.core.cache import cache
from django.urls import reverse

from .. import lookout
from .base import BaseTestCase, grant_perms, make_project, make_timelapse, make_user


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


class LookoutSyncErrorTests(BaseTestCase):
	"""sync_timelapse must not relay Lookout's own error text to the client.

	LookoutError messages embed the internal endpoint and up to 500 characters
	of the upstream response body, so they belong in the log, not the response.
	"""

	INTERNAL_DETAIL = (
		"Lookout GET https://lookout.internal/api/v1/internal/sessions/abc "
		"returned 500: {\"trace\": \"secret-internal-detail\"}"
	)

	def setUp(self):
		super().setUp()
		cache.clear()
		self.owner = make_user("recorder")
		self.project = make_project(self.owner)
		self.session = make_timelapse(self.project)
		self.client.force_login(self.owner)

	def _sync(self):
		return self.client.post(reverse("sync_timelapse", args=[self.session.pk]))

	@patch("atlantis_site.views.client.timelapse.lookout.get_internal_session")
	def test_upstream_detail_is_not_returned(self, mock_get):
		mock_get.side_effect = lookout.LookoutError(self.INTERNAL_DETAIL)
		response = self._sync()
		self.assertEqual(response.status_code, 502)
		payload = response.json()
		self.assertFalse(payload["ok"])
		self.assertNotIn("secret-internal-detail", response.content.decode())
		self.assertNotIn("lookout.internal", response.content.decode())
		self.assertEqual(payload["error"], "Could not reach Lookout right now.")

	@patch("atlantis_site.views.client.timelapse.lookout.get_internal_session")
	def test_upstream_detail_is_logged(self, mock_get):
		mock_get.side_effect = lookout.LookoutError(self.INTERNAL_DETAIL)
		with self.assertLogs("atlantis_site.views.client.timelapse", level="WARNING") as logs:
			self._sync()
		self.assertIn("secret-internal-detail", "\n".join(logs.output))

	@patch("atlantis_site.views.client.timelapse.lookout.get_internal_session")
	def test_successful_sync_still_reports_state(self, mock_get):
		mock_get.return_value = {
			"session": {"status": "complete", "totalActiveSeconds": 120},
			"trackedSeconds": 3600,
			"screenshotCount": 4,
		}
		payload = self._sync().json()
		self.assertTrue(payload["ok"])
		self.assertEqual(payload["trackedSeconds"], 3600)

	def test_other_users_session_is_404(self):
		self.client.force_login(make_user("stranger"))
		self.assertEqual(self._sync().status_code, 404)
