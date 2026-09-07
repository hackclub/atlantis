"""The Lapse integration: the API client, the authorization, and the picker.

The parts worth testing here are the ones where getting it wrong costs money or
leaks a credential: that `duration` lands on tracked_seconds without being
converted, that footage with no video behind it can never back an hour, that the
same timelapse can't be paid for twice, and that the token never reaches a page.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from datetime import timedelta, timezone as dt_timezone

from django.utils import timezone as dj_timezone

from .. import lapse
from ..models import LapseAccount, Journal, Timelapse
from .base import (
	BaseTestCase, image_upload, make_lookout, make_project, make_user,
	message_texts, stl_upload,
)

LAPSE_SETTINGS = {
	"LAPSE_CLIENT_ID": "svc_test",
	"LAPSE_CLIENT_SECRET": "scs_test",
	"LAPSE_API_BASE_URL": "https://api.lapse.test/api",
	"LAPSE_WEB_BASE_URL": "https://lapse.test",
	"LAPSE_REDIRECT_URI": "https://atlantis.test/lapse/callback/",
}


def timelapse_payload(lapse_id="tl-1", duration=7200, **overrides):
	"""One timelapse as /timelapse/myPublishedTimelapses returns it."""
	payload = {
		"id": lapse_id,
		"name": "Modelling the bracket",
		"description": "",
		"visibility": "PUBLIC",
		"duration": duration,
		"createdAt": 1788226165685,
		"playbackUrl": f"https://lookout.hackclub.com/api/media/{lapse_id}/video.mp4",
		"thumbnailUrl": f"https://lookout.hackclub.com/api/media/{lapse_id}/thumbnail.jpg",
	}
	payload.update(overrides)
	return payload


class FakeResponse:
	def __init__(self, payload, status_code=200, text=""):
		self._payload = payload
		self.status_code = status_code
		self.ok = 200 <= status_code < 300
		self.text = text

	def json(self):
		if self._payload is None:
			raise ValueError("not json")
		return self._payload


def envelope(data):
	return FakeResponse({"ok": True, "data": data})


def refusal(error="ERROR", message="nope", status_code=200):
	return FakeResponse({"ok": False, "error": error, "message": message}, status_code)


@override_settings(**LAPSE_SETTINGS)
class LapseClientTests(BaseTestCase):
	"""The API client, against the envelope the real API actually returns."""

	def test_authorize_url_carries_pkce_and_every_scope(self):
		verifier, challenge = lapse.generate_pkce()
		url = lapse.authorize_url("state-123", challenge)

		self.assertTrue(url.startswith("https://api.lapse.test/api/auth/authorize?"))
		for fragment in (
			"response_type=code",
			"client_id=svc_test",
			"code_challenge_method=S256",
			"state=state-123",
			f"code_challenge={challenge}",
		):
			self.assertIn(fragment, url)
		# The three scopes the app is registered for, space-separated.
		self.assertIn("scope=timelapse%3Aread+snapshot%3Aread+user%3Aread", url)
		self.assertNotIn(verifier, url, "the verifier must never travel through the browser")

	def test_pkce_challenge_is_the_unpadded_base64url_sha256_of_the_verifier(self):
		import base64, hashlib

		verifier, challenge = lapse.generate_pkce()
		expected = base64.urlsafe_b64encode(
			hashlib.sha256(verifier.encode("ascii")).digest()
		).decode("ascii").rstrip("=")
		self.assertEqual(challenge, expected)
		self.assertNotIn("=", challenge)

	def test_refusal_inside_a_200_envelope_is_an_error(self):
		"""Both outcomes arrive as 200s, so the envelope is what decides."""
		with patch("atlantis_site.lapse.requests.get", return_value=refusal()):
			with self.assertRaises(lapse.LapseError):
				lapse.fetch_myself("token")

	def test_no_permission_and_expired_are_auth_errors(self):
		for error in ("NO_PERMISSION", "EXPIRED"):
			with self.subTest(error=error):
				with patch("atlantis_site.lapse.requests.get", return_value=refusal(error)):
					with self.assertRaises(lapse.LapseAuthError):
						lapse.fetch_myself("token")

	def test_401_is_an_auth_error(self):
		with patch("atlantis_site.lapse.requests.get", return_value=FakeResponse(None, 401)):
			with self.assertRaises(lapse.LapseAuthError):
				lapse.fetch_myself("token")

	def test_published_timelapses_follow_the_cursor(self):
		pages = [
			envelope({"timelapses": [timelapse_payload("a")], "nextCursor": "a"}),
			envelope({"timelapses": [timelapse_payload("b")], "nextCursor": None}),
		]
		with patch("atlantis_site.lapse.requests.get", side_effect=pages) as get:
			found = lapse.fetch_published_timelapses("token")

		self.assertEqual([item["id"] for item in found], ["a", "b"])
		self.assertEqual(get.call_args_list[1].kwargs["params"]["cursor"], "a")

	def test_paging_stops_rather_than_following_a_cursor_forever(self):
		endless = envelope({"timelapses": [timelapse_payload()], "nextCursor": "more"})
		with patch("atlantis_site.lapse.requests.get", return_value=endless) as get:
			lapse.fetch_published_timelapses("token")
		self.assertEqual(get.call_count, lapse.MAX_PAGES)

	def test_bearer_token_is_sent_and_never_put_in_the_query(self):
		with patch("atlantis_site.lapse.requests.get", return_value=envelope({"user": None})) as get:
			lapse.fetch_myself("secret-token")
		call = get.call_args
		self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer secret-token")
		self.assertNotIn("secret-token", call.args[0])

	def test_attachable_requires_a_video_and_a_published_visibility(self):
		self.assertTrue(lapse.is_attachable(timelapse_payload()))
		self.assertTrue(lapse.is_attachable(timelapse_payload(visibility="UNLISTED")))
		# Still processing.
		self.assertFalse(lapse.is_attachable(timelapse_payload(playbackUrl=None)))
		# Processing gave up.
		self.assertFalse(lapse.is_attachable(timelapse_payload(visibility="FAILED_PROCESSING")))
		self.assertFalse(lapse.is_attachable(None))

	def test_watch_url_is_the_permalink(self):
		self.assertEqual(lapse.watch_url("abc"), "https://lapse.test/timelapse/abc")

	def test_a_500_from_the_token_endpoint_is_a_rejected_code(self):
		"""Lapse answers a code it won't take with a 500, not `invalid_grant`."""
		with patch("atlantis_site.lapse.requests.post",
				   return_value=FakeResponse(None, 500, text="Internal server error")):
			with self.assertRaises(lapse.LapseCodeRejected):
				lapse.exchange_code("spent", "verifier")

	def test_the_client_secret_is_sent_when_configured(self):
		token = {"access_token": "at", "expires_in": 3600, "token_type": "Bearer", "scope": "x"}
		with patch("atlantis_site.lapse.requests.post", return_value=FakeResponse(token)) as post:
			lapse.exchange_code("c", "v")
		self.assertEqual(post.call_args.kwargs["json"]["client_secret"], "scs_test")

	def test_token_expiry_is_read_out_of_the_jwt(self):
		import base64, json as _json
		payload = base64.urlsafe_b64encode(
			_json.dumps({"sub": "u", "exp": 1756063831}).encode()
		).decode().rstrip("=")
		exp = lapse.token_expiry(f"header.{payload}.sig")
		self.assertEqual(exp.year, 2025)
		self.assertEqual(exp.tzinfo, dt_timezone.utc)

	def test_token_expiry_is_none_when_there_is_nothing_to_read(self):
		for bad in ("", "not-a-jwt", "a.b", "a.!!!not-base64!!!.c", "a.e30.c"):
			with self.subTest(token=bad):
				self.assertIsNone(lapse.token_expiry(bad))

	def test_a_null_user_is_how_a_dead_token_presents(self):
		"""/user/myself answers a bad token with 200 {"user": null}, never a 401."""
		with patch("atlantis_site.lapse.requests.get", return_value=envelope({"user": None})):
			self.assertIsNone(lapse.token_names_a_user("dead-token"))

	def test_token_response_without_an_access_token_is_refused(self):
		with patch("atlantis_site.lapse.requests.post", return_value=FakeResponse({"scope": "x"})):
			with self.assertRaises(lapse.LapseError):
				lapse.exchange_code("code", "verifier")

	def test_token_exchange_sends_the_verifier_and_the_redirect_uri(self):
		token = {"access_token": "at", "expires_in": 3600, "token_type": "Bearer", "scope": "user:read"}
		with patch("atlantis_site.lapse.requests.post", return_value=FakeResponse(token)) as post:
			self.assertEqual(lapse.exchange_code("the-code", "the-verifier"), token)
		body = post.call_args.kwargs["json"]
		self.assertEqual(body["grant_type"], "authorization_code")
		self.assertEqual(body["code"], "the-code")
		self.assertEqual(body["code_verifier"], "the-verifier")
		self.assertEqual(body["redirect_uri"], "https://atlantis.test/lapse/callback/")


