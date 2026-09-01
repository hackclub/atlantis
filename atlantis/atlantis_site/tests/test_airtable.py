from io import StringIO
from unittest.mock import patch

import requests
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from .. import airtable, submissions
from ..airtable import (
	AirtableNotConfigured, AirtableRequestFailed, AirtableUnknownOutcome,
	create_record, is_configured, missing_settings,
)
from ..hca import AddressUnavailable, IdentityUnavailable, extract_birthdate
from ..models import AirtableSubmission, AuditLog, Ship, T2, T3
from ..submissions import (
	FIELDS, LAPSE_HEADING, NotFinalized, build_fields,
	build_override_justification, pending_ships, submit_ship,
)
from .base import (
	BaseTestCase,
	approve_timelapse,
	grant_perms,
	make_journal,
	make_project,
	make_ship,
	make_timelapse,
	make_user,
	message_texts,
)

AIRTABLE_SETTINGS = dict(
	AIRTABLE_PAT="pat-test",
	AIRTABLE_BASE_ID="appTest",
	AIRTABLE_TABLE_ID="tblTest",
	AIRTABLE_API_BASE_URL="https://api.airtable.com/v0",
)

USERINFO = {
	"sub": "user!1",
	"birthdate": "2009-04-17",
	"addresses": [{
		"id": "adr_1",
		"first_name": "Ada",
		"last_name": "Shipper",
		"line_1": "15 Falls Rd",
		"line_2": "Apt 3",
		"city": "Shelburne",
		"state": "VT",
		"postal_code": "05482",
		"country": "US",
		"primary": True,
	}],
}


class FakeResponse:
	def __init__(self, status_code=200, payload=None, text=""):
		self.status_code = status_code
		self.ok = status_code < 400
		self.payload = payload
		self.text = text

	def json(self):
		if self.payload is None:
			raise ValueError("not json")
		return self.payload


@override_settings(**AIRTABLE_SETTINGS)
class AirtableClientTests(TestCase):
	def _post(self, response):
		return patch.object(
			airtable.requests,
			"post",
			return_value=response if not isinstance(response, Exception) else None,
			side_effect=response if isinstance(response, Exception) else None,
		)

	def test_configuration_is_reported_not_guessed(self):
		self.assertEqual(missing_settings(), [])
		self.assertTrue(is_configured())
		with override_settings(AIRTABLE_PAT="", AIRTABLE_TABLE_ID=""):
			self.assertEqual(missing_settings(), ["AIRTABLE_PAT", "AIRTABLE_TABLE_ID"])
			self.assertFalse(is_configured())

	def test_missing_credentials_send_nothing(self):
		with override_settings(AIRTABLE_PAT=""):
			with patch.object(airtable.requests, "post") as post:
				with self.assertRaises(AirtableNotConfigured):
					create_record({"Code URL": "x"})
			post.assert_not_called()

	def test_record_is_created_with_typecast_and_bearer_token(self):
		with self._post(FakeResponse(payload={"id": "rec123"})) as post:
			self.assertEqual(create_record({"Code URL": "x"}), "rec123")

		_, kwargs = post.call_args
		self.assertEqual(post.call_args[0][0], airtable.records_url())
		self.assertEqual(kwargs["json"], {"fields": {"Code URL": "x"}, "typecast": True})
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer pat-test")

	def test_error_status_is_retryable_and_names_the_error_type(self):
		body = {"error": {"type": "INVALID_VALUE_FOR_COLUMN", "message": "..."}}
		with self._post(FakeResponse(status_code=422, payload=body)):
			with self.assertRaises(AirtableRequestFailed) as caught:
				create_record({})
		self.assertIn("422", str(caught.exception))
		self.assertIn("INVALID_VALUE_FOR_COLUMN", str(caught.exception))

	def test_a_rejected_value_is_not_quoted_back_into_the_error(self):
		# Airtable echoes the value it refused. On this table that is somebody's
		# address or birthday, and the error text ends up in the database and in
		# front of a reviewer.
		body = {"error": {
			"type": "INVALID_VALUE_FOR_COLUMN",
			"message": 'Field "Birthday" cannot accept the value "15 Falls Rd"',
		}}
		with self._post(FakeResponse(status_code=422, payload=body)):
			with self.assertRaises(AirtableRequestFailed) as caught:
				create_record({})
		self.assertNotIn("15 Falls Rd", str(caught.exception))

	def test_an_unparseable_error_body_is_not_passed_on_either(self):
		with self._post(FakeResponse(status_code=500, text="15 Falls Rd, Shelburne")):
			with self.assertRaises(AirtableRequestFailed) as caught:
				create_record({})
		self.assertNotIn("Falls Rd", str(caught.exception))

	def test_transport_failure_is_an_unknown_outcome(self):
		with self._post(requests.ConnectionError("boom")):
			with self.assertRaises(AirtableUnknownOutcome):
				create_record({})

	def test_unreadable_success_is_an_unknown_outcome(self):
		for response in (FakeResponse(payload=None), FakeResponse(payload={})):
			with self.subTest(response=response.payload):
				with self._post(response):
					with self.assertRaises(AirtableUnknownOutcome):
						create_record({})

	def test_token_never_appears_in_the_error_shown_to_a_reviewer(self):
		with self._post(FakeResponse(status_code=401, text="unauthorized")):
			with self.assertRaises(AirtableRequestFailed) as caught:
				create_record({})
		self.assertNotIn("pat-test", str(caught.exception))


