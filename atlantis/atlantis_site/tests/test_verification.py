from unittest.mock import patch

from django.test import TestCase, override_settings

from .. import hca
from ..hca import IdentityUnavailable, extract_verification, refresh_verification
from ..models import Profile
from .base import TEST_ENCRYPTION_KEY, make_user


class ExtractVerificationTests(TestCase):
	def test_reads_both_claims(self):
		self.assertEqual(
			extract_verification({"verification_status": "verified", "ysws_eligible": True}),
			("verified", True),
		)

	def test_reads_claims_nested_under_identity(self):
		payload = {"identity": {"verification_status": "pending", "ysws_eligible": False}}
		self.assertEqual(extract_verification(payload), ("pending", False))

	def test_missing_claims_give_nothing(self):
		for payload in ({}, {"sub": "user!1"}, None, "nope"):
			with self.subTest(payload=payload):
				self.assertEqual(extract_verification(payload), ("", None))

	def test_non_boolean_eligibility_is_not_read_as_a_verdict(self):
		"""A string or a null must not pass for a yes or a no — the door only
		closes on a real False."""
		for raw in ("true", "false", 0, 1, None):
			with self.subTest(raw=raw):
				payload = {"verification_status": "verified", "ysws_eligible": raw}
				self.assertEqual(extract_verification(payload), ("verified", None))

	def test_non_string_status_is_dropped(self):
		self.assertEqual(extract_verification({"verification_status": 7}), ("", None))


class ProfileEligibilityTests(TestCase):
	CASES = [
		("verified and eligible", "verified", True, True),
		("verified, no verdict yet", "verified", None, True),
		("verified but ineligible", "verified", False, False),
		("under review", "pending", None, False),
		("nothing submitted", "needs_submission", None, False),
		("turned down", "ineligible", False, False),
		("never told", "", None, False),
	]

	def test_only_a_verified_identity_without_a_no_is_eligible(self):
		for label, status, eligible, expected in self.CASES:
			with self.subTest(label=label):
				profile = Profile(verification_status=status, ysws_eligible=eligible)
				self.assertEqual(profile.is_ysws_eligible, expected)


@override_settings(ADDRESS_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
class RefreshVerificationTests(TestCase):
	def setUp(self):
		super().setUp()
		self.profile = make_user(
			"asker", hca_token={"access_token": "at-1"},
			verification_status="pending", ysws_eligible=None,
		).hackclub_profile

	def _refresh(self, userinfo):
		with patch.object(hca, "fetch_userinfo", return_value=userinfo):
			return refresh_verification(self.profile)

	def test_stores_what_hca_says(self):
		result = self._refresh({"verification_status": "verified", "ysws_eligible": True})

		self.assertEqual(result, ("verified", True))
		reloaded = Profile.objects.get(pk=self.profile.pk)
		self.assertEqual(reloaded.verification_status, "verified")
		self.assertTrue(reloaded.ysws_eligible)

	def test_stores_a_refusal_too(self):
		self._refresh({"verification_status": "verified", "ysws_eligible": False})

		reloaded = Profile.objects.get(pk=self.profile.pk)
		self.assertFalse(reloaded.is_ysws_eligible)

	def test_touches_no_other_column(self):
		self.profile.layers = 999  # an unsaved edit a concurrent refresh must not flush
		self._refresh({"verification_status": "verified", "ysws_eligible": True})

		reloaded = Profile.objects.get(pk=self.profile.pk)
		self.assertEqual(reloaded.layers, 0)
		self.assertEqual(reloaded.verification_status, "verified")

	def test_unreachable_hca_leaves_the_stored_answer_alone(self):
		with patch.object(hca, "fetch_userinfo", side_effect=IdentityUnavailable("down")):
			with self.assertRaises(IdentityUnavailable):
				refresh_verification(self.profile)

		reloaded = Profile.objects.get(pk=self.profile.pk)
		self.assertEqual(reloaded.verification_status, "pending")
		self.assertIsNone(reloaded.ysws_eligible)

	def test_profile_without_a_token_never_reaches_hca(self):
		self.profile.save_hca_token({})
		with self.assertRaises(IdentityUnavailable):
			refresh_verification(self.profile)
