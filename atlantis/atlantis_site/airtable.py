"""Airtable REST client for the YSWS Project Submission table.

Server-side only. The personal access token lives in settings (read from the
environment) and is only ever put in an Authorization header from here — it is
never handed to a template, a context, or a redirect, and it never appears in
the error strings this module raises, so a failure is safe to show a reviewer.

The three failure modes are separate exception types because they are not
equally safe to retry: see AirtableUnknownOutcome.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 15

REQUIRED_SETTINGS = ("AIRTABLE_PAT", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_ID")


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


def missing_settings():
	"""Which of the credentials Airtable needs aren't set."""
	return [name for name in REQUIRED_SETTINGS if not getattr(settings, name, "")]


def is_configured():
	return not missing_settings()


def _headers():
	return {
		"Content-Type": "application/json",
		"Authorization": f"Bearer {settings.AIRTABLE_PAT}",
	}


def records_url():
	base = settings.AIRTABLE_API_BASE_URL.rstrip("/")
	return f"{base}/{settings.AIRTABLE_BASE_ID}/{settings.AIRTABLE_TABLE_ID}"


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
		# offending value back at you on a validation error, which for this table
		# means somebody's address or birthday. The exception text is stored on
		# the submission row and shown to a reviewer, so it gets the error type
		# and nothing else.
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
