"""The Lapse client and the authorization that gets it a token.

Three things here are worth more than the rest.

The unit of `duration`. Lapse reports the *recorded* time that went into a
timelapse, and the compiled video runs sixty times faster — the live API returns
720 for a video that is twelve seconds long. Reading it as a video length would
multiply every shipper's hours by sixty, so the arithmetic is pinned down in
both directions.

PKCE and `state`. The redirect URI is a page anyone can navigate to with a
query string of their choosing, so what makes a code ours is the state in the
session, and what makes the token call ours is a verifier that never went
through the browser. Both are tested for the case where they don't match.

The envelope. A failed Lapse call still comes back HTTP 200 with
`{"ok": false}`, so a client that trusted the status code would read a refusal
as a result.
"""

import base64
import hashlib
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from .. import lapse
from ..models import LapseAccount, Timelapse, tracked_to_video
from .base import (
	BaseTestCase, connect_lapse, lapse_payload, make_project, make_timelapse,
	make_user,
)

LAPSE_SETTINGS = dict(
	LAPSE_CLIENT_ID="atlantis-test-client",
	LAPSE_API_BASE_URL="https://api.lapse.example/api",
	LAPSE_WEB_BASE_URL="https://lapse.example",
	LAPSE_REDIRECT_URI="https://atlantis.example/projects/",
)


def envelope(data):
	return {"ok": True, "data": data}


class FakeResponse:
	"""Just enough of requests.Response for the client to read."""

	def __init__(self, payload, status_code=200):
		self._payload = payload
		self.status_code = status_code
		self.ok = 200 <= status_code < 300
		self.text = str(payload)
		self.content = b"x"

	def json(self):
		if isinstance(self._payload, Exception):
			raise self._payload
		return self._payload


@override_settings(**LAPSE_SETTINGS)
class PkceTests(BaseTestCase):
	def test_the_challenge_is_the_unpadded_base64url_sha256_of_the_verifier(self):
		verifier, challenge = lapse.generate_pkce()
		expected = base64.urlsafe_b64encode(
			hashlib.sha256(verifier.encode("ascii")).digest()
		).decode("ascii").rstrip("=")
		self.assertEqual(challenge, expected)
		self.assertNotIn("=", challenge)

	def test_the_verifier_is_within_the_length_the_rfc_allows(self):
		verifier, _ = lapse.generate_pkce()
		self.assertGreaterEqual(len(verifier), 43)
		self.assertLessEqual(len(verifier), 128)

	def test_every_pair_is_fresh(self):
		self.assertNotEqual(lapse.generate_pkce()[0], lapse.generate_pkce()[0])


@override_settings(**LAPSE_SETTINGS)
class AuthorizeUrlTests(BaseTestCase):
	def _query(self):
		return parse_qs(urlparse(lapse.authorize_url("st4te", "ch4llenge")).query)

	def test_it_points_at_the_documented_endpoint(self):
		parsed = urlparse(lapse.authorize_url("st4te", "ch4llenge"))
		self.assertEqual(parsed.scheme, "https")
		self.assertEqual(parsed.netloc, "api.lapse.example")
		self.assertEqual(parsed.path, "/api/auth/authorize")

	def test_it_carries_every_parameter_the_endpoint_requires(self):
		query = self._query()
		self.assertEqual(query["response_type"], ["code"])
		self.assertEqual(query["client_id"], ["atlantis-test-client"])
		self.assertEqual(query["redirect_uri"], ["https://atlantis.example/projects/"])
		self.assertEqual(query["state"], ["st4te"])
		self.assertEqual(query["code_challenge"], ["ch4llenge"])
		self.assertEqual(query["code_challenge_method"], ["S256"])

	def test_it_asks_for_the_scopes_the_app_is_registered_with(self):
		self.assertEqual(
			sorted(self._query()["scope"][0].split()),
			["snapshot:read", "timelapse:read", "user:read"],
		)