@override_settings(**LAPSE_SETTINGS)
class LapseAccountTests(BaseTestCase):
	def test_token_is_encrypted_at_rest_and_readable_back(self):
		account = LapseAccount.objects.create(user=make_user("shipper"))
		account.save_token({"access_token": "sekrit", "expires_in": 3600, "scope": "user:read"})
		account.save()

		self.assertEqual(account.access_token, "sekrit")
		self.assertNotIn("sekrit", account.encrypted_token)
		self.assertEqual(account.scope, "user:read")

	def test_expiry_is_shaved_so_a_token_is_gone_before_it_is_used(self):
		account = LapseAccount.objects.create(user=make_user("shipper"))
		# One second of life, minus the minute of slack, is already expired.
		account.save_token({"access_token": "at", "expires_in": 1})
		self.assertTrue(account.is_expired)
		self.assertFalse(account.is_usable)

		account.save_token({"access_token": "at", "expires_in": 3600})
		self.assertFalse(account.is_expired)
		self.assertTrue(account.is_usable)

	def test_a_token_that_will_not_decrypt_is_not_usable(self):
		account = LapseAccount.objects.create(user=make_user("shipper"), encrypted_token="rubbish")
		self.assertEqual(account.access_token, "")
		self.assertFalse(account.is_usable)


