import os
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from .. import airtable
from ..airtable import AirtableRequestFailed, email_contact, upsert_emails
from ..models import AuditLog
from .base import BaseTestCase, User, grant_perms, make_user, message_texts
from .test_airtable import FakeResponse
from .test_auth import SLACK_USER_RESPONSE, TOKEN, USERINFO

EMAILS_SETTINGS = dict(
	AIRTABLE_PAT="pat-test",
	AIRTABLE_BASE_ID="appTest",
	AIRTABLE_EMAILS_TABLE_ID="tblEmails",
	AIRTABLE_API_BASE_URL="https://api.airtable.com/v0",
)


def run_inline(target):
	target()


@override_settings(**EMAILS_SETTINGS)
@patch.object(airtable.time, "sleep")
class UpsertEmailsTests(TestCase):
	def test_sends_only_name_and_email_keyed_on_email(self, _sleep):
		with patch.object(airtable.requests, "patch", return_value=FakeResponse(200, {"records": []})) as patch_:
			self.assertEqual(upsert_emails([("Ada Lovelace", "ada@example.com")]), 1)

		url = patch_.call_args[0][0]
		body = patch_.call_args.kwargs["json"]
		self.assertEqual(url, "https://api.airtable.com/v0/appTest/tblEmails")
		self.assertEqual(body["performUpsert"], {"fieldsToMergeOn": ["Email"]})
		self.assertEqual(body["records"], [{"fields": {"Name": "Ada Lovelace", "Email": "ada@example.com"}}])

	def test_batches_by_ten(self, _sleep):
		people = [(f"P{i}", f"p{i}@example.com") for i in range(23)]
		with patch.object(airtable.requests, "patch", return_value=FakeResponse(200, {})) as patch_:
			self.assertEqual(upsert_emails(people), 23)
		self.assertEqual([len(c.kwargs["json"]["records"]) for c in patch_.call_args_list], [10, 10, 3])

	def test_retries_after_rate_limit(self, sleep):
		responses = [FakeResponse(429, {"errors": []}), FakeResponse(200, {})]
		with patch.object(airtable.requests, "patch", side_effect=responses) as patch_:
			upsert_emails([("Ada", "ada@example.com")])
		self.assertEqual(patch_.call_count, 2)
		sleep.assert_called_with(airtable._RATE_LIMIT_WAIT)

	def test_error_does_not_echo_the_body(self, _sleep):
		response = FakeResponse(422, {"error": {"type": "INVALID_VALUE_FOR_COLUMN", "message": "ada@example.com"}})
		with patch.object(airtable.requests, "patch", return_value=response):
			with self.assertRaises(AirtableRequestFailed) as ctx:
				upsert_emails([("Ada", "ada@example.com")])
		self.assertNotIn("ada@example.com", str(ctx.exception))


class EmailContactTests(BaseTestCase):
	def test_full_name_and_email(self):
		user = make_user("ada", email="ada@example.com", first_name="Ada", last_name="Lovelace")
		self.assertEqual(email_contact(user), ("Ada Lovelace", "ada@example.com"))

	def test_no_real_address_is_skipped(self):
		self.assertIsNone(email_contact(make_user("blank", email="")))
		self.assertIsNone(email_contact(make_user("placeholder", email="hackclubber@example.com")))


@override_settings(**EMAILS_SETTINGS)
@patch.dict(os.environ, {"DEFAULT_PFP": "https://example.com/default.png"})
@patch.object(airtable, "_start", run_inline)
class BackfillEmailsViewTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.organizer = grant_perms(
			make_user("organizer", email="org@example.com", first_name="Org", last_name="Anizer"),
			"organizer",
		)

	def test_non_organizer_cannot_backfill(self):
		self.client.force_login(make_user("pleb"))
		with patch.object(airtable, "upsert_emails") as upsert:
			self.client.post(reverse("backfill_emails"))
		upsert.assert_not_called()

	def test_get_not_allowed(self):
		self.client.force_login(self.organizer)
		self.assertEqual(self.client.get(reverse("backfill_emails")).status_code, 405)

	def test_sends_every_user_once(self):
		make_user("ada", email="ada@example.com", first_name="Ada", last_name="Lovelace")
		make_user("ada2", email="ADA@example.com", first_name="Ada", last_name="Again")
		make_user("nobody", email="")
		self.client.force_login(self.organizer)

		with patch.object(airtable, "upsert_emails", return_value=2) as upsert:
			response = self.client.post(reverse("backfill_emails"))

		self.assertRedirects(response, reverse("users"), fetch_redirect_response=False)
		self.assertEqual(upsert.call_args[0][0], [
			("Org Anizer", "org@example.com"),
			("Ada Lovelace", "ada@example.com"),
		])
		self.assertTrue(AuditLog.objects.filter(action="backfill_emails").exists())

	@override_settings(AIRTABLE_EMAILS_TABLE_ID="")
	def test_unconfigured_reports_and_sends_nothing(self):
		self.client.force_login(self.organizer)
		with patch.object(airtable, "upsert_emails") as upsert:
			response = self.client.post(reverse("backfill_emails"), follow=True)
		upsert.assert_not_called()
		self.assertTrue(any("AIRTABLE_EMAILS_TABLE_ID" in m for m in message_texts(response)))


@override_settings(**EMAILS_SETTINGS)
@patch.dict(os.environ, {"DEFAULT_PFP": "https://example.com/default.png"})
@patch.object(airtable, "_start", run_inline)
@patch("atlantis_site.views.client.auth.slack_client.users_info", return_value=SLACK_USER_RESPONSE)
@patch("atlantis_site.views.client.auth.oauth.hackclub.authorize_access_token")
class SignupEmailTests(TestCase):
	def _callback(self, mock_token, **userinfo):
		mock_token.return_value = {"userinfo": {**USERINFO, **userinfo}, **TOKEN}
		return self.client.get(reverse("auth_callback"))

	def test_new_signup_is_sent(self, mock_token, _slack):
		with patch.object(airtable, "upsert_emails") as upsert:
			self._callback(mock_token)
		upsert.assert_called_once_with([("Test Person", "tester@example.com")])

	def test_returning_user_is_not_resent(self, mock_token, _slack):
		User.objects.create_user(username="user_abc123", email="tester@example.com")
		with patch.object(airtable, "upsert_emails") as upsert:
			self._callback(mock_token)
		upsert.assert_not_called()

	def test_airtable_failure_does_not_block_login(self, mock_token, _slack):
		with patch.object(airtable, "upsert_emails", side_effect=AirtableRequestFailed("boom")):
			response = self._callback(mock_token)
		self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