@override_settings(**LAPSE_SETTINGS)
class TokenExchangeTests(BaseTestCase):
	TOKEN = {
		"access_token": "at-1",
		"expires_in": 3600,
		"token_type": "Bearer",
		"scope": "timelapse:read snapshot:read user:read",
	}

	@patch("atlantis_site.lapse.requests.post")
	def test_the_body_is_json_and_carries_the_verifier(self, mock_post):
		mock_post.return_value = FakeResponse(self.TOKEN)
		self.assertEqual(lapse.exchange_code("c0de", "v3rifier"), self.TOKEN)

		url = mock_post.call_args.args[0]
		self.assertEqual(url, "https://api.lapse.example/api/auth/token")
		# JSON, not the form encoding OAuth2 usually uses — that is what the
		# endpoint documents.
		body = mock_post.call_args.kwargs["json"]
		self.assertEqual(body, {
			"grant_type": "authorization_code",
			"code": "c0de",
			"redirect_uri": "https://atlantis.example/projects/",
			"client_id": "atlantis-test-client",
			"code_verifier": "v3rifier",
		})

	@patch("atlantis_site.lapse.requests.post")
	def test_a_response_with_no_token_in_it_is_an_error(self, mock_post):
		mock_post.return_value = FakeResponse({"token_type": "Bearer"})
		with self.assertRaises(lapse.LapseError):
			lapse.exchange_code("c0de", "v3rifier")

	# A non-2xx from the token endpoint is the one case where the status code
	# does carry the answer: that response has no envelope in it.
	@patch("atlantis_site.lapse.requests.post")
	def test_a_refusal_is_an_error(self, mock_post):
		mock_post.return_value = FakeResponse({"error": "invalid_grant"}, status_code=400)
		with self.assertRaises(lapse.LapseError):
			lapse.exchange_code("c0de", "v3rifier")


@override_settings(**LAPSE_SETTINGS)
class ApiEnvelopeTests(BaseTestCase):
	@patch("atlantis_site.lapse.requests.get")
	def test_the_token_travels_as_a_bearer_header(self, mock_get):
		mock_get.return_value = FakeResponse(envelope({"user": {"handle": "shipper"}}))
		self.assertEqual(lapse.fetch_myself("at-1"), {"handle": "shipper"})
		self.assertEqual(
			mock_get.call_args.kwargs["headers"], {"Authorization": "Bearer at-1"}
		)

	@patch("atlantis_site.lapse.requests.get")
	def test_a_refusal_inside_a_200_is_still_a_failure(self, mock_get):
		"""The envelope is the answer, not the status code."""
		mock_get.return_value = FakeResponse(
			{"ok": False, "error": "NOT_FOUND", "message": "no such thing"}
		)
		with self.assertRaises(lapse.LapseError):
			lapse.fetch_myself("at-1")

	@patch("atlantis_site.lapse.requests.get")
	def test_a_rejected_token_gets_its_own_exception(self, mock_get):
		"""It is the one failure the shipper can fix, so callers must tell."""
		for status in (401, 403):
			with self.subTest(status=status):
				mock_get.return_value = FakeResponse({}, status_code=status)
				with self.assertRaises(lapse.LapseAuthError):
					lapse.fetch_myself("at-1")

	@patch("atlantis_site.lapse.requests.get")
	def test_no_permission_in_the_envelope_reads_as_a_rejected_token_too(self, mock_get):
		mock_get.return_value = FakeResponse(
			{"ok": False, "error": "NO_PERMISSION", "message": "nope"}
		)
		with self.assertRaises(lapse.LapseAuthError):
			lapse.fetch_myself("at-1")

	@patch("atlantis_site.lapse.requests.get")
	def test_an_unauthenticated_caller_gets_a_null_user(self, mock_get):
		mock_get.return_value = FakeResponse(envelope({"user": None}))
		self.assertIsNone(lapse.fetch_myself("at-1"))


@override_settings(**LAPSE_SETTINGS)
class PaginationTests(BaseTestCase):
	@patch("atlantis_site.lapse.requests.get")
	def test_it_follows_the_cursor_until_there_is_none(self, mock_get):
		mock_get.side_effect = [
			FakeResponse(envelope({
				"timelapses": [lapse_payload("aaa")], "nextCursor": "aaa",
			})),
			FakeResponse(envelope({
				"timelapses": [lapse_payload("bbb")], "nextCursor": None,
			})),
		]
		found = lapse.fetch_published_timelapses("at-1")
		self.assertEqual([item["id"] for item in found], ["aaa", "bbb"])

		# The first call starts at the beginning; the second carries the cursor.
		first, second = mock_get.call_args_list
		self.assertNotIn("cursor", first.kwargs["params"])
		self.assertEqual(second.kwargs["params"]["cursor"], "aaa")

	@patch("atlantis_site.lapse.requests.get")
	def test_it_asks_for_the_biggest_page_the_api_allows(self, mock_get):
		mock_get.return_value = FakeResponse(
			envelope({"timelapses": [], "nextCursor": None})
		)
		lapse.fetch_published_timelapses("at-1")
		self.assertEqual(mock_get.call_args.kwargs["params"]["limit"], 100)
		self.assertLessEqual(lapse.PAGE_SIZE, 100)

	@patch("atlantis_site.lapse.requests.get")
	def test_a_cursor_that_never_ends_does_not_hold_the_request_open_forever(self, mock_get):
		mock_get.return_value = FakeResponse(envelope({
			"timelapses": [lapse_payload("aaa")], "nextCursor": "always",
		}))
		found = lapse.fetch_published_timelapses("at-1")
		self.assertEqual(mock_get.call_count, lapse.MAX_PAGES)
		self.assertEqual(len(found), lapse.MAX_PAGES)


