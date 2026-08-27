"""Submitting a finalized ship to the YSWS Project Submission table.

This is the last step of the pipeline: once a T3 reviewer approves a ship, the
project it belongs to becomes one record in Hack Club HQ's Airtable, and that
record is what pays the shipper. It happens once per ship and never before
finalization.

Two things about it are worth knowing before changing anything here.

The override-hours justification is the whole audit trail. HQ reads Airtable's
"Automation - Unified Justification", which is defined as *our* override
justification whenever we set one — so if the internal timelapse review cut time
out of somebody's hours, the only place that ever becomes visible outside
Atlantis is this field. The T2 reviewer's own words open it, verbatim; the
timelapse reviewer's justification, the Lookout links, the removed ranges,
and the reason each range was removed are appended below them.

None of the shipper's personal data is stored. Their address and birthday come
from HCA at submission time, go straight into the request, and are not written
to our database — the same rule the rest of the app follows.
"""

import logging

from django.conf import settings
from django.core.files.storage import default_storage
from django.db.models import Q
from django.utils import timezone

from . import airtable
from .airtable import AirtableError, AirtableUnknownOutcome
from .hca import IdentityUnavailable, extract_addresses, extract_birthdate, fetch_userinfo, select_address
from .models import AirtableSubmission, Ship, T3

logger = logging.getLogger(__name__)

# The column names in the YSWS Project Submission table, keyed by what this app
# calls them. Airtable writes fail on an unknown field name, so a rename over
# there is a one-line fix here.
FIELDS = {
	"code": "Code URL",
	"demo": "Playable URL",
	"first_name": "First Name",
	"last_name": "Last Name",
	"email": "Email",
	"screenshot": "Screenshot",
	"description": "Description",
	"address_line_1": "Address (Line 1)",
	"address_line_2": "Address (Line 2)",
	"city": "City",
	"state": "State / Province",
	"country": "Country",
	"zip": "ZIP / Postal Code",
	"birthday": "Birthday",
	"override_hours": "Optional - Override Hours Spent",
	"override_justification": "Optional - Override Hours Spent Justification",
}

LOOKOUT_HEADING = "[LOOKOUT TIMELAPSE AUDIT]"


class NotFinalized(Exception):
	"""A ship was handed to the submitter before a T3 reviewer approved it."""


def _hours(minutes):
	"""Minutes as hours to one decimal place — the precision of the Airtable column."""
	return round((minutes or 0) / 60, 1)


def _display_hours(minutes):
	minutes = minutes or 0
	return f"{minutes // 60}h {minutes % 60}m"


def latest_t2(ship):
	return ship.t2_reviews.order_by("-id").first()


def approving_t3(ship):
	"""The T3 review that finalized the ship, which is where the hours to submit
	come from. Falls back to the most recent T3 so a ship finalized some other
	way still submits something rather than nothing."""
	return (
		ship.t3_reviews.filter(decision=T3.Decision.APPROVE).order_by("-id").first()
		or ship.t3_reviews.order_by("-id").first()
	)


def download_url(value):
	"""A URL Airtable's own fetcher can reach for one of our stored files.

	Uploads are object keys in a private R2 bucket with no public hostname, so
	the link has to be presigned. Anything already absolute — an Onshape share
	link, an externally hosted image — is passed through untouched.
	"""
	if not value:
		return ""
	if value.startswith(("http://", "https://")):
		return value
	try:
		return default_storage.url(value, expire=settings.AIRTABLE_URL_EXPIRE_SECONDS)
	except TypeError:
		# Backends other than S3 (the in-memory one the tests use) take no expiry.
		return default_storage.url(value)