class BirthdateTests(TestCase):
	def test_iso_date_is_kept(self):
		self.assertEqual(extract_birthdate({"birthdate": "2009-04-17"}), "2009-04-17")

	def test_read_from_nested_identity(self):
		self.assertEqual(
			extract_birthdate({"identity": {"birthdate": "2009-04-17"}}), "2009-04-17"
		)

	def test_unusable_values_are_dropped(self):
		# A bare year is a legal OIDC birthdate but not a date Airtable can take,
		# and a wrong guess at the month would be worse than no birthday.
		for payload in (None, {}, "nope", {"birthdate": ""}, {"birthdate": "2009"},
						{"birthdate": "17/04/2009"}, {"birthdate": 20090417}):
			with self.subTest(payload=payload):
				self.assertEqual(extract_birthdate(payload), "")

	def test_scope_asks_for_the_claim(self):
		# The claim has its own scope on HCA; `profile` does not carry it.
		from ..hca import HCA_SCOPE
		self.assertIn("birthdate", HCA_SCOPE.split())

	def test_address_unavailable_is_still_an_identity_failure(self):
		self.assertTrue(issubclass(AddressUnavailable, IdentityUnavailable))


class JustificationTests(BaseTestCase):
	"""The override-hours justification is the only place the internal timelapse
	review is ever seen outside Atlantis, so its contents are load-bearing."""

	def setUp(self):
		super().setUp()
		self.author = make_user("author")
		self.project = make_project(self.author, shippable=True)
		self.ship = Ship.objects.create(
			project=self.project, status=Ship.ShipStatus.T3_QUEUE
		)
		self.journal = make_journal(self.project, ship=self.ship, time_spent=0)
		self.session = make_timelapse(self.project, journal=self.journal, minutes=120)
		approve_timelapse(
			self.journal,
			removals=[(self.session, 300, 1800, "idle, nothing on screen")],
			internal_notes="footage checks out, cut the idle stretch",
		)
		T2.objects.create(
			ship=self.ship, reviewer=make_user("t2rev"), decision=T2.Decision.APPROVE,
			deductions=10, feedback="nice", justification="Solid build, hours check out.",
		)

	def test_t2_justification_opens_it_verbatim(self):
		text = build_override_justification(self.ship)
		self.assertTrue(text.startswith("Solid build, hours check out."))
		self.assertLess(text.index("Solid build"), text.index(LAPSE_HEADING))

	def test_lapse_link_removed_range_and_reason_are_appended(self):
		text = build_override_justification(self.ship)
		self.assertIn(self.session.watch_url, text)
		self.assertIn("5:00-30:00", text)
		self.assertIn("idle, nothing on screen", text)
		self.assertIn(self.journal.title, text)

	def test_timelapse_reviewer_justification_is_appended(self):
		text = build_override_justification(self.ship)
		self.assertIn("footage checks out, cut the idle stretch", text)

	def test_deductions_and_removals_are_both_accounted_for(self):
		T3.objects.create(
			ship=self.ship, reviewer=make_user("t3rev2"),
			decision=T3.Decision.APPROVE, payout_time=95, airtable_time=95,
		)
		text = build_override_justification(self.ship)
		self.assertIn("Tracked by Lapse: 2h 0m", text)
		self.assertIn("Removed in internal timelapse review: 0h 25m", text)
		self.assertIn("Deducted by T2 review: 0h 10m", text)
		self.assertIn("Submitted hours: 1.6", text)

	def test_timelapses_with_nothing_removed_are_still_listed(self):
		journal = make_journal(self.project, ship=self.ship, time_spent=0)
		session = make_timelapse(self.project, journal=journal, minutes=30)
		approve_timelapse(journal)

		text = build_override_justification(self.ship)
		self.assertIn(session.watch_url, text)
		self.assertIn("nothing removed", text)

	def test_a_ship_with_no_t2_review_still_gets_the_audit(self):
		self.ship.t2_reviews.all().delete()
		text = build_override_justification(self.ship)
		self.assertTrue(text.startswith(LAPSE_HEADING))
		self.assertIn(self.session.watch_url, text)

	def test_latest_t2_justification_is_the_one_quoted(self):
		T2.objects.create(
			ship=self.ship, reviewer=make_user("t2rev2"), decision=T2.Decision.APPROVE,
			deductions=10, feedback="", justification="Second pass, still fine.",
		)
		self.assertTrue(
			build_override_justification(self.ship).startswith("Second pass, still fine.")
		)


