import os
from unittest.mock import patch

from django.http import HttpResponseRedirect
from django.test import TestCase
from django.urls import reverse

from ..models import Profile
from ..views.client.auth import FORCE_REAUTH_COOKIE
from .base import User, make_user

USERINFO = {
	"sub": "user!abc123",
	"email": "tester@example.com",
	"name": "Test Person",
	"given_name": "Test",
	"family_name": "Person",
	"slack_id": "U0SLACK",
	"verification_status": "verified",
}

SLACK_USER_RESPONSE = {
	"user": {
		"profile": {
			"display_name": "slack-tester",
			"real_name": "Test Person",
			"image_512": "https://cdn.slack.example/pfp.png",
		}
	}
}


class LoginViewTests(TestCase):
	def test_get_not_allowed(self):
		self.assertEqual(self.client.get(reverse("login")).status_code, 405)

	def test_authenticated_user_redirected_to_dashboard(self):
		self.client.force_login(make_user())
		response = self.client.post(reverse("login"))
		self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

	@patch("atlantis_site.views.client.auth.oauth.hackclub.authorize_redirect")
	def test_anonymous_user_sent_to_oauth_provider(self, mock_authorize):
		mock_authorize.return_value = HttpResponseRedirect("https://auth.hackclub.com/authorize")
		response = self.client.post(reverse("login"))
		self.assertEqual(response.status_code, 302)
		mock_authorize.assert_called_once()
		self.assertNotIn("prompt", mock_authorize.call_args.kwargs)

	@patch("atlantis_site.views.client.auth.oauth.hackclub.authorize_redirect")
	def test_force_reauth_cookie_adds_login_prompt(self, mock_authorize):
		mock_authorize.return_value = HttpResponseRedirect("https://auth.hackclub.com/authorize")
		self.client.cookies[FORCE_REAUTH_COOKIE] = "1"
		self.client.post(reverse("login"))
		self.assertEqual(mock_authorize.call_args.kwargs.get("prompt"), "login")


class LogoutViewTests(TestCase):
	def test_get_not_allowed(self):
		self.assertEqual(self.client.get(reverse("logout")).status_code, 405)

	def test_logout_clears_session_and_sets_reauth_cookie(self):
		self.client.force_login(make_user())
		response = self.client.post(reverse("logout"))
		self.assertRedirects(response, "/", fetch_redirect_response=False)
		self.assertEqual(response.cookies[FORCE_REAUTH_COOKIE].value, "1")
		self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)


@patch.dict(os.environ, {"DEFAULT_PFP": "https://example.com/default.png"})
@patch("atlantis_site.views.client.auth.slack_client.users_info")
@patch("atlantis_site.views.client.auth.oauth.hackclub.authorize_access_token")
class AuthCallbackTests(TestCase):
	def _callback(self, mock_token, userinfo=None):
		mock_token.return_value = {"userinfo": userinfo or dict(USERINFO)}
		return self.client.get(reverse("auth_callback"))

	def test_creates_user_and_profile(self, mock_token, mock_slack):
		mock_slack.return_value = SLACK_USER_RESPONSE
		response = self._callback(mock_token)

		self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
		user = User.objects.get(username="user_abc123")
		self.assertEqual(user.email, "tester@example.com")
		self.assertEqual(user.first_name, "Test")
		self.assertEqual(user.last_name, "Person")

		profile = user.hackclub_profile
		self.assertEqual(profile.slack_id, "U0SLACK")
		self.assertEqual(profile.slack_username, "slack-tester")
		self.assertEqual(profile.slack_pfp_url, "https://cdn.slack.example/pfp.png")
		self.assertEqual(profile.verification_status, "verified")

	def test_logs_user_in(self, mock_token, mock_slack):
		mock_slack.return_value = SLACK_USER_RESPONSE
		self._callback(mock_token)
		self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

	def test_existing_user_not_duplicated_and_profile_updated(self, mock_token, mock_slack):
		mock_slack.return_value = SLACK_USER_RESPONSE
		user = User.objects.create_user(username="user_abc123", email="old@example.com")
		Profile.objects.create(user=user, slack_username="old-name", layers=42)

		self._callback(mock_token)

		self.assertEqual(User.objects.filter(username="user_abc123").count(), 1)
		profile = Profile.objects.get(user=user)
		self.assertEqual(profile.slack_username, "slack-tester")
		self.assertEqual(profile.layers, 42)
		user.refresh_from_db()
		self.assertEqual(user.email, "old@example.com")

	def test_slack_fetch_failure_falls_back_to_oidc_name_and_default_pfp(self, mock_token, mock_slack):
		mock_slack.side_effect = Exception("slack down")
		self._callback(mock_token)

		profile = User.objects.get(username="user_abc123").hackclub_profile
		self.assertEqual(profile.slack_username, "Test Person")
		self.assertEqual(profile.slack_pfp_url, "https://example.com/default.png")

	def test_slack_display_name_falls_back_to_real_name(self, mock_token, mock_slack):
		mock_slack.return_value = {
			"user": {"profile": {"display_name": "", "real_name": "Real Name", "image_512": "https://x/pfp.png"}}
		}
		self._callback(mock_token)
		profile = User.objects.get(username="user_abc123").hackclub_profile
		self.assertEqual(profile.slack_username, "Real Name")
