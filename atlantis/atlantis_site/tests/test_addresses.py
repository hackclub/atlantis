from unittest.mock import patch

from django.test import TestCase, override_settings

from .. import hca
from ..hca import AddressUnavailable, extract_addresses, fetch_addresses, storable_token
from .base import TEST_ENCRYPTION_KEY, make_user

HCA_METADATA = {
	"token_endpoint": "https://auth.hackclub.com/oauth/token",
	"userinfo_endpoint": "https://auth.hackclub.com/oauth/userinfo",
}

TOKEN = {"access_token": "at-old", "refresh_token": "rt-old", "token_type": "Bearer"}

ADDRESS = {
	"id": "adr_1",
	"first_name": "Test",
	"last_name": "Person",
	"line_1": "15 Falls Rd",
	"city": "Shelburne",
	"state": "VT",
	"postal_code": "05482",
	"country": "US",
	"primary": True,
	"phone_number": "+15555550100",
}


class FakeResponse:
	def __init__(self, status_code=200, payload=None):
		self.status_code = status_code
		self.payload = payload or {}

	def json(self):
		return self.payload

	def raise_for_status(self):
		if self.status_code >= 400:
			raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
	"""Stands in for authlib's OAuth2Session: hands back queued responses and
	records the refreshes fetch_addresses asks for."""

	def __init__(self, responses, refreshed_token=None, refresh_error=None, **kwargs):
		self.responses = list(responses)
		self.refreshed_token = refreshed_token
		self.refresh_error = refresh_error
		self.init_kwargs = kwargs
		self.update_token = kwargs.get("update_token")
		self.requested_urls = []
		self.refreshed_with = []
		self.closed = False

	def get(self, url, timeout=None):
		self.requested_urls.append(url)
		response = self.responses.pop(0)
		if isinstance(response, Exception):
			raise response
		return response

	def refresh_token(self, url, refresh_token=None):
		self.refreshed_with.append(refresh_token)
		if self.refresh_error:
			raise self.refresh_error
		self.update_token(self.refreshed_token)
		return self.refreshed_token

	def close(self):
		self.closed = True


class StorableTokenTests(TestCase):
	def test_keeps_only_reusable_fields(self):
		token = storable_token({
			"access_token": "at",
			"refresh_token": "rt",
			"token_type": "Bearer",
			"expires_at": 1893456000,
			"scope": "openid address",
			"id_token": "jwt",
			"userinfo": {"sub": "user!1"},
		})
		self.assertEqual(token, {
			"access_token": "at",
			"refresh_token": "rt",
			"token_type": "Bearer",
			"expires_at": 1893456000,
			"scope": "openid address",
		})

	def test_token_without_access_token_is_not_storable(self):
		self.assertEqual(storable_token({"userinfo": {"sub": "user!1"}}), {})
		self.assertEqual(storable_token(None), {})


class ExtractAddressesTests(TestCase):
	def test_strips_phone_number(self):
		[address] = extract_addresses({"addresses": [ADDRESS]})
		self.assertNotIn("phone_number", address)
		self.assertEqual(address["id"], "adr_1")

	def test_reads_addresses_nested_under_identity(self):
		addresses = extract_addresses({"identity": {"addresses": [ADDRESS, ADDRESS]}})
		self.assertEqual(len(addresses), 2)

	def test_single_address_claim_is_wrapped(self):
		self.assertEqual(len(extract_addresses({"address": ADDRESS})), 1)
		self.assertEqual(len(extract_addresses({"addresses": ADDRESS})), 1)

	def test_missing_or_malformed_gives_nothing(self):
		for payload in (None, {}, "nope", {"address": {}}, {"addresses": ["a string"]}):
			with self.subTest(payload=payload):
				self.assertEqual(extract_addresses(payload), [])