@override_settings(**AIRTABLE_SETTINGS)
class BuildFieldsTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.author = make_user(
			"author", first_name="Fallback", last_name="Name",
			email="ada@example.com", hca_token={"access_token": "at"},
		)
		self.project = make_project(self.author, shippable=True, description="A widget.")
		self.ship = make_ship(self.project, status=Ship.ShipStatus.FINALIZED)
		T2.objects.create(
			ship=self.ship, reviewer=make_user("t2rev"), decision=T2.Decision.APPROVE,
			deductions=0, feedback="", justification="Hours look right.",
		)
		T3.objects.create(
			ship=self.ship, reviewer=make_user("t3rev"), decision=T3.Decision.APPROVE,
			payout_time=240, airtable_time=250,
		)

	def _build(self, userinfo=USERINFO, notes=None):
		with patch.object(submissions, "fetch_userinfo", return_value=userinfo):
			return build_fields(self.ship, notes if notes is not None else [])

	def test_every_requested_column_is_populated(self):
		fields = self._build()
		self.assertEqual(fields[FIELDS["code"]], self.project.printablesUrl)
		self.assertEqual(fields[FIELDS["demo"]], self.project.editor_model_url)
		self.assertEqual(fields[FIELDS["first_name"]], "Ada")
		self.assertEqual(fields[FIELDS["last_name"]], "Shipper")
		self.assertEqual(fields[FIELDS["email"]], "ada@example.com")
		self.assertEqual(fields[FIELDS["description"]], "A widget.")
		self.assertEqual(fields[FIELDS["address_line_1"]], "15 Falls Rd")
		self.assertEqual(fields[FIELDS["address_line_2"]], "Apt 3")
		self.assertEqual(fields[FIELDS["city"]], "Shelburne")
		self.assertEqual(fields[FIELDS["state"]], "VT")
		self.assertEqual(fields[FIELDS["country"]], "US")
		self.assertEqual(fields[FIELDS["zip"]], "05482")
		self.assertEqual(fields[FIELDS["birthday"]], "2009-04-17")
		self.assertIn("Hours look right.", fields[FIELDS["override_justification"]])

	def test_screenshot_is_sent_as_an_attachment_url(self):
		[attachment] = self._build()[FIELDS["screenshot"]]
		self.assertIn("screenshot.png", attachment["url"])

	def test_stored_keys_become_absolute_urls_and_links_pass_through(self):
		# The bucket has no public hostname, so an uploaded model has to be
		# turned into a fetchable URL; an editor's own share link must not be.
		self.project.editor_model_url = "editor_models/thing.f3d"
		self.project.save()
		self.assertTrue(self._build()[FIELDS["demo"]].startswith("/media/"))

		self.project.editor_model_url = "https://cad.onshape.com/documents/abc"
		self.project.save()
		self.assertEqual(
			self._build()[FIELDS["demo"]], "https://cad.onshape.com/documents/abc"
		)

	def test_override_hours_come_from_the_t3_airtable_time_in_hours(self):
		self.assertEqual(self._build()[FIELDS["override_hours"]], 4.2)

	def test_zero_hours_are_sent_rather_than_dropped(self):
		self.ship.t3_reviews.update(airtable_time=0)
		self.assertEqual(self._build()[FIELDS["override_hours"]], 0)

	def test_name_falls_back_to_the_account_when_the_address_has_none(self):
		fields = self._build({"addresses": [{"id": "a", "line_1": "1 Road"}]})
		self.assertEqual(fields[FIELDS["first_name"]], "Fallback")
		self.assertEqual(fields[FIELDS["last_name"]], "Name")

	def test_missing_identity_is_noted_not_fatal(self):
		notes = []
		with patch.object(
			submissions, "fetch_userinfo", side_effect=IdentityUnavailable("no token")
		):
			fields = build_fields(self.ship, notes)

		self.assertNotIn(FIELDS["birthday"], fields)
		self.assertNotIn(FIELDS["address_line_1"], fields)
		self.assertEqual(fields[FIELDS["code"]], self.project.printablesUrl)
		self.assertTrue(any("no token" in note for note in notes))

	def test_missing_birthdate_is_noted(self):
		notes = []
		self._build({"addresses": USERINFO["addresses"]}, notes=notes)
		self.assertTrue(any("birthdate" in note for note in notes))

	def test_empty_values_are_omitted_rather_than_sent_blank(self):
		self.project.printablesUrl = ""
		self.project.image_url = ""
		self.project.save()
		fields = self._build()
		self.assertNotIn(FIELDS["code"], fields)
		self.assertNotIn(FIELDS["screenshot"], fields)


