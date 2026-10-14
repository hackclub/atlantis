"""Form views answering atlantis.js in JSON (views.helpers.fetch_aware).

The same views a browser posts to and gets a redirect from; asked by script
(X-Requested-With: XMLHttpRequest), they answer whether it worked and where
to go, so an upload that fails leaves the page — and the files picked on it —
where it was.
"""
from django.conf import settings
from django.core.cache import cache
from django.shortcuts import resolve_url
from django.urls import reverse

from ..models import Project
from .base import (
	VALID_EDITOR_LINK,
	BaseTestCase,
	make_project,
	make_user,
)

FETCH = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


class FetchAwareTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		cache.clear()
		self.user = make_user("fetcher")
		self.client.force_login(self.user)

	def _create(self, headers=FETCH, **overrides):
		data = {"title": "New Project", "description": "Something cool.", "printables_url": ""}
		data.update(overrides)
		return self.client.post(reverse("create_project"), data, **headers)

	def test_a_browser_still_gets_the_redirect(self):
		response = self._create(headers={})
		self.assertRedirects(response, reverse("projects"))

	def test_success_answers_with_where_to_go(self):
		response = self._create()
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"ok": True, "redirect": reverse("projects")})
		self.assertEqual(Project.objects.count(), 1)

	def test_failure_answers_with_the_messages(self):
		response = self._create(title="   ")
		self.assertEqual(response.status_code, 400)
		body = response.json()
		self.assertFalse(body["ok"])
		self.assertEqual(body["messages"], [{"level": "error", "text": "Title is required."}])
		self.assertEqual(Project.objects.count(), 0)

	def test_a_failure_said_by_script_is_not_said_again(self):
		self._create(title="   ")
		page = self.client.get(reverse("projects"))
		self.assertNotContains(page, "Title is required.")

	def test_a_success_message_waits_for_the_next_page(self):
		project = make_project(self.user)
		response = self.client.post(
			reverse("update_editor_model", args=[project.id]),
			{"editor_model_link": VALID_EDITOR_LINK},
			**FETCH,
		)
		self.assertTrue(response.json()["ok"])
		page = self.client.get(response.json()["redirect"])
		self.assertContains(page, "Editor model updated successfully.")

	def test_rate_limit_answers_in_json(self):
		self._create()
		response = self._create()
		self.assertEqual(response.status_code, 429)
		body = response.json()
		self.assertFalse(body["ok"])
		self.assertEqual(body["error"], "rate_limited")
		self.assertTrue(body["messages"])

	def test_logged_out_is_sent_to_log_in(self):
		self.client.logout()
		response = self._create()
		body = response.json()
		self.assertTrue(body["ok"])
		self.assertTrue(body["redirect"].startswith(resolve_url(settings.LOGIN_URL)))


class BannerTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		cache.clear()
		self.user = make_user("reader")
		self.client.force_login(self.user)

	def test_errors_are_marked_as_errors(self):
		self.client.post(reverse("create_project"), {"title": "", "description": "x"})
		page = self.client.get(reverse("projects"))
		self.assertContains(page, '<p class="message is-bad" role="alert">Title is required.</p>', html=True)

	def test_every_page_loads_the_shared_script(self):
		for name in ("dashboard", "projects", "explore", "shop"):
			with self.subTest(page=name):
				self.assertContains(self.client.get(reverse(name)), "atlantis_site/atlantis.js")