def build_lookout_audit(ship):
	"""The internal timelapse review of this ship, written out for HQ.

	One block per journal: the reviewer's own justification for the journal's
	decision, then every Lookout attached to it, and under each Lookout the
	ranges a timelapse reviewer refused to pay for, with the reason they gave.
	Lookouts with nothing removed are listed too — the links are part of the
	evidence for the hours whether or not anything was cut from them.
	"""
	journals = list(
		ship.journals.order_by("created_at").select_related("timelapse_review").prefetch_related(
			"timelapses", "timelapses__removals"
		)
	)

	tracked = 0
	removed = 0
	blocks = []
	for journal in journals:
		sessions = list(journal.timelapses.all())
		lines = [f'"{journal.title}" — {journal.tracked_display} tracked']
		review = journal.timelapse_review_or_none
		if review and review.internal_notes:
			lines.append(f"  reviewer's justification: {review.internal_notes}")
		if not sessions:
			lines.append("  no Lookout attached")
		for session in sessions:
			tracked += session.tracked_seconds or 0
			lines.append(f"  {session.video_url} ({session.tracked_display} tracked)")
			removals = sorted(session.removals.all(), key=lambda r: r.start_seconds)
			if not removals:
				lines.append("    nothing removed")
			for removal in removals:
				removed += removal.duration_seconds
				# Both timelines: the video range is where to scrub to on the
				# link above, the tracked range is what it cost.
				lines.append(
					f"    removed {removal.video_range_display} on the video "
					f"= {removal.range_display} tracked "
					f"({removal.duration_display}): {removal.reason}"
				)
		blocks.append("\n".join(lines))

	t2 = latest_t2(ship)
	deductions = t2.deductions if t2 else 0
	t3 = approving_t3(ship)

	summary = [
		f"Tracked by Lookout: {_display_hours(tracked // 60)}",
		f"Removed in internal timelapse review: {_display_hours(removed // 60)}",
		f"Deducted by T2 review: {_display_hours(deductions)}",
	]
	if t3:
		summary.append(f"Submitted hours: {_hours(t3.airtable_time)}")

	return "\n".join([LOOKOUT_HEADING, *summary, "", *blocks]).strip()


def build_override_justification(ship):
	"""The full "Optional - Override Hours Spent Justification" for a ship.

	The T2 reviewer's justification comes first and is copied character for
	character: it is their account of the hours, HQ reads this field as the
	unified justification, and nothing here may edit or replace it. The Lookout
	audit is appended below it.

	The latest T2 review is the one quoted — it is the decision that stands, and
	the same one whose deductions the T3 page shows. Superseded T2 passes stay
	where they already are, in the review history.
	"""
	t2 = latest_t2(ship)
	original = (t2.justification if t2 else "").strip()
	audit = build_lookout_audit(ship)
	return "\n\n".join(part for part in (original, audit) if part)


def build_fields(ship, notes=None):
	"""The Airtable payload for a finalized ship.

	Anything HCA can't tell us (no token on file, identity service down) is left
	out and explained in `notes` rather than blocking the submission: a record
	that exists with a gap in it can be chased, one that was never created
	can't.
	"""
	notes = notes if notes is not None else []
	project = ship.project
	owner = project.owner
	profile = getattr(owner, "hackclub_profile", None)

	# One userinfo call for both claims — the address and the birthday come out
	# of the same response.
	userinfo = {}
	if profile is None:
		notes.append("No Hack Club profile on file: no address or birthday submitted.")
	else:
		try:
			userinfo = fetch_userinfo(profile)
		except IdentityUnavailable as exc:
			notes.append(f"Hack Club Auth unavailable ({exc}): no address or birthday submitted.")

	address = select_address(extract_addresses(userinfo)) or {}
	if userinfo and not address:
		notes.append("Hack Club Auth returned no address for this user.")

	birthday = extract_birthdate(userinfo)
	if userinfo and not birthday:
		# Every token issued before `birthdate` joined HCA_SCOPE lacks the claim,
		# so this clears itself the next time the shipper logs in.
		notes.append(
			"No birthdate from Hack Club Auth — the shipper may need to log in "
			"again to grant the birthdate scope."
		)

	first_name = address.get("first_name") or owner.first_name
	last_name = address.get("last_name") or owner.last_name
	screenshot = download_url(project.image_url)
	t3 = approving_t3(ship)

	fields = {
		FIELDS["code"]: project.printablesUrl,
		FIELDS["demo"]: download_url(project.editor_model_url),
		FIELDS["first_name"]: first_name,
		FIELDS["last_name"]: last_name,
		FIELDS["email"]: owner.email,
		FIELDS["screenshot"]: [{"url": screenshot}] if screenshot else [],
		FIELDS["description"]: project.description,
		FIELDS["address_line_1"]: address.get("line_1") or address.get("street_address") or "",
		FIELDS["address_line_2"]: address.get("line_2") or "",
		FIELDS["city"]: address.get("city") or address.get("locality") or "",
		FIELDS["state"]: address.get("state") or address.get("region") or "",
		FIELDS["country"]: address.get("country") or "",
		FIELDS["zip"]: address.get("postal_code") or "",
		FIELDS["birthday"]: birthday,
		FIELDS["override_hours"]: _hours(t3.airtable_time if t3 else 0),
		FIELDS["override_justification"]: build_override_justification(ship),
	}

	# Empty values are dropped rather than sent as "": Airtable's date and
	# attachment columns reject an empty string, and a field we have nothing for
	# is better left untouched than written blank.
	return {
		name: value for name, value in fields.items()
		if value or isinstance(value, (int, float))
	}