@override_settings(**AIRTABLE_SETTINGS)
class SubmitShipTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.author = make_user("author", hca_token={"access_token": "at"})
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project, status=Ship.ShipStatus.FINALIZED)
		T3.objects.create(
			ship=self.ship, reviewer=make_user("t3rev"), decision=T3.Decision.APPROVE,
			payout_time=120, airtable_time=120,
		)
		patcher = patch.object(submissions, "fetch_userinfo", return_value=USERINFO)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _create_record(self, **kwargs):
		return patch.object(airtable, "create_record", **kwargs)

	def test_unfinalized_ships_are_never_submitted(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE, journal_minutes=())
		with self._create_record() as create:
			with self.assertRaises(NotFinalized):
				submit_ship(ship)
		create.assert_not_called()
		self.assertEqual(AirtableSubmission.objects.count(), 0)

	def test_successful_submission_records_the_id(self):
		with self._create_record(return_value="recABC"):
			submission = submit_ship(self.ship)

		self.assertEqual(submission.status, AirtableSubmission.Status.SUBMITTED)
		self.assertEqual(submission.record_id, "recABC")
		self.assertEqual(submission.attempts, 1)
		self.assertIsNotNone(submission.submitted_at)
		self.assertTrue(submission.is_submitted)
		self.assertFalse(submission.needs_retry)

	def test_a_second_call_does_not_create_a_second_record(self):
		with self._create_record(return_value="recABC") as create:
			submit_ship(self.ship)
			submission = submit_ship(self.ship)

		create.assert_called_once()
		self.assertEqual(AirtableSubmission.objects.count(), 1)
		self.assertEqual(submission.record_id, "recABC")
		self.assertEqual(submission.attempts, 1)

	def test_rejected_submission_stays_retryable(self):
		with self._create_record(side_effect=AirtableRequestFailed("422: bad field")):
			submission = submit_ship(self.ship)

		self.assertEqual(submission.status, AirtableSubmission.Status.FAILED)
		self.assertIn("422", submission.error)
		self.assertTrue(submission.needs_retry)
		self.assertIn(self.ship, list(pending_ships()))

		with self._create_record(return_value="recABC"):
			submission = submit_ship(self.ship)
		self.assertEqual(submission.record_id, "recABC")
		self.assertEqual(submission.attempts, 2)

	def test_unresolved_submission_is_not_retried_automatically(self):
		# The request went out and we never heard back, so a retry is exactly how
		# a duplicate record gets made. It waits for a human instead.
		with self._create_record(side_effect=AirtableUnknownOutcome("timed out")):
			submission = submit_ship(self.ship)

		self.assertEqual(submission.status, AirtableSubmission.Status.SENDING)
		self.assertFalse(submission.needs_retry)
		self.assertNotIn(self.ship, list(pending_ships()))

		with self._create_record() as create:
			submit_ship(self.ship)
		create.assert_not_called()

	def test_missing_credentials_fail_gracefully(self):
		with override_settings(AIRTABLE_PAT="", AIRTABLE_BASE_ID=""):
			submission = submit_ship(self.ship)

		self.assertEqual(submission.status, AirtableSubmission.Status.FAILED)
		self.assertIn("AIRTABLE_PAT", submission.error)
		self.assertTrue(submission.needs_retry)

	def test_a_broken_payload_does_not_leave_the_row_wedged(self):
		with patch.object(submissions, "build_fields", side_effect=RuntimeError("boom")):
			submission = submit_ship(self.ship)

		self.assertEqual(submission.status, AirtableSubmission.Status.FAILED)
		self.assertTrue(submission.needs_retry)

	def test_notes_survive_onto_the_row_but_personal_data_does_not(self):
		with patch.object(
			submissions, "fetch_userinfo", side_effect=IdentityUnavailable("no token")
		):
			with self._create_record(return_value="recABC"):
				submission = submit_ship(self.ship)

		self.assertIn("no token", submission.notes)
		row = " ".join([submission.notes, submission.error])
		for personal in ("15 Falls Rd", "2009-04-17", "Shelburne", "05482"):
			self.assertNotIn(personal, row)

	def test_pending_ships_skips_ships_already_submitted(self):
		with self._create_record(return_value="recABC"):
			submit_ship(self.ship)
		self.assertNotIn(self.ship, list(pending_ships()))

	def test_pending_ships_includes_finalized_ships_never_attempted(self):
		self.assertIn(self.ship, list(pending_ships()))

	def test_pending_ships_ignores_ships_still_under_review(self):
		ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE, journal_minutes=())
		self.assertNotIn(ship, list(pending_ships()))


