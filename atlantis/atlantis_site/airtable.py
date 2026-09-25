"""Airtable REST client for the YSWS Project Submission table.

Server-side only. The personal access token lives in settings (read from the
environment) and is only ever put in an Authorization header from here — it is
never handed to a template, a context, or a redirect, and it never appears in
the error strings this module raises, so a failure is safe to show a reviewer.

The three failure modes are separate exception types because they are not
equally safe to retry: see AirtableUnknownOutcome.
"""

import logging
import threading
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 15

REQUIRED_SETTINGS = ("AIRTABLE_PAT", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_ID")
EMAILS_REQUIRED_SETTINGS = ("AIRTABLE_PAT", "AIRTABLE_BASE_ID", "AIRTABLE_EMAILS_TABLE_ID")

# Airtable takes at most ten records per write and five requests a second per
# base; a 429 locks the base out for thirty seconds.
_BATCH_SIZE = 10
_REQUEST_SPACING = 0.25
_RATE_LIMIT_WAIT = 30
_RATE_LIMIT_RETRIES = 3


class AirtableError(Exception):
	"""Base class for every way an Airtable call can go wrong."""


class AirtableNotConfigured(AirtableError):
	"""Credentials are missing, so nothing was sent."""


class AirtableRequestFailed(AirtableError):
	"""Airtable answered with an error status. A write that comes back 4xx/5xx
	created nothing, so the caller may safely try again."""


class AirtableUnknownOutcome(AirtableError):
	"""The request went out and we never got a usable answer.

	A timeout, a dropped connection, or a success we couldn't read all leave the
	same question open: did the record land? Retrying is how you end up with two
	of them, so callers must not — a human checks the table instead.
	"""


def missing_settings(required=REQUIRED_SETTINGS):
	"""Which of the credentials Airtable needs aren't set."""
	return [name for name in required if not getattr(settings, name, "")]


def is_configured():
	return not missing_settings()


def _headers():
	return {
		"Content-Type": "application/json",
		"Authorization": f"Bearer {settings.AIRTABLE_PAT}",
	}


def records_url(table_id=None):
	base = settings.AIRTABLE_API_BASE_URL.rstrip("/")
	return f"{base}/{settings.AIRTABLE_BASE_ID}/{table_id or settings.AIRTABLE_TABLE_ID}"


def _error_type(response):
	"""Airtable's machine-readable error type, e.g. INVALID_VALUE_FOR_COLUMN.

	Deliberately just the type. Airtable's `message` echoes the value it
	rejected, and on this table that is personal data we do not keep.
	"""
	try:
		error = response.json().get("error")
	except ValueError:
		return "unreadable error body"
	if isinstance(error, str):
		return error
	if isinstance(error, dict):
		return error.get("type") or "unknown error type"
	return "no error type given"


def create_record(fields):
	"""Create one record and return its Airtable id.

	`typecast` is on so Airtable coerces our strings into its own column types
	(a date string into the Birthday date, a number into an hours count) rather
	than refusing the whole write over a formatting mismatch.
	"""
	missing = missing_settings()
	if missing:
		raise AirtableNotConfigured(
			f"Airtable is not configured (missing {', '.join(missing)})"
		)

	url = records_url()
	try:
		response = requests.post(
			url,
			headers=_headers(),
			json={"fields": fields, "typecast": True},
			timeout=_TIMEOUT,
		)
	except requests.RequestException as exc:
		logger.error("Airtable create_record transport failure: %s", exc)
		raise AirtableUnknownOutcome(
			f"Airtable did not answer, so it is unknown whether the record was "
			f"created: {exc}"
		) from exc

	if not response.ok:
		# The body is logged but never put in the exception: Airtable quotes the
		# offending value back on a validation error, which here means somebody's
		# address or birthday, and that text is stored on the submission row for
		# a reviewer to read.
		logger.error(
			"Airtable create_record -> %s: %s", response.status_code, response.text[:500]
		)
		raise AirtableRequestFailed(
			f"Airtable returned {response.status_code} ({_error_type(response)}). "
			f"The full response is in the server log."
		)

	try:
		record_id = response.json().get("id")
	except ValueError as exc:
		raise AirtableUnknownOutcome(
			"Airtable accepted the record but its response could not be read"
		) from exc

	if not record_id:
		raise AirtableUnknownOutcome(
			"Airtable accepted the record but returned no record id"
		)
	return record_id


def emails_configured():
	return not missing_settings(EMAILS_REQUIRED_SETTINGS)


def upsert_emails(people):
	"""Write (full name, email) pairs to the Emails table; return how many went.

	An upsert keyed on Email, so running it twice — a backfill clicked again, or
	a signup the backfill already covered — updates the existing row instead of
	adding a second one. That also makes every failure here safe to retry,
	unlike create_record. Only the name and the email are ever sent.
	"""
	missing = missing_settings(EMAILS_REQUIRED_SETTINGS)
	if missing:
		raise AirtableNotConfigured(
			f"Airtable is not configured (missing {', '.join(missing)})"
		)

	url = records_url(settings.AIRTABLE_EMAILS_TABLE_ID)
	records = [{"fields": {"Name": name, "Email": email}} for name, email in people]
	sent = 0
	for start in range(0, len(records), _BATCH_SIZE):
		if start:
			time.sleep(_REQUEST_SPACING)
		batch = records[start:start + _BATCH_SIZE]
		_patch_upsert(url, batch)
		sent += len(batch)
	return sent


def _patch_upsert(url, records):
	body = {
		"performUpsert": {"fieldsToMergeOn": ["Email"]},
		"records": records,
		"typecast": True,
	}
	for attempt in range(_RATE_LIMIT_RETRIES + 1):
		try:
			response = requests.patch(url, headers=_headers(), json=body, timeout=_TIMEOUT)
		except requests.RequestException as exc:
			logger.error("Airtable upsert_emails transport failure: %s", exc)
			raise AirtableUnknownOutcome(f"Airtable did not answer: {exc}") from exc

		if response.status_code == 429 and attempt < _RATE_LIMIT_RETRIES:
			time.sleep(_RATE_LIMIT_WAIT)
			continue
		break

	if not response.ok:
		# Same reasoning as create_record: the body can quote an email back.
		logger.error(
			"Airtable upsert_emails -> %s: %s", response.status_code, response.text[:500]
		)
		raise AirtableRequestFailed(
			f"Airtable returned {response.status_code} ({_error_type(response)}). "
			f"The full response is in the server log."
		)


# What auth_callback stores when HCA hands back no email. Not a real inbox.
PLACEHOLDER_EMAIL = "hackclubber@example.com"


def email_contact(user, fallback_name=""):
	"""The (full name, email) pair the Emails table gets for a user, or None
	when there is no real address to send."""
	email = (user.email or "").strip()
	if not email or email.lower() == PLACEHOLDER_EMAIL:
		return None
	return (user.get_full_name() or fallback_name).strip(), email


def upsert_emails_in_background(people, label):
	"""upsert_emails off the request thread.

	A backfill is hundreds of rate-limited requests, longer than gunicorn will
	hold a worker, and a signup shouldn't wait on Airtable to reach its
	dashboard. The outcome goes to the log.
	"""
	def run():
		try:
			sent = upsert_emails(people)
		except AirtableError as exc:
			logger.error("Emails %s failed: %s", label, exc)
		except Exception:
			logger.exception("Emails %s crashed", label)
		else:
			logger.info("Emails %s sent %s record(s) to Airtable", label, sent)

	_start(run)


def _start(target):
	threading.Thread(target=target, daemon=True).start()
