import logging

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import (
	LddbCursor, Profile, Timelapse, TimelapseAnnotation, TimelapseRemoval, TimelapseReview,
	TimelapseReviewEdit, format_timecode,
	TRACKED_SECONDS_PER_VIDEO_SECOND,
)

logger = logging.getLogger(__name__)

PROGRAM = "atlantis"
_TIMEOUT = 15
_ATTEMPTS = 3
CHANGES_PAGE = 500

MARK_TEXT_MAX = 500
NOTE_MAX = 1000
MAX_IDLE_RANGES = 2000

REASON_KINDS = {
	"AFK / idle screen": "idle",
	"Non-project activity": "invalid",
	"Duplicate session": "duplicate",
	"Unrelated browsing": "invalid",
	"Other": "other",
}

SYNC_USERNAME = "lddb-sync"


class LddbError(Exception):
	pass


class LddbStale(LddbError):
	def __init__(self, current):
		super().__init__("The deflation changed after it was read.")
		self.current = current


class LddbRejected(LddbError):
	pass


def enabled():
	return bool(settings.LDDB_API_KEY)


def _request(method, path, *, json=None, params=None):
	url = f"{settings.LDDB_API_BASE_URL.rstrip('/')}{path}"
	try:
		response = requests.request(
			method,
			url,
			headers={
				"Authorization": f"Bearer {settings.LDDB_API_KEY}",
				"Accept": "application/json",
			},
			json=json,
			params=params,
			timeout=_TIMEOUT,
		)
	except requests.RequestException as exc:
		raise LddbError(f"LDDB {method} {path} failed: {exc}") from exc

	if response.status_code == 404 and method == "GET":
		return None
	try:
		body = response.json()
	except ValueError:
		body = None
	if response.status_code == 409 and isinstance(body, dict):
		raise LddbStale(body.get("current"))
	if not response.ok:
		message = (body or {}).get("error", {}).get("message") if isinstance(body, dict) else None
		error = LddbRejected if response.status_code in (400, 404, 422) else LddbError
		raise error(
			f"LDDB {method} {path} returned {response.status_code}: "
			f"{message or response.text[:300]}"
		)
	return body


def tracked_to_ms(tracked_seconds):
	return round(tracked_seconds * 1000 / TRACKED_SECONDS_PER_VIDEO_SECOND)


def ms_to_tracked(video_ms):
	return round(video_ms * TRACKED_SECONDS_PER_VIDEO_SECOND / 1000)


def _mark_key(mark):
	if not mark:
		return None
	return (
		mark.get("start_ms"), mark.get("end_ms"), mark.get("reason"),
		round(float(mark.get("fraction", 1)), 6), mark.get("text") or "",
	)


def _mark_input(mark):
	sent = {
		key: mark[key]
		for key in ("id", "start_ms", "end_ms", "reason", "fraction", "text")
		if key in mark
	}
	if mark.get("data") is not None:
		sent["data"] = mark["data"]
	return sent


def mark_for(removal):
	if removal.lddb_mark and removal.lddb_mark_id:
		return _mark_input(removal.lddb_mark)
	mark = {
		"start_ms": tracked_to_ms(removal.start_seconds),
		"end_ms": tracked_to_ms(removal.end_seconds),
		"reason": REASON_KINDS.get(removal.reason.strip(), "other"),
		"fraction": 1,
		"text": removal.reason[:MARK_TEXT_MAX].strip(),
	}
	if removal.lddb_mark_id:
		mark["id"] = str(removal.lddb_mark_id)
	if len(removal.reason) > MARK_TEXT_MAX:
		mark["data"] = {"atlantis": {"reason": removal.reason}}
	return mark


def _idle_for(timelapse):
	ranges = [
		[int(segment.get("start") or 0) * 1000, int(segment.get("end") or 0) * 1000]
		for segment in (timelapse.inactive_segments or [])
	]
	ranges = [r for r in ranges if r[1] > r[0]][:MAX_IDLE_RANGES]
	return {"applied": False, "ranges": ranges}