@override_settings(**LAPSE_SETTINGS)
class LapseAuthorizationTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("shipper")
		self.client.force_login(self.user)

	def _start(self, **data):
		return self.client.post(reverse("lapse_connect"), data)

	def test_connect_redirects_to_lapse_and_keeps_the_verifier_server_side(self):
		response = self._start()
		self.assertEqual(response.status_code, 302)
		self.assertTrue(response["Location"].startswith("https://api.lapse.test/api/auth/authorize?"))

		pending = self.client.session["lapse_oauth"]
		self.assertIn("verifier", pending)
		self.assertNotIn(pending["verifier"], response["Location"])

	def test_a_code_with_the_wrong_state_is_refused(self):
		self._start()
		with patch("atlantis_site.lapse.exchange_code") as exchange:
			response = self.client.get(
				reverse("lapse_callback"), {"code": "c", "state": "not-the-one"}, follow=True
			)
		exchange.assert_not_called()
		self.assertEqual(LapseAccount.objects.count(), 0)
		self.assertTrue(any("didn't match" in m for m in message_texts(response)))

	def test_a_code_with_no_authorization_behind_it_is_refused(self):
		with patch("atlantis_site.lapse.exchange_code") as exchange:
			self.client.get(reverse("lapse_callback"), {"code": "c", "state": "anything"})
		exchange.assert_not_called()
		self.assertEqual(LapseAccount.objects.count(), 0)

	def test_a_successful_callback_stores_the_token_and_names_the_account(self):
		self._start()
		state = self.client.session["lapse_oauth"]["state"]

		token = {"access_token": "at", "expires_in": 3600, "scope": "user:read"}
		user = {"id": "u1", "handle": "swarit", "displayName": "Swarit", "profilePictureUrl": ""}
		with patch("atlantis_site.lapse.exchange_code", return_value=token), \
				patch("atlantis_site.lapse.fetch_myself", return_value=user):
			response = self.client.get(
				reverse("lapse_callback"), {"code": "c", "state": state}, follow=True
			)

		account = LapseAccount.objects.get()
		self.assertEqual(account.access_token, "at")
		self.assertEqual(account.handle, "swarit")
		self.assertTrue(any("@swarit" in m for m in message_texts(response)))

	def test_a_token_lapse_will_not_accept_is_never_stored(self):
		"""The bug this exists to stop.

		/user/myself answers a dead token with 200 {"user": null} rather than a
		401, so a connection that skips this check reports success and then
		fails on every call the shipper makes afterwards.
		"""
		self._start()
		state = self.client.session["lapse_oauth"]["state"]

		with patch("atlantis_site.lapse.exchange_code",
				   return_value={"access_token": "already-dead", "expires_in": 3600}), \
				patch("atlantis_site.lapse.fetch_myself", return_value=None):
			response = self.client.get(
				reverse("lapse_callback"), {"code": "c", "state": state}, follow=True
			)

		self.assertEqual(LapseAccount.objects.count(), 0, "a refused token must not be stored")
		texts = message_texts(response)
		self.assertFalse(any("Connected your Lapse account" in m for m in texts), texts)
		self.assertTrue(any("won't accept" in m for m in texts), texts)

	def test_lapse_being_unreachable_during_the_check_stores_nothing(self):
		"""Down and refused are different, and neither may leave a half-connection."""
		self._start()
		state = self.client.session["lapse_oauth"]["state"]

		with patch("atlantis_site.lapse.exchange_code", return_value={"access_token": "at", "expires_in": 3600}), \
				patch("atlantis_site.lapse.fetch_myself", side_effect=lapse.LapseError("down")):
			response = self.client.get(
				reverse("lapse_callback"), {"code": "c", "state": state}, follow=True
			)

		self.assertEqual(LapseAccount.objects.count(), 0)
		self.assertTrue(any("Try again in a moment" in m for m in message_texts(response)))

	def test_a_rejected_code_says_so_rather_than_offering_a_retry(self):
		self._start()
		state = self.client.session["lapse_oauth"]["state"]

		with patch("atlantis_site.lapse.exchange_code",
				   side_effect=lapse.LapseCodeRejected("refused")):
			response = self.client.get(
				reverse("lapse_callback"), {"code": "c", "state": state}, follow=True
			)

		self.assertEqual(LapseAccount.objects.count(), 0)
		self.assertTrue(any("turned that sign-in down" in m for m in message_texts(response)))

	def test_the_stored_expiry_comes_from_the_jwt_not_expires_in(self):
		"""They disagree in the wild; the JWT is what Lapse enforces."""
		import base64, json as _json
		past = int((dj_timezone.now() - timedelta(days=14)).timestamp())
		payload = base64.urlsafe_b64encode(_json.dumps({"exp": past}).encode()).decode().rstrip("=")
		jwt = f"h.{payload}.s"

		self._start()
		state = self.client.session["lapse_oauth"]["state"]
		user = {"id": "u1", "handle": "swarit", "displayName": "S", "profilePictureUrl": ""}
		# expires_in claims an hour of life; the token itself expired a fortnight ago.
		with patch("atlantis_site.lapse.exchange_code",
				   return_value={"access_token": jwt, "expires_in": 3600}), \
				patch("atlantis_site.lapse.fetch_myself", return_value=user):
			self.client.get(reverse("lapse_callback"), {"code": "c", "state": state})

		account = LapseAccount.objects.get()
		self.assertTrue(account.is_expired, "expires_in must not override the JWT's own exp")
		self.assertFalse(account.is_usable)

	def test_the_authorization_returns_to_the_book_it_started_from(self):
		project = make_project(self.user)
		book = reverse("project_detail", args=[project.id])
		self._start(next=book)
		state = self.client.session["lapse_oauth"]["state"]

		with patch("atlantis_site.lapse.exchange_code", return_value={"access_token": "at", "expires_in": 3600}), \
				patch("atlantis_site.lapse.fetch_myself", return_value={"id": "u1", "handle": "h", "displayName": "d", "profilePictureUrl": ""}):
			response = self.client.get(reverse("lapse_callback"), {"code": "c", "state": state})
		self.assertEqual(response["Location"], book)

	def test_an_offsite_next_is_ignored(self):
		self._start(next="https://evil.test/steal")
		self.assertEqual(self.client.session["lapse_oauth"]["next"], "")

	def test_a_spent_code_cannot_be_replayed(self):
		self._start()
		state = self.client.session["lapse_oauth"]["state"]
		with patch("atlantis_site.lapse.exchange_code", return_value={"access_token": "at", "expires_in": 3600}), \
				patch("atlantis_site.lapse.fetch_myself",
					  return_value={"id": "u1", "handle": "h", "displayName": "d", "profilePictureUrl": ""}):
			self.client.get(reverse("lapse_callback"), {"code": "c", "state": state})

		# The pending authorization is gone, so the same code lands nowhere.
		with patch("atlantis_site.lapse.exchange_code") as exchange:
			self.client.get(reverse("lapse_callback"), {"code": "c", "state": state})
		exchange.assert_not_called()

	def test_disconnect_forgets_the_token_but_keeps_the_hours(self):
		account = LapseAccount.objects.create(user=self.user)
		account.save_token({"access_token": "at", "expires_in": 3600})
		account.save()
		project = make_project(self.user)
		taped_in = Timelapse.objects.create(
			project=project, owner=self.user, source=Timelapse.Source.LAPSE,
			lapse_id="keep-me", tracked_seconds=3600, status=Timelapse.Status.COMPLETE,
		)

		self.client.post(reverse("lapse_disconnect"))
		self.assertEqual(LapseAccount.objects.count(), 0)
		taped_in.refresh_from_db()
		self.assertEqual(taped_in.tracked_seconds, 3600)