@override_settings(**LAPSE_SETTINGS)
class WatchUrlTests(BaseTestCase):
	def test_it_is_the_permalink_not_the_expiring_playback_url(self):
		self.assertEqual(
			lapse.watch_url("L5A77TIFzEAg"),
			"https://lapse.example/timelapse/L5A77TIFzEAg",
		)

	def test_a_row_builds_its_own(self):
		project = make_project(make_user("shipper"))
		timelapse = make_timelapse(project, lapse_id="L5A77TIFzEAg")
		self.assertEqual(
			timelapse.watch_url, "https://lapse.example/timelapse/L5A77TIFzEAg"
		)


class DurationUnitTests(BaseTestCase):
	"""`duration` is recorded seconds. The live API returns 720 for a
	twelve-second video, and reading it the other way round would inflate every
	shipper's hours sixtyfold."""

	def test_recorded_time_lands_on_tracked_seconds_unconverted(self):
		project = make_project(make_user("shipper"))
		timelapse = make_timelapse(project, minutes=0, tracked_seconds=720)
		self.assertEqual(timelapse.tracked_seconds, 720)

	def test_the_video_runs_a_sixtieth_of_the_recorded_time(self):
		project = make_project(make_user("shipper"))
		timelapse = make_timelapse(project, minutes=0, tracked_seconds=720)
		self.assertEqual(timelapse.video_seconds, 12)
		self.assertEqual(timelapse.video_duration_display, "0:12")

	def test_the_attach_reads_the_duration_the_api_gave(self):
		self.assertEqual(tracked_to_video(720), 12)


class AccountTokenTests(BaseTestCase):
	def test_the_token_is_encrypted_at_rest_and_reads_back(self):
		user = make_user("shipper")
		account = connect_lapse(user)
		self.assertEqual(account.access_token, f"test-token-{user.pk}")
		# The ciphertext is what is stored; the token must not be findable in it.
		self.assertNotIn(f"test-token-{user.pk}", account.encrypted_token)

	def test_an_expiry_is_shaved_so_a_token_is_never_used_on_its_last_breath(self):
		account = LapseAccount(user=make_user("shipper"))
		account.save_token({"access_token": "at-1", "expires_in": 3600})
		remaining = (account.expires_at - timezone.now()).total_seconds()
		self.assertLess(remaining, 3600)
		self.assertGreater(remaining, 3600 - 120)

	def test_an_expired_token_is_not_usable(self):
		account = connect_lapse(make_user("shipper"))
		account.expires_at = timezone.now() - timedelta(seconds=1)
		self.assertTrue(account.is_expired)
		self.assertFalse(account.is_usable)

	def test_a_token_response_with_no_expiry_does_not_expire(self):
		account = LapseAccount(user=make_user("shipper"))
		account.save_token({"access_token": "at-1"})
		self.assertIsNone(account.expires_at)
		self.assertFalse(account.is_expired)


@override_settings(**LAPSE_SETTINGS)
class ConnectFlowTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("shipper")
		self.project = make_project(self.user)
		self.client.force_login(self.user)

	def _connect(self, **data):
		return self.client.post(reverse("lapse_connect"), data)

	def test_get_is_not_allowed(self):
		self.assertEqual(self.client.get(reverse("lapse_connect")).status_code, 405)

	def test_it_sends_the_browser_to_lapse_with_a_challenge(self):
		response = self._connect()
		self.assertEqual(response.status_code, 302)
		query = parse_qs(urlparse(response["Location"]).query)

		pending = self.client.session["lapse_oauth"]
		self.assertEqual(query["state"], [pending["state"]])
		# The verifier stays here. Only its hash goes through the browser.
		expected = base64.urlsafe_b64encode(
			hashlib.sha256(pending["verifier"].encode("ascii")).digest()
		).decode("ascii").rstrip("=")
		self.assertEqual(query["code_challenge"], [expected])
		self.assertNotIn(pending["verifier"], response["Location"])

	def test_it_remembers_the_book_to_come_back_to(self):
		destination = reverse("project_detail", args=[self.project.id])
		self._connect(next=destination)
		self.assertEqual(self.client.session["lapse_oauth"]["next"], destination)

	def test_it_refuses_to_be_pointed_off_site(self):
		self._connect(next="https://evil.example/steal")
		self.assertEqual(self.client.session["lapse_oauth"]["next"], "")

	def test_anonymous_users_are_sent_to_log_in(self):
		self.client.logout()
		self.assertEqual(self._connect().status_code, 302)
		self.assertNotIn("lapse_oauth", self.client.session)