def _reason_for(mark, labels):
	data = mark.get("data") or {}
	text = (mark.get("text") or "").strip()
	whole = ((data.get("atlantis") or {}).get("reason") or "") if isinstance(data, dict) else ""
	if whole and whole[:MARK_TEXT_MAX].strip() == text:
		text = whole
	kind = mark.get("reason") or "other"
	reason = text or labels.get(kind) or kind
	fraction = float(mark.get("fraction", 1))
	if fraction < 1:
		start = mark["start_ms"] // 1000
		end = -(-mark["end_ms"] // 1000)
		reason = (
			f"{reason} ({round(fraction * 100)}% of "
			f"{format_timecode(start)}-{format_timecode(end)} taken off in LDDB)"
		)
	return reason[:1000]


def _removal_from(mark, timelapse, labels):
	fraction = float(mark.get("fraction", 1))
	if fraction <= 0:
		return None
	start = ms_to_tracked(mark["start_ms"])
	full_end = ms_to_tracked(mark["end_ms"])
	end = min(start + round((full_end - start) * min(fraction, 1)), timelapse.tracked_seconds or 0)
	if end <= start:
		return None
	return TimelapseRemoval(
		session=timelapse,
		start_seconds=start,
		end_seconds=end,
		reason=_reason_for(mark, labels),
		counts_toward_makeup=True,
		lddb_mark_id=mark["id"],
		lddb_mark=mark,
	)


def mark_pending(timelapses):
	Timelapse.objects.filter(
		id__in=[t.id for t in timelapses], source=Timelapse.Source.LAPSE,
	).update(lddb_pending=True)


def push_soon(timelapse_ids):
	if not enabled() or not timelapse_ids:
		return
	from .views.helpers import run_in_background
	run_in_background(push_many, list(timelapse_ids))


def push_many(timelapse_ids):
	pushed = 0
	for timelapse_id in timelapse_ids:
		try:
			if push(timelapse_id):
				pushed += 1
		except Exception:
			logger.exception("LDDB push of timelapse %s failed", timelapse_id)
	return pushed


def _note_for(review):
	from .views.helpers import display_name
	edit = review.edits.select_related("editor", "editor__hackclub_profile").first()
	if edit is not None and "lddb" not in (edit.changes or {}):
		note = f"Corrected in Atlantis's Review Audit by {display_name(edit.editor)}: {edit.note}"
	else:
		note = f"Atlantis timelapse review by {display_name(review.reviewer)}"
	return note[:NOTE_MAX]


def push(timelapse_id):
	if not enabled():
		return False
	with transaction.atomic():
		timelapse = (
			Timelapse.objects.select_for_update(skip_locked=True)
			.filter(id=timelapse_id, lddb_pending=True).first()
		)
		if timelapse is None:
			return False
		review = (
			TimelapseReview.objects.select_related("reviewer", "reviewer__hackclub_profile")
			.filter(journal_id=timelapse.journal_id).first()
		)
		if not timelapse.is_lapse or review is None:
			timelapse.lddb_pending = False
			timelapse.save(update_fields=["lddb_pending"])
			return False
		removals = list(review.removals.filter(session=timelapse).order_by("start_seconds", "id"))
		annotation = review.annotations.filter(session=timelapse).first()
		description = annotation.description if annotation else ""
		note = _note_for(review)
		path = f"/deflations/{timelapse.lapse_id}"

		try:
			current = _request("GET", path)
			for attempt in range(_ATTEMPTS):
				try:
					view = _write(timelapse, removals, description, note, current, path)
					break
				except LddbStale as stale:
					if attempt == _ATTEMPTS - 1:
						raise LddbError("The deflation kept changing under the push; will try again.")
					current = stale.current
		except LddbError as exc:
			logger.warning("LDDB push of timelapse %s: %s", timelapse.id, exc)
			timelapse.lddb_error = str(exc)[:500]
			if isinstance(exc, LddbRejected):
				timelapse.lddb_pending = False
			timelapse.save(update_fields=["lddb_error", "lddb_pending"])
			return False

		_link(removals, view)
		timelapse.lddb_pending = False
		timelapse.lddb_error = ""
		notify = _reconcile(timelapse, review, view, change=None)
	_notify(notify)
	return True


def _write(timelapse, removals, description, note, current, path):
	if timelapse.lddb_version is None or current is None:
		body = {
			"marks": [mark_for(r) for r in removals],
			"idle": _idle_for(timelapse),
			"description": description,
			"note": note,
			"program": PROGRAM,
		}
		if current is not None:
			body["version"] = current["version"]
		return _request("PUT", path, json=body)

	current_marks = current.get("marks") or []
	current_ids = {m["id"] for m in current_marks}
	ours = {str(r.lddb_mark_id) for r in removals if r.lddb_mark_id}
	unlinked = [mark_for(r) for r in removals if not r.lddb_mark_id]
	unlinked_ranges = {(m["start_ms"], m["end_ms"]) for m in unlinked}
	there = {
		m["id"] for m in current_marks
		if m["id"] not in ours and (m["start_ms"], m["end_ms"]) in unlinked_ranges
	}
	there_ranges = {(m["start_ms"], m["end_ms"]) for m in current_marks if m["id"] in there}
	add = [m for m in unlinked if (m["start_ms"], m["end_ms"]) not in there_ranges]
	ours |= there
	remove = [
		mark_id for mark_id in (timelapse.lddb_marks or [])
		if mark_id not in ours and mark_id in current_ids
	]
	for mark in current_marks:
		if mark["id"] in ours or mark["id"] in remove:
			continue
		if any(a["start_ms"] < mark["end_ms"] and mark["start_ms"] < a["end_ms"] for a in add):
			remove.append(mark["id"])
	body = {
		"version": current["version"],
		"add": add,
		"remove": remove,
		"description": description,
		"note": note,
		"program": PROGRAM,
	}
	return _request("PATCH", path, json=body)


def _link(removals, view):
	marks = view.get("marks") or []
	by_id = {m["id"]: m for m in marks}
	taken = {str(r.lddb_mark_id) for r in removals if r.lddb_mark_id}
	for removal in removals:
		if removal.lddb_mark_id:
			mark = by_id.get(str(removal.lddb_mark_id))
			if mark is not None and removal.lddb_mark is not None:
				removal.lddb_mark = mark
				removal.save(update_fields=["lddb_mark"])
			continue
		sent = mark_for(removal)
		match = next(
			(
				m for m in marks
				if m["id"] not in taken
				and m["start_ms"] == sent["start_ms"] and m["end_ms"] == sent["end_ms"]
			),
			None,
		)
		if match is None:
			continue
		taken.add(match["id"])
		removal.lddb_mark_id = match["id"]
		removal.lddb_mark = None
		removal.save(update_fields=["lddb_mark_id", "lddb_mark"])


def _sync_user():
	user, _ = get_user_model().objects.get_or_create(
		username=SYNC_USERNAME, defaults={"is_active": False},
	)
	return user


def _editor_for(by):
	slack_id = (by or {}).get("slack_id")
	if slack_id:
		profile = Profile.objects.filter(slack_id=slack_id).select_related("user").first()
		if profile is not None:
			return profile.user
	return _sync_user()


def _labels():
	try:
		kinds = _request("GET", "/deflations/kinds") or []
	except LddbError:
		return {}
	return {kind["id"]: kind.get("label") or kind["id"] for kind in kinds}


def _reconcile(timelapse, review, view, change, labels=None):
	from .views.admin.timelapse_review import _live_makeup, _removal_record

	marks = (view or {}).get("marks") or []
	by_id = {m["id"]: m for m in marks}
	removals = list(review.removals.filter(session=timelapse))
	owner = timelapse.journal.project.owner
	makeup_before = _live_makeup(owner)

	deleted, added, kept_ids = [], [], set()
	for removal in removals:
		if not removal.lddb_mark_id:
			continue
		mark = by_id.get(str(removal.lddb_mark_id))
		if mark is None:
			deleted.append(removal)
			continue
		kept_ids.add(mark["id"])
		previous = removal.lddb_mark or mark_for(removal)
		if _mark_key(previous) != _mark_key(mark):
			deleted.append(removal)
			kept_ids.discard(mark["id"])

	staying = [r for r in removals if r not in deleted]
	for mark in marks:
		if mark["id"] in kept_ids:
			continue
		if labels is None:
			labels = _labels()
		removal = _removal_from(mark, timelapse, labels)
		if removal is None:
			continue
		if any(
			other.start_seconds < removal.end_seconds and removal.start_seconds < other.end_seconds
			for other in staying
		):
			continue
		removal.review = review
		added.append(removal)
		staying.append(removal)

	description_change = None
	description = ((view or {}).get("description") or "").strip()[:500]
	annotation = review.annotations.filter(session=timelapse).first()
	if change is not None and description and description != (annotation.description if annotation else ""):
		description_change = {
			"session_id": timelapse.id,
			"before": annotation.description if annotation else "",
			"after": description,
		}

	removed_before = sum(r.duration_seconds for r in removals)
	if deleted or added or description_change:
		TimelapseRemoval.objects.filter(id__in=[r.id for r in deleted]).delete()
		TimelapseRemoval.objects.bulk_create(added)
		if description_change:
			if annotation is None:
				TimelapseAnnotation.objects.create(
					review=review, session=timelapse, description=description,
				)
			else:
				annotation.description = description
				annotation.save(update_fields=["description"])
		removed_after = (
			removed_before
			- sum(r.duration_seconds for r in deleted)
			+ sum(r.duration_seconds for r in added)
		)
		by = (change or {}).get("by") or {}
		who = by.get("name") or "someone"
		where = (change or {}).get("program") or "LDDB"
		why = (change or {}).get("note") or ""
		note = f"Changed in LDDB by {who} in {where}" + (f": {why}" if why else ".")
		TimelapseReviewEdit.objects.create(
			review=review,
			editor=_editor_for(by),
			note=note[:500],
			changes={
				"descriptions": [description_change] if description_change else [],
				"removals_deleted": [_removal_record(r) for r in deleted],
				"removals_added": [
					{
						"session_id": r.session_id,
						"start_seconds": r.start_seconds,
						"end_seconds": r.end_seconds,
						"reason": r.reason,
					}
					for r in added
				],
				"removed_seconds_before": removed_before,
				"removed_seconds_after": removed_after,
				"lddb": {
					"version": (view or {}).get("version") or (change or {}).get("version"),
					"actor": (change or {}).get("actor"),
					"program": (change or {}).get("program"),
				},
			},
		)

	linked = [r for r in staying if r.lddb_mark_id]
	timelapse.lddb_marks = sorted({str(r.lddb_mark_id) for r in linked})
	if view is not None:
		timelapse.lddb_version = view["version"]
	elif change is not None and change.get("version") is not None:
		timelapse.lddb_version = change["version"]
	timelapse.lddb_synced_at = timezone.now()
	timelapse.save(update_fields=[
		"lddb_marks", "lddb_version", "lddb_synced_at", "lddb_pending", "lddb_error",
	])
	if not added:
		return None
	return (owner, timelapse.journal.project, makeup_before)


def _notify(notify):
	if notify is None:
		return
	from .views.admin.timelapse_review import notify_makeup
	owner, project, before = notify
	notify_makeup(owner, project, before)


def apply_change(external_id, version, labels=None):
	timelapse = Timelapse.objects.filter(
		lapse_id=external_id, source=Timelapse.Source.LAPSE,
	).first()
	if timelapse is None or timelapse.lddb_version is None or timelapse.lddb_pending:
		return False
	if version is not None and version <= timelapse.lddb_version:
		return False

	history = _request("GET", f"/deflations/{external_id}/history") or []
	if not history:
		return False
	change = history[0]
	view = None
	if change.get("kind") != "cleared":
		view = _request("GET", f"/deflations/{external_id}")

	with transaction.atomic():
		timelapse = Timelapse.objects.select_for_update().get(pk=timelapse.pk)
		if timelapse.lddb_pending:
			return False
		latest = (view or {}).get("version") or change.get("version")
		if latest is not None and timelapse.lddb_version is not None and latest <= timelapse.lddb_version:
			return False
		review = TimelapseReview.objects.select_for_update().filter(journal_id=timelapse.journal_id).first()
		if review is None:
			return False
		notify = _reconcile(timelapse, review, view, change, labels)
	_notify(notify)
	return True


def pull(limit_pages=20):
	if not enabled():
		return 0
	cursor, _ = LddbCursor.objects.get_or_create(pk=1)
	applied = 0
	labels = None
	for _ in range(limit_pages):
		feed = _request(
			"GET", "/deflations/changes",
			params={"after": cursor.position, "limit": CHANGES_PAGE},
		) or {}
		items = feed.get("items") or []
		if not items:
			break
		latest = {}
		for item in items:
			external_id = item.get("external_id")
			if external_id:
				latest[external_id] = max(latest.get(external_id) or 0, item.get("version") or 0)
		followed = set(
			Timelapse.objects.filter(
				lapse_id__in=list(latest), source=Timelapse.Source.LAPSE,
				lddb_version__isnull=False,
			).values_list("lapse_id", flat=True)
		)
		for external_id in followed:
			if labels is None:
				labels = _labels()
			if apply_change(external_id, latest[external_id], labels):
				applied += 1
		cursor.position = feed.get("next") or cursor.position
		cursor.save(update_fields=["position", "updated_at"])
		if len(items) < CHANGES_PAGE:
			break
	return applied


def queue_backfill():
	return Timelapse.objects.filter(
		source=Timelapse.Source.LAPSE,
		lddb_version__isnull=True,
		lddb_pending=False,
		journal__timelapse_review__isnull=False,
	).update(lddb_pending=True)