def _record_failure(submission, exc, notes):
	"""Mark a submission that definitely created nothing, so it can be retried."""
	submission.status = AirtableSubmission.Status.FAILED
	submission.error = str(exc)
	submission.notes = "\n".join(notes)
	submission.save(
		update_fields=["status", "attempts", "error", "notes", "updated_at"]
	)
	return submission


def submit_ship(ship):
	"""Create the Airtable record for a finalized ship, at most once.

	Safe to call again after a failure and safe to call twice by accident. The
	AirtableSubmission row is the lock: a single conditional UPDATE moves it into
	`sending`, and only the caller that wins that UPDATE sends anything. A ship
	that already has a record_id is never submitted again, which is what stops a
	retried finalization from putting a second copy of the project in front of
	HQ.

	Always returns the submission row — read its status rather than expecting an
	exception. The one exception raised is NotFinalized, which is a programming
	error at the call site.
	"""
	if ship.status != Ship.ShipStatus.FINALIZED:
		raise NotFinalized(
			f"Ship {ship.id} is {ship.get_status_display().lower()}, not finalized"
		)

	Status = AirtableSubmission.Status
	submission, _ = AirtableSubmission.objects.get_or_create(ship=ship)

	# The claim. One UPDATE, so exactly one of two concurrent finalizations of
	# the same ship gets to send the record; the loser reads back what the
	# winner did.
	claimed = AirtableSubmission.objects.filter(
		pk=submission.pk, record_id="", status__in=(Status.PENDING, Status.FAILED)
	).update(status=Status.SENDING, error="", notes="")
	submission.refresh_from_db()
	if not claimed:
		return submission

	submission.attempts += 1
	notes = []
	try:
		record_id = airtable.create_record(build_fields(ship, notes))
	except AirtableUnknownOutcome as exc:
		# Deliberately left in `sending`: we don't know whether the record
		# landed, and retrying is exactly how a duplicate gets made. needs_retry
		# is False for this state, so a person has to look in Airtable.
		logger.error("Airtable submission for ship %s is unresolved: %s", ship.id, exc)
		submission.error = str(exc)
		submission.notes = "\n".join(notes)
		submission.save(update_fields=["attempts", "error", "notes", "updated_at"])
		return submission
	except AirtableError as exc:
		# Missing credentials, or a 4xx/5xx that created nothing. Expected
		# enough not to want a traceback; retryable either way.
		logger.error("Airtable submission for ship %s failed: %s", ship.id, exc)
		return _record_failure(submission, exc, notes)
	except Exception as exc:
		# A bug on our side — the payload couldn't even be built. Nothing was
		# sent, so it stays retryable, but it wants the traceback.
		logger.exception("Airtable submission for ship %s could not be built", ship.id)
		return _record_failure(submission, exc, notes)

	submission.status = Status.SUBMITTED
	submission.record_id = record_id
	submission.error = ""
	submission.notes = "\n".join(notes)
	submission.submitted_at = timezone.now()
	submission.save(update_fields=[
		"status", "record_id", "attempts", "error", "notes", "submitted_at",
		"updated_at",
	])
	return submission


def pending_ships():
	"""Finalized ships that still owe HQ a record — never submitted, or a
	previous attempt failed in a way that is safe to repeat.

	Mirrors AirtableSubmission.needs_retry, so a submission stuck in `sending`
	is left out: nobody knows whether that one landed.
	"""
	Status = AirtableSubmission.Status
	return (
		Ship.objects.filter(status=Ship.ShipStatus.FINALIZED)
		.filter(
			Q(airtable_submission__isnull=True)
			| Q(
				airtable_submission__record_id="",
				airtable_submission__status__in=(Status.PENDING, Status.FAILED),
			)
		)
		.select_related("project", "project__owner", "project__owner__hackclub_profile")
		.order_by("id")
	)