@override_settings(**LAPSE_SETTINGS)
class CallbackTests(BaseTestCase):
	"""The projects list doubles as the redirect URI, so it is also the callback."""

	TOKEN = {
		"access_token": "at-1",
		"expires_in": 3600,
		"token_type": "Bearer",
		"scope": "timelapse:read snapshot:read user:read",
	}
	USER = {
		"id": "u-1",
		"handle": "shipper",
		"displayName": "Ship Per",
		"profilePictureUrl": "https://example.com/pfp.png",
	}

	def setUp(self):
		super().setUp()
		self.user = make_user("shipper")
		self.project = make_project(self.user)
		self.client.force_login(self.user)

	def _start(self, next_url=""):
		"""Run the real first half, so the session holds a real state/verifier."""
		self.client.post(reverse("lapse_connect"), {"next": next_url})
		return self.client.session["lapse_oauth"]

	def _land(self, **params):
		return self.client.get(reverse("projects"), params)

	def test_the_plain_projects_list_still_just_renders(self):
		self.assertEqual(self._land().status_code, 200)

	@patch("atlantis_site.lapse.fetch_myself")
	@patch("atlantis_site.lapse.exchange_code")
	def test_a_good_code_is_exchanged_and_stored(self, mock_exchange, mock_myself):
		mock_exchange.return_value = self.TOKEN
		mock_myself.return_value = self.USER
		pending = self._start()

		response = self._land(code="c0de", state=pending["state"])
		self.assertRedirects(response, reverse("projects"))

		mock_exchange.assert_called_once_with("c0de", pending["verifier"])
		account = LapseAccount.objects.get(user=self.user)
		self.assertEqual(account.access_token, "at-1")
		self.assertEqual(account.handle, "shipper")
		self.assertEqual(account.display_name, "Ship Per")
		self.assertEqual(account.lapse_user_id, "u-1")
		self.assertTrue(account.is_usable)
		# The in-flight authorization is spent.
		self.assertNotIn("lapse_oauth", self.client.session)

	@patch("atlantis_site.lapse.fetch_myself")
	@patch("atlantis_site.lapse.exchange_code")
	def test_it_returns_the_shipper_to_the_book_they_started_from(self, mock_exchange, mock_myself):
		mock_exchange.return_value = self.TOKEN
		mock_myself.return_value = self.USER
		destination = reverse("project_detail", args=[self.project.id])
		pending = self._start(next_url=destination)

		response = self._land(code="c0de", state=pending["state"])
		self.assertEqual(response["Location"], destination)

	@patch("atlantis_site.lapse.exchange_code")
	def test_a_code_whose_state_does_not_match_is_never_spent(self, mock_exchange):
		self._start()
		response = self._land(code="c0de", state="not-the-one")
		self.assertRedirects(response, reverse("projects"))
		mock_exchange.assert_not_called()
		self.assertFalse(LapseAccount.objects.exists())

	@patch("atlantis_site.lapse.exchange_code")
	def test_a_code_with_no_authorization_behind_it_is_never_spent(self, mock_exchange):
		"""Anyone can navigate to this URL with a query string of their choosing."""
		response = self._land(code="c0de", state="invented")
		self.assertRedirects(response, reverse("projects"))
		mock_exchange.assert_not_called()
		self.assertFalse(LapseAccount.objects.exists())

	@patch("atlantis_site.lapse.exchange_code")
	def test_a_refusal_from_lapse_leaves_the_connection_alone(self, mock_exchange):
		existing = connect_lapse(self.user)
		self._start()
		response = self._land(error="access_denied")
		self.assertRedirects(response, reverse("projects"))
		mock_exchange.assert_not_called()
		self.assertEqual(
			LapseAccount.objects.get(user=self.user).pk, existing.pk
		)

	@patch("atlantis_site.lapse.exchange_code")
	def test_a_failed_exchange_does_not_connect_anything(self, mock_exchange):
		mock_exchange.side_effect = lapse.LapseError("upstream on fire")
		pending = self._start()
		response = self._land(code="c0de", state=pending["state"])
		self.assertRedirects(response, reverse("projects"))
		self.assertFalse(LapseAccount.objects.exists())

	@patch("atlantis_site.lapse.fetch_myself")
	@patch("atlantis_site.lapse.exchange_code")
	def test_a_token_survives_a_failed_lookup_of_who_it_belongs_to(self, mock_exchange, mock_myself):
		"""The label is worth losing. The token is not."""
		mock_exchange.return_value = self.TOKEN
		mock_myself.side_effect = lapse.LapseError("no answer")
		pending = self._start()

		self._land(code="c0de", state=pending["state"])
		account = LapseAccount.objects.get(user=self.user)
		self.assertEqual(account.access_token, "at-1")
		self.assertEqual(account.handle, "")

	@patch("atlantis_site.lapse.fetch_myself")
	@patch("atlantis_site.lapse.exchange_code")
	def test_reconnecting_replaces_the_token_on_the_same_row(self, mock_exchange, mock_myself):
		first = connect_lapse(self.user)
		mock_exchange.return_value = {**self.TOKEN, "access_token": "at-2"}
		mock_myself.return_value = self.USER
		pending = self._start()

		self._land(code="c0de", state=pending["state"])
		self.assertEqual(LapseAccount.objects.count(), 1)
		account = LapseAccount.objects.get(pk=first.pk)
		self.assertEqual(account.access_token, "at-2")