@override_settings(**AIRTABLE_SETTINGS)
class T3FinalizationSubmitsTests(BaseTestCase):
	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t3rev"), "t3_review")
		self.client.force_login(self.reviewer)
		self.author = make_user("author", slack_id="U0AUTHOR", hca_token={"access_token": "at"})
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project, status=Ship.ShipStatus.T3_QUEUE)
		T2.objects.create(
			ship=self.ship, reviewer=self.reviewer, decision=T2.Decision.APPROVE,
			deductions=0, feedback="", justification="Hours look right.",
		)
		patcher = patch.object(submissions, "fetch_userinfo", return_value=USERINFO)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _decide(self, decision=T3.Decision.APPROVE, ship=None):
		return self.client.post(reverse("t3_decision", args=[(ship or self.ship).id]), {
			"decision": decision,
			"internal_notes": "clean",
			"payout_time": "240",
			"airtable_time": "240",
		})

	def test_approval_submits_the_project(self):
		with patch.object(airtable, "create_record", return_value="recABC") as create:
			response = self._decide()

		fields = create.call_args[0][0]
		self.assertEqual(fields[FIELDS["override_hours"]], 4.0)
		self.assertIn("Hours look right.", fields[FIELDS["override_justification"]])
		self.assertEqual(
			AirtableSubmission.objects.get(ship=self.ship).record_id, "recABC"
		)
		self.assertIn("Submitted to Airtable as recABC.", message_texts(response))

	def test_returns_submit_nothing(self):
		for decision in (T3.Decision.RETURN_T1, T3.Decision.RETURN_T2):
			with self.subTest(decision=decision):
				ship = make_ship(
					self.project, status=Ship.ShipStatus.T3_QUEUE, journal_minutes=()
				)
				with patch.object(airtable, "create_record") as create:
					self._decide(decision=decision, ship=ship)
				create.assert_not_called()
		self.assertEqual(AirtableSubmission.objects.count(), 0)

	def test_a_failed_submission_still_finalizes_and_pays_out(self):
		with patch.object(
			airtable, "create_record", side_effect=AirtableRequestFailed("422: nope")
		):
			response = self._decide()

		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.FINALIZED)
		self.author.hackclub_profile.refresh_from_db()
		self.assertEqual(self.author.hackclub_profile.layers, 32)
		self.assertTrue(
			any("Airtable record was not created" in text for text in message_texts(response))
		)

	def test_unconfigured_airtable_does_not_break_finalization(self):
		with override_settings(AIRTABLE_PAT="", AIRTABLE_BASE_ID="", AIRTABLE_TABLE_ID=""):
			response = self._decide()

		self.ship.refresh_from_db()
		self.assertEqual(self.ship.status, Ship.ShipStatus.FINALIZED)
		self.assertEqual(
			AirtableSubmission.objects.get(ship=self.ship).status,
			AirtableSubmission.Status.FAILED,
		)
		self.assertTrue(
			any("AIRTABLE_PAT" in text for text in message_texts(response))
		)

	def test_audit_log_records_the_outcome(self):
		with patch.object(airtable, "create_record", return_value="recABC"):
			self._decide()

		log = AuditLog.objects.get(action="t3_decision")
		self.assertEqual(log.metadata["airtable_record_id"], "recABC")
		self.assertEqual(log.metadata["airtable_status"], "submitted")

	def test_the_token_is_never_put_in_front_of_the_reviewer(self):
		with patch.object(
			airtable, "create_record", side_effect=AirtableRequestFailed("401: nope")
		):
			response = self._decide()
		self.assertNotIn("pat-test", " ".join(message_texts(response)))
		self.assertNotContains(self.client.get(
			reverse("fraud_review_project", args=[self.ship.id])
		), "pat-test")


