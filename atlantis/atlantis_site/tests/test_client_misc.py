from django.urls import reverse

from .base import BaseTestCase, grant_perms, make_project, make_user


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