@override_settings(**LAPSE_SETTINGS)
class DisconnectTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("shipper")
		self.project = make_project(self.user)
		self.client.force_login(self.user)
		connect_lapse(self.user)

	def test_get_is_not_allowed(self):
		self.assertEqual(self.client.get(reverse("lapse_disconnect")).status_code, 405)

	def test_it_forgets_the_token(self):
		self.client.post(reverse("lapse_disconnect"))
		self.assertFalse(LapseAccount.objects.filter(user=self.user).exists())

	def test_footage_already_taped_in_stays_where_it_is(self):
		"""Those hours are already claimed and already reviewable."""
		timelapse = make_timelapse(self.project, minutes=60)
		self.client.post(reverse("lapse_disconnect"))
		self.assertTrue(Timelapse.objects.filter(pk=timelapse.pk).exists())

	def test_it_leaves_other_peoples_connections_alone(self):
		other = make_user("someone-else")
		connect_lapse(other)
		self.client.post(reverse("lapse_disconnect"))
		self.assertTrue(LapseAccount.objects.filter(user=other).exists())

	def test_it_returns_to_the_book_it_was_asked_to(self):
		destination = reverse("project_detail", args=[self.project.id])
		response = self.client.post(reverse("lapse_disconnect"), {"next": destination})
		self.assertEqual(response["Location"], destination)

	def test_it_refuses_to_be_pointed_off_site(self):
		response = self.client.post(
			reverse("lapse_disconnect"), {"next": "https://evil.example/steal"}
		)
		self.assertEqual(response["Location"], reverse("projects"))


@override_settings(**LAPSE_SETTINGS)
class PickerThrottleTests(BaseTestCase):
	"""Every open of the picker is a call on somebody else's API."""

	def setUp(self):
		super().setUp()
		cache.clear()
		self.user = make_user("shipper")
		self.project = make_project(self.user)
		self.client.force_login(self.user)
		connect_lapse(self.user)

	@patch("atlantis_site.lapse.fetch_published_timelapses", return_value=[])
	def test_a_second_read_straight_away_is_refused(self, mock_fetch):
		url = reverse("lapse_timelapses", args=[self.project.id])
		self.assertEqual(self.client.get(url).status_code, 200)
		self.assertEqual(self.client.get(url).status_code, 429)
		# And the refusal costs Lapse nothing.
		self.assertEqual(mock_fetch.call_count, 1)

	@patch("atlantis_site.lapse.fetch_published_timelapses", return_value=[])
	def test_one_shippers_refresh_does_not_throttle_anothers(self, mock_fetch):
		url = reverse("lapse_timelapses", args=[self.project.id])
		self.assertEqual(self.client.get(url).status_code, 200)

		other = make_user("someone-else")
		other_project = make_project(other)
		connect_lapse(other)
		self.client.force_login(other)
		self.assertEqual(
			self.client.get(
				reverse("lapse_timelapses", args=[other_project.id])
			).status_code,
			200,
		)