class FraudReviewJustificationVisibilityTests(BaseTestCase):
	"""A T3 reviewer has to be able to read the whole justification — T2's words,
	the Lapse links, the removed ranges and the reasons — before approving."""

	def setUp(self):
		super().setUp()
		self.reviewer = grant_perms(make_user("t3rev"), "t3_review")
		self.client.force_login(self.reviewer)
		self.project = make_project(make_user("author"), shippable=True)
		self.ship = Ship.objects.create(
			project=self.project, status=Ship.ShipStatus.T3_QUEUE
		)
		self.journal = make_journal(self.project, ship=self.ship, time_spent=0)
		self.session = make_timelapse(self.project, journal=self.journal, minutes=60)
		approve_timelapse(
			self.journal, removals=[(self.session, 0, 600, "modelling something else")]
		)
		T2.objects.create(
			ship=self.ship, reviewer=self.reviewer, decision=T2.Decision.APPROVE,
			deductions=5, feedback="", justification="Checked against the lapses.",
		)

	def test_page_shows_the_complete_justification(self):
		response = self.client.get(reverse("fraud_review_project", args=[self.ship.id]))
		self.assertContains(response, "Checked against the lapses.")
		self.assertContains(response, "modelling something else")
		self.assertContains(response, "0:00-10:00")
		self.assertContains(response, self.session.watch_url)
		self.assertEqual(
			response.context["override_justification"],
			build_override_justification(self.ship),
		)

	def test_page_shows_the_submission_state_once_there_is_one(self):
		submission = AirtableSubmission.objects.create(
			ship=self.ship, status=AirtableSubmission.Status.SUBMITTED,
			record_id="recABC",
		)
		response = self.client.get(reverse("fraud_review_project", args=[self.ship.id]))
		self.assertEqual(response.context["airtable_submission"], submission)
		self.assertContains(response, "recABC")

	def test_no_submission_yet_is_not_an_error(self):
		response = self.client.get(reverse("fraud_review_project", args=[self.ship.id]))
		self.assertIsNone(response.context["airtable_submission"])
		self.assertContains(response, "Not submitted yet")


