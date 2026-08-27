import os
from unittest.mock import patch

from django.http import HttpResponseRedirect
from django.test import TestCase, override_settings
from django.urls import reverse

from ..models import Profile
from ..views.client.auth import FORCE_REAUTH_COOKIE
from .base import TEST_ENCRYPTION_KEY, User, make_user

TOKEN = {
	"access_token": "at-123",
	"refresh_token": "rt-456",
	"token_type": "Bearer",
	"expires_at": 1893456000,
	"id_token": "jwt-not-worth-keeping",
}

USERINFO = {
	"sub": "user!abc123",
	"email": "tester@example.com",
	"name": "Test Person",
	"given_name": "Test",
	"family_name": "Person",
	"slack_id": "U0SLACK",
	"verification_status": "verified",
	"ysws_eligible": True,
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


@override_settings(ADDRESS_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
@patch.dict(os.environ, {"DEFAULT_PFP": "https://example.com/default.png"})
@patch("atlantis_site.views.client.auth.slack_client.users_info")
@patch("atlantis_site.views.client.auth.oauth.hackclub.authorize_access_token")
class AuthCallbackTests(TestCase):
	def _callback(self, mock_token, userinfo=None, token=None):
		mock_token.return_value = {
			"userinfo": userinfo or dict(USERINFO),
			**(token if token is not None else TOKEN),
		}
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
		self.assertTrue(profile.ysws_eligible)

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

	def test_stores_a_refusal_from_hca(self, mock_token, mock_slack):
		"""Whatever HCA says goes on the profile — the create/ship gate reads it
		back from there."""
		mock_slack.return_value = SLACK_USER_RESPONSE
		self._callback(
			mock_token,
			userinfo={**USERINFO, "verification_status": "ineligible", "ysws_eligible": False},
		)

		profile = User.objects.get(username="user_abc123").hackclub_profile
		self.assertEqual(profile.verification_status, "ineligible")
		self.assertFalse(profile.ysws_eligible)
		self.assertFalse(profile.is_ysws_eligible)

	def test_missing_verification_claims_store_no_verdict(self, mock_token, mock_slack):
		"""A token issued before the claim scope carries neither claim, which is
		not the same as a no — but it is not a yes either, so the gate holds."""
		mock_slack.return_value = SLACK_USER_RESPONSE
		userinfo = {k: v for k, v in USERINFO.items()
					if k not in ("verification_status", "ysws_eligible")}
		self._callback(mock_token, userinfo=userinfo)

		profile = User.objects.get(username="user_abc123").hackclub_profile
		self.assertEqual(profile.verification_status, "")
		self.assertIsNone(profile.ysws_eligible)
		self.assertFalse(profile.is_ysws_eligible)

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

	def test_stores_encrypted_token_and_no_address(self, mock_token, mock_slack):
		mock_slack.return_value = SLACK_USER_RESPONSE
		self._callback(mock_token, userinfo={**USERINFO, "addresses": [{"id": "adr_1", "line_1": "15 Falls Rd"}]})

		profile = User.objects.get(username="user_abc123").hackclub_profile
		self.assertNotIn("at-123", profile.encrypted_hca_token)
		self.assertNotIn("15 Falls Rd", profile.encrypted_hca_token)
		# The id_token is single-use, so it is not worth carrying around.
		self.assertEqual(profile.get_hca_token(), {
			"access_token": "at-123",
			"refresh_token": "rt-456",
			"token_type": "Bearer",
			"expires_at": 1893456000,
		})

	def test_signin_without_usable_token_keeps_the_stored_one(self, mock_token, mock_slack):
		mock_slack.return_value = SLACK_USER_RESPONSE
		self._callback(mock_token)
		self.client.post(reverse("logout"))

		self._callback(mock_token, token={})

		profile = User.objects.get(username="user_abc123").hackclub_profile
		self.assertEqual(profile.get_hca_token()["access_token"], "at-123")