@override_settings(**LAPSE_SETTINGS)
class LapsePickerTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("shipper")
		self.client.force_login(self.user)
		self.project = make_project(self.user)
		self.account = LapseAccount.objects.create(user=self.user)
		self.account.save_token({"access_token": "at", "expires_in": 3600})
		self.account.save()

	def _list(self, project=None):
		cache.clear()  # the picker is rate limited; the guard isn't what's under test
		return self.client.get(
			reverse("lapse_timelapses", args=[(project or self.project).id])
		)

	def test_unconnected_account_is_reported_rather_than_erroring(self):
		LapseAccount.objects.all().delete()
		payload = self._list().json()
		self.assertTrue(payload["ok"])
		self.assertFalse(payload["connected"])

	def test_an_expired_token_asks_for_a_reconnect_without_calling_lapse(self):
		self.account.save_token({"access_token": "at", "expires_in": 1})
		self.account.save()
		with patch("atlantis_site.lapse.fetch_published_timelapses") as fetch:
			payload = self._list().json()
		fetch.assert_not_called()
		self.assertTrue(payload["expired"])

	def test_rows_are_marked_with_why_they_cannot_be_picked(self):
		Timelapse.objects.create(
			project=self.project, owner=self.user, source=Timelapse.Source.LAPSE,
			lapse_id="taken", tracked_seconds=60, status=Timelapse.Status.COMPLETE,
		)
		listing = [
			timelapse_payload("ok"),
			timelapse_payload("taken"),
			timelapse_payload("busy", playbackUrl=None),
			timelapse_payload("broken", visibility="FAILED_PROCESSING"),
		]
		with patch("atlantis_site.lapse.fetch_published_timelapses", return_value=listing):
			payload = self._list().json()

		states = {row["id"]: row["state"] for row in payload["timelapses"]}
		self.assertEqual(states, {
			"ok": "available",
			"taken": "attached",
			"busy": "processing",
			"broken": "failed",
		})

	def test_footage_taped_into_another_book_still_counts_as_attached(self):
		other = make_project(self.user, title="Elsewhere")
		Timelapse.objects.create(
			project=other, owner=self.user, source=Timelapse.Source.LAPSE,
			lapse_id="shared", tracked_seconds=60, status=Timelapse.Status.COMPLETE,
		)
		with patch("atlantis_site.lapse.fetch_published_timelapses", return_value=[timelapse_payload("shared")]):
			payload = self._list().json()
		self.assertEqual(payload["timelapses"][0]["state"], "attached")

	def test_duration_is_shown_as_recorded_time_not_video_time(self):
		"""7200 is two hours of work, not two hours of video."""
		with patch("atlantis_site.lapse.fetch_published_timelapses", return_value=[timelapse_payload(duration=7200)]):
			row = self._list().json()["timelapses"][0]
		self.assertEqual(row["trackedSeconds"], 7200)
		self.assertEqual(row["trackedDisplay"], "2h 0m")

	def test_lapse_being_down_is_a_502_not_a_crash(self):
		with patch("atlantis_site.lapse.fetch_published_timelapses", side_effect=lapse.LapseError("down")):
			response = self._list()
		self.assertEqual(response.status_code, 502)
		self.assertFalse(response.json()["ok"])

	def test_the_token_never_reaches_the_response(self):
		with patch("atlantis_site.lapse.fetch_published_timelapses", return_value=[timelapse_payload()]):
			response = self._list()
		self.assertNotIn("at", response.json().get("account", {}).values())
		self.assertNotIn(b"encrypted_token", response.content)

	def test_a_refused_token_is_dropped_so_the_picker_offers_a_reconnect(self):
		"""Fallout clears its token on Unauthorized; without this the book
		would keep sending a credential Lapse has already refused."""
		with patch("atlantis_site.lapse.fetch_published_timelapses",
				   side_effect=lapse.LapseAuthError("refused")):
			payload = self._list().json()

		self.assertTrue(payload["expired"])
		self.assertFalse(payload["connected"])
		account = LapseAccount.objects.get()
		self.assertEqual(account.access_token, "", "the refused credential must be dropped")
		self.assertFalse(account.is_usable)
		# The label survives, so the page can still say whose account it was.
		self.assertEqual(account.pk, self.account.pk)

	def test_lapse_being_down_does_not_drop_a_good_token(self):
		with patch("atlantis_site.lapse.fetch_published_timelapses",
				   side_effect=lapse.LapseError("down")):
			self._list()
		self.assertEqual(LapseAccount.objects.get().access_token, "at")

	def test_another_users_book_is_not_readable(self):
		theirs = make_project(make_user("stranger"))
		self.assertEqual(self._list(theirs).status_code, 404)