@override_settings(**AIRTABLE_SETTINGS)
class SubmitAirtableCommandTests(BaseTestCase):
	"""The retry path for submissions that didn't go through on finalization."""

	def setUp(self):
		super().setUp()
		self.author = make_user("author", hca_token={"access_token": "at"})
		self.project = make_project(self.author, shippable=True)
		self.ship = make_ship(self.project, status=Ship.ShipStatus.FINALIZED)
		T3.objects.create(
			ship=self.ship, reviewer=make_user("t3rev"), decision=T3.Decision.APPROVE,
			payout_time=120, airtable_time=120,
		)
		patcher = patch.object(submissions, "fetch_userinfo", return_value=USERINFO)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _run(self, *args):
		out, err = StringIO(), StringIO()
		call_command("submit_airtable", *args, stdout=out, stderr=err)
		return out.getvalue(), err.getvalue()

	def test_submits_a_finalized_ship_that_was_missed(self):
		with patch.object(airtable, "create_record", return_value="recABC"):
			out, _ = self._run()

		self.assertIn("recABC", out)
		self.assertEqual(
			AirtableSubmission.objects.get(ship=self.ship).record_id, "recABC"
		)

	def test_dry_run_sends_nothing(self):
		with patch.object(airtable, "create_record") as create:
			out, _ = self._run("--dry-run")
		create.assert_not_called()
		self.assertIn("would submit", out)
		self.assertEqual(AirtableSubmission.objects.count(), 0)

	def test_running_twice_creates_one_record(self):
		with patch.object(airtable, "create_record", return_value="recABC") as create:
			self._run()
			out, _ = self._run()

		create.assert_called_once()
		self.assertIn("Nothing to submit.", out)

	def test_unresolved_submissions_are_reported_not_retried(self):
		AirtableSubmission.objects.create(
			ship=self.ship, status=AirtableSubmission.Status.SENDING,
			error="timed out",
		)
		with patch.object(airtable, "create_record") as create:
			out, err = self._run()

		create.assert_not_called()
		self.assertIn("Nothing to submit.", out)
		self.assertIn("check Airtable by hand", err)

	def test_missing_credentials_stop_the_command(self):
		with override_settings(AIRTABLE_PAT=""):
			with patch.object(airtable, "create_record") as create:
				_, err = self._run()
		create.assert_not_called()
		self.assertIn("AIRTABLE_PAT", err)

	def test_single_ship_can_be_targeted(self):
		other = make_ship(self.project, status=Ship.ShipStatus.FINALIZED, journal_minutes=())
		with patch.object(airtable, "create_record", return_value="recABC") as create:
			self._run("--ship", str(other.id))

		create.assert_called_once()
		self.assertFalse(AirtableSubmission.objects.filter(ship=self.ship).exists())
		self.assertTrue(AirtableSubmission.objects.filter(ship=other).exists())