@override_settings(ADDRESS_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
class FetchAddressesTests(TestCase):
	def setUp(self):
		super().setUp()
		self.user = make_user("buyer", hca_token=TOKEN)
		self.profile = self.user.hackclub_profile

		patcher = patch.object(
			hca.oauth.hackclub, "load_server_metadata", return_value=HCA_METADATA
		)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _fetch(self, responses, **session_kwargs):
		"""Run fetch_addresses against a FakeSession, returning (result, session)."""
		sessions = []

		def build_session(**kwargs):
			session = FakeSession(responses, **{**session_kwargs, **kwargs})
			sessions.append(session)
			return session

		with patch.object(hca, "OAuth2Session", side_effect=build_session):
			try:
				return fetch_addresses(self.profile), sessions[0]
			except AddressUnavailable:
				if sessions:
					self.assertTrue(sessions[0].closed, "session was left open")
				raise

	def _userinfo(self):
		return FakeResponse(payload={"sub": "user!1", "addresses": [ADDRESS]})

	def test_profile_without_token_never_calls_hca(self):
		self.profile.save_hca_token({})
		with patch.object(hca, "OAuth2Session") as session_cls:
			with self.assertRaises(AddressUnavailable):
				fetch_addresses(self.profile)
		session_cls.assert_not_called()

	def test_fetches_address_with_stored_token(self):
		addresses, session = self._fetch([self._userinfo()])

		self.assertEqual(len(addresses), 1)
		self.assertEqual(addresses[0]["postal_code"], "05482")
		self.assertNotIn("phone_number", addresses[0])
		self.assertEqual(session.requested_urls, [HCA_METADATA["userinfo_endpoint"]])
		self.assertEqual(session.init_kwargs["token"], TOKEN)
		self.assertTrue(session.closed)

	def test_rejected_token_is_refreshed_and_retried(self):
		refreshed = {"access_token": "at-new", "refresh_token": "rt-new"}
		addresses, session = self._fetch(
			[FakeResponse(status_code=401), self._userinfo()],
			refreshed_token=refreshed,
		)

		self.assertEqual(len(addresses), 1)
		self.assertEqual(session.refreshed_with, ["rt-old"])
		self.assertEqual(len(session.requested_urls), 2)

	def test_refreshed_token_is_persisted_encrypted(self):
		refreshed = {"access_token": "at-new", "refresh_token": "rt-new", "id_token": "jwt"}
		self._fetch(
			[FakeResponse(status_code=401), self._userinfo()],
			refreshed_token=refreshed,
		)

		self.profile.refresh_from_db()
		self.assertNotIn("at-new", self.profile.encrypted_hca_token)
		self.assertEqual(
			self.profile.get_hca_token(),
			{"access_token": "at-new", "refresh_token": "rt-new"},
		)

	def test_rejected_token_without_refresh_token_gives_up(self):
		self.profile.save_hca_token({"access_token": "at-old"})
		with self.assertRaises(AddressUnavailable):
			self._fetch([FakeResponse(status_code=401)])

	def test_failed_refresh_surfaces_as_unavailable(self):
		with self.assertRaises(AddressUnavailable):
			self._fetch(
				[FakeResponse(status_code=401)],
				refresh_error=RuntimeError("invalid_grant"),
			)

	def test_unreachable_identity_service_surfaces_as_unavailable(self):
		with self.assertRaises(AddressUnavailable):
			self._fetch([ConnectionError("hca down")])

	def test_server_error_surfaces_as_unavailable(self):
		with self.assertRaises(AddressUnavailable):
			self._fetch([FakeResponse(status_code=500)])


@override_settings(ADDRESS_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
class ProfileTokenStorageTests(TestCase):
	def test_token_is_encrypted_at_rest_and_round_trips(self):
		profile = make_user("tokened", hca_token=TOKEN).hackclub_profile
		profile.refresh_from_db()

		self.assertNotIn("at-old", profile.encrypted_hca_token)
		self.assertNotIn("rt-old", profile.encrypted_hca_token)
		self.assertEqual(profile.get_hca_token(), TOKEN)

	def test_unset_token_reads_back_empty(self):
		self.assertEqual(make_user("tokenless").hackclub_profile.get_hca_token(), {})

	def test_unreadable_token_reads_back_empty(self):
		profile = make_user("corrupt").hackclub_profile
		profile.encrypted_hca_token = "not-a-fernet-token"
		self.assertEqual(profile.get_hca_token(), {})

	def test_saving_a_token_touches_no_other_column(self):
		profile = make_user("racer", layers=10).hackclub_profile
		profile.layers = 999  # an unsaved edit a concurrent refresh must not flush
		profile.save_hca_token(TOKEN)

		reloaded = type(profile).objects.get(pk=profile.pk)
		self.assertEqual(reloaded.layers, 10)
		self.assertEqual(reloaded.get_hca_token(), TOKEN)


class ProfileAddressSelectionTests(TestCase):
	def setUp(self):
		super().setUp()
		self.profile = make_user("picker").hackclub_profile
		self.addresses = [
			{"id": "adr_1", "line_1": "First"},
			{"id": "adr_2", "line_1": "Primary", "primary": True},
		]
		patcher = patch.object(hca, "fetch_addresses", side_effect=lambda profile: self.addresses)
		self.fetch = patcher.start()
		self.addCleanup(patcher.stop)

	def test_matching_id_wins(self):
		self.assertEqual(self.profile.get_address("adr_1")["line_1"], "First")

	def test_unknown_id_falls_back_to_primary(self):
		self.assertEqual(self.profile.get_address("adr_gone")["line_1"], "Primary")

	def test_primary_wins_when_no_id_given(self):
		self.assertEqual(self.profile.get_address()["line_1"], "Primary")
		self.assertEqual(self.profile.primary_address_id, "adr_2")

	def test_first_address_used_when_none_is_primary(self):
		self.addresses = [{"id": "adr_9", "line_1": "Only"}]
		self.assertEqual(self.profile.get_address()["line_1"], "Only")

	def test_no_addresses_gives_none(self):
		self.addresses = []
		self.assertIsNone(self.profile.get_address())
		self.assertEqual(self.profile.primary_address_id, "")

	def test_addresses_are_fetched_every_time_not_cached(self):
		self.profile.get_address()
		self.profile.get_address()
		self.assertEqual(self.fetch.call_count, 2)