@override_settings(ALLOW_JOURNALING=True, **LAPSE_SETTINGS)
class CreateJournalFromLapseTests(BaseTestCase):
	"""Taping Lapse footage into a lapse — the path hours actually arrive by."""

	def setUp(self):
		super().setUp()
		self.user = make_user("shipper")
		self.client.force_login(self.user)
		self.project = make_project(self.user)
		self.account = LapseAccount.objects.create(user=self.user)
		self.account.save_token({"access_token": "at", "expires_in": 3600})
		self.account.save()

	def _create(self, published=None, lapse_ids=("tl-1",), **overrides):
		cache.clear()
		data = {
			"lapse_timelapses": list(lapse_ids),
			"title": "Progress update",
			"image": image_upload(),
			"STL": stl_upload(),
		}
		data.update(overrides)
		listing = [timelapse_payload()] if published is None else published
		with patch("atlantis_site.lapse.fetch_published_timelapses", return_value=listing):
			return self.client.post(
				reverse("create_journal", args=[self.project.id]), data
			)

	def test_a_lapse_timelapse_is_written_at_the_attach(self):
		self._create()

		row = Timelapse.objects.get()
		self.assertEqual(row.source, Timelapse.Source.LAPSE)
		self.assertEqual(row.lapse_id, "tl-1")
		self.assertEqual(row.name, "Modelling the bracket")
		self.assertEqual(row.journal, Journal.objects.get())
		self.assertEqual(row.owner, self.user)
		# It arrived finished; there was no lifecycle to watch.
		self.assertEqual(row.status, Timelapse.Status.COMPLETE)
		self.assertEqual(row.session_id, "")
		self.assertEqual(row.token, "")

	def test_duration_lands_on_tracked_seconds_unconverted(self):
		"""The bug that would multiply every shipper's hours by sixty."""
		self._create(published=[timelapse_payload(duration=7200)])
		self.assertEqual(Timelapse.objects.get().tracked_seconds, 7200)
		self.assertEqual(Journal.objects.get().tracked_minutes, 120)

	def test_recorded_at_is_read_as_epoch_milliseconds(self):
		self._create()
		recorded = Timelapse.objects.get().recorded_at
		self.assertEqual(recorded.year, 2026)

	def test_time_is_re_read_from_lapse_not_taken_from_the_form(self):
		"""The browser sends ids and nothing else."""
		response = self._create(
			published=[timelapse_payload(duration=600)],
			trackedSeconds="999999", duration="999999",
		)
		self.assertEqual(response.status_code, 302)
		self.assertEqual(Timelapse.objects.get().tracked_seconds, 600)

	def test_footage_that_is_not_on_the_account_is_refused(self):
		response = self._create(published=[timelapse_payload("something-else")])
		self.assertEqual(Journal.objects.count(), 0)
		self.assertTrue(any("can't be attached" in m for m in message_texts(response)))

	def test_footage_with_no_video_behind_it_is_refused(self):
		for broken in (
			timelapse_payload(playbackUrl=None),
			timelapse_payload(visibility="FAILED_PROCESSING"),
		):
			with self.subTest(visibility=broken["visibility"], playback=broken["playbackUrl"]):
				response = self._create(published=[broken])
				self.assertEqual(Journal.objects.count(), 0)
				self.assertTrue(any("can't be attached" in m for m in message_texts(response)))

	def test_the_same_footage_cannot_be_paid_for_twice(self):
		self._create()
		self.assertEqual(Journal.objects.count(), 1)

		response = self._create()
		self.assertEqual(Journal.objects.count(), 1)
		self.assertEqual(Timelapse.objects.count(), 1)
		self.assertTrue(any("already taped into a lapse" in m for m in message_texts(response)))

	def test_the_same_id_twice_in_one_submission_is_refused(self):
		response = self._create(lapse_ids=("tl-1", "tl-1"))
		self.assertEqual(Journal.objects.count(), 0)
		self.assertTrue(any("same timelapse in it twice" in m for m in message_texts(response)))

	def test_an_unconnected_account_cannot_tape_anything_in(self):
		LapseAccount.objects.all().delete()
		response = self._create()
		self.assertEqual(Journal.objects.count(), 0)
		self.assertTrue(any("Connect your Lapse account" in m for m in message_texts(response)))

	def test_an_expired_connection_asks_for_a_reconnect(self):
		self.account.save_token({"access_token": "at", "expires_in": 1})
		self.account.save()
		response = self._create()
		self.assertEqual(Journal.objects.count(), 0)
		self.assertTrue(any("expired" in m for m in message_texts(response)))

	def test_a_refused_token_is_dropped_when_taping_in(self):
		cache.clear()
		with patch("atlantis_site.lapse.fetch_published_timelapses",
				   side_effect=lapse.LapseAuthError("refused")):
			response = self.client.post(reverse("create_journal", args=[self.project.id]), {
				"lapse_timelapses": ["tl-1"], "title": "T",
				"image": image_upload(), "STL": stl_upload(),
			})
		self.assertEqual(Journal.objects.count(), 0)
		self.assertEqual(LapseAccount.objects.get().access_token, "")
		self.assertTrue(any("expired" in m for m in message_texts(response)))

	def test_lapse_being_down_leaves_no_journal_behind(self):
		cache.clear()
		with patch("atlantis_site.lapse.fetch_published_timelapses", side_effect=lapse.LapseError("down")):
			response = self.client.post(reverse("create_journal", args=[self.project.id]), {
				"lapse_timelapses": ["tl-1"],
				"title": "Progress update",
				"image": image_upload(),
				"STL": stl_upload(),
			})
		self.assertEqual(Journal.objects.count(), 0)
		self.assertTrue(any("Couldn't reach Lapse" in m for m in message_texts(response)))

	def test_lapse_and_lookout_footage_can_back_the_same_lapse(self):
		"""Both recorders feed one journal's hours, and both rows point at it."""
		legacy = make_lookout(self.project, minutes=30)
		self._create(
			published=[timelapse_payload(duration=3600)],
			lookout_timelapses=[str(legacy.pk)],
		)

		journal = Journal.objects.get()
		self.assertEqual(journal.tracked_minutes, 90)
		self.assertEqual(
			set(journal.timelapses.values_list("source", flat=True)),
			{Timelapse.Source.LAPSE, Timelapse.Source.LOOKOUT},
		)


@override_settings(**LAPSE_SETTINGS)
class TimelapseModelTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.project = make_project(make_user("shipper"))

	def _lapse_row(self, **kwargs):
		defaults = {
			"project": self.project, "owner": self.project.owner,
			"source": Timelapse.Source.LAPSE, "lapse_id": "tl-9",
			"playback_url": "https://media.test/tl-9.mp4",
			"lapse_thumbnail_url": "https://media.test/tl-9.jpg",
			"tracked_seconds": 7200, "status": Timelapse.Status.COMPLETE,
		}
		defaults.update(kwargs)
		return Timelapse.objects.create(**defaults)

	def test_a_lapse_row_links_to_the_permalink_and_streams_the_video(self):
		row = self._lapse_row()
		self.assertEqual(row.watch_url, "https://lapse.test/timelapse/tl-9")
		self.assertEqual(row.video_url, "https://media.test/tl-9.mp4")
		self.assertEqual(row.thumbnail_url, "https://media.test/tl-9.jpg")

	@override_settings(LOOKOUT_BASE_URL="https://lookout.test")
	def test_a_lookout_row_still_builds_its_urls_from_the_session_id(self):
		row = make_lookout(self.project, minutes=60, session_id="sess-1")
		self.assertEqual(row.video_url, "https://lookout.test/api/media/sess-1/video.mp4")
		self.assertEqual(row.thumbnail_url, "https://lookout.test/api/media/sess-1/thumbnail.jpg")
		# Lookout never offered a page, so watching means the file.
		self.assertEqual(row.watch_url, row.video_url)

	def test_video_length_for_lapse_is_the_tracked_time_over_sixty(self):
		self.assertEqual(self._lapse_row(tracked_seconds=7200).video_seconds, 120)

	def test_video_length_for_lookout_is_the_screenshot_count(self):
		row = make_lookout(self.project, minutes=65, screenshot_count=71)
		self.assertEqual(row.video_seconds, 71)

	def test_a_measured_length_wins_over_either_estimate(self):
		self.assertEqual(
			self._lapse_row(tracked_seconds=7200, measured_video_seconds=118).video_seconds,
			118,
		)

	def test_only_a_lookout_row_is_ever_resumable(self):
		self.assertFalse(self._lapse_row().is_recordable)
		self.assertTrue(
			make_lookout(self.project, status=Timelapse.Status.ACTIVE).is_recordable
		)

	def test_a_lapse_id_cannot_be_reused_but_empty_ones_do_not_collide(self):
		from django.db import IntegrityError, transaction

		self._lapse_row(lapse_id="dupe")
		with self.assertRaises(IntegrityError):
			with transaction.atomic():
				self._lapse_row(lapse_id="dupe")

		# Two Lookout rows both have an empty lapse_id, and that must be fine.
		make_lookout(self.project)
		make_lookout(self.project)

	def test_a_row_must_be_identifiable_on_the_service_it_came_from(self):
		from django.db import IntegrityError, transaction

		with self.assertRaises(IntegrityError):
			with transaction.atomic():
				self._lapse_row(lapse_id="")
