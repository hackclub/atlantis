"""Port of Fallout's `Admin::Reviews::TimeAuditsController` and the shared
review-queue helpers it uses (claim/heartbeat/next).

Lookout-only: every recording is an atlantis_site LookoutSession. The routes
live under `/admin/...` (same as Fallout) so the copied frontend needs no URL
changes.

This version is per-project (not per-ship) and shows up when a project has
journals (devlogs) with timelapses. Time audits happen BEFORE shipping and
continue independently - shipping puts the project in T1 queue but doesn't
affect the time audit queue.
"""

import json
import re
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import user_passes_test
from django.db import transaction
from django.db.models import Q, Exists, OuterRef
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from django_inertia.core import Inertia

from . import inertia
from .models import ProjectFlag, ReviewerNote, TimeAuditReview
from atlantis_site.models import (
    Journal,
    LookoutSession,
    TimelapseRemoval,
    TimelapseReview,
    first_overlap,
    video_to_tracked,
)
from .serializers import (
    serialize_review_detail,
    serialize_review_row,
    serialize_reviewer_notes,
    serialize_ta_journal_entry,
    serialize_ta_project_context,
)

CLAIM_TTL = timedelta(minutes=30)
PAGE_LIMIT = 25
STATUSES = [s[0] for s in TimeAuditReview.Status.choices]


def _can_review(user):
    return bool(user and (user.is_superuser or user.has_perm("atlantis_site.timelapse_review")))


require_timelapse_reviewer = user_passes_test(_can_review, login_url="/admin/login/")


def _release_claims(user, keep_review=None):
    qs = TimeAuditReview.objects.filter(claimed_by=user)
    if keep_review:
        qs = qs.exclude(id=keep_review.id)
    qs.update(claimed_by=None, claim_expires_at=None)


def _claim(review, user):
    """Atomically claim this review; releases the user's other claims."""
    _release_claims(user, keep_review=review)
    now = timezone.now()
    updated = TimeAuditReview.objects.filter(id=review.id).filter(
        Q(claimed_by__isnull=True)
        | Q(claim_expires_at__isnull=True)
        | Q(claim_expires_at__lt=now)
        | Q(claimed_by=user)
    ).update(claimed_by=user, claim_expires_at=now + CLAIM_TTL)
    if updated:
        review.claimed_by_id = user.id
        review.claim_expires_at = now + CLAIM_TTL
    return bool(updated)


def _lazy(callable_):
    return Inertia.lazy(callable_)


def _get_reviews_with_unreviewed_timelapses(status_filter=None):
    """
    Get TimeAuditReviews for projects that have journals with LookoutSessions
    that haven't been reviewed yet.
    
    A journal is considered "unreviewed" if it has at least one LookoutSession
    that hasn't been reviewed in a completed time audit.
    """
    # Subquery: journals that have LookoutSessions
    journals_with_sessions = Journal.objects.filter(
        project=OuterRef("project"),
        timelapses__isnull=False
    ).distinct()

    # Subquery: journals that have been marked as reviewed
    reviewed_journal_ids = TimeAuditReview.objects.filter(
        project=OuterRef("project")
    ).values("reviewed_journal_ids")

    reviews = TimeAuditReview.objects.select_related("project__owner", "reviewer", "claimed_by")

    if status_filter:
        reviews = reviews.filter(status=status_filter)

    # Filter to only reviews where the project has journals with sessions
    # that aren't in reviewed_journal_ids
    review_ids = []
    for review in reviews:
        unreviewed = Journal.objects.filter(
            project=review.project,
            timelapses__isnull=False
        ).exclude(
            id__in=review.reviewed_journal_ids or []
        ).distinct()
        if unreviewed.exists():
            review_ids.append(review.id)

    return TimeAuditReview.objects.filter(id__in=review_ids).select_related(
        "project__owner", "reviewer", "claimed_by"
    )


def _turnaround_stats():
    """Simplified P90 turnaround stat matching the frontend's expectations."""
    reviews = _get_reviews_with_unreviewed_timelapses()
    waits = []
    now = timezone.now()
    for r in reviews:
        oldest = Journal.objects.filter(
            project=r.project,
            timelapses__isnull=False
        ).exclude(
            id__in=r.reviewed_journal_ids or []
        ).order_by("created_at").first()
        if oldest:
            end = r.reviewed_at if r.status != "pending" and r.reviewed_at else now
            waits.append((end - oldest.created_at).total_seconds() / 86400)
    count = len(waits)
    if not waits:
        return {"turnaround": {"ship_days": None, "cycle_days": None, "count": 0,
                               "ship_delta": None, "cycle_delta": None}}
    waits.sort()
    p90 = waits[min(len(waits) - 1, int(len(waits) * 0.9))]
    return {"turnaround": {"ship_days": round(p90, 1), "cycle_days": None,
                           "count": count, "ship_delta": None, "cycle_delta": None}}


@require_GET
@require_timelapse_reviewer
def time_audit_index(request):
    def pending_rows():
        qs = _get_reviews_with_unreviewed_timelapses(status_filter="pending")
        # Sort by oldest unreviewed journal's created_at
        reviews = list(qs)
        reviews.sort(key=lambda r: (
            Journal.objects.filter(
                project=r.project,
                timelapses__isnull=False
            ).exclude(
                id__in=r.reviewed_journal_ids or []
            ).order_by("created_at").values_list("created_at", flat=True).first()
            or r.created_at
        ))
        flagged = set(ProjectFlag.objects.filter(project_id__in=[r.project_id for r in reviews]).values_list("project_id", flat=True))
        return [serialize_review_row(r, flagged) for r in reviews]

    def pagy_props():
        # All reviews (including those with no unreviewed timelapses) for the history table
        qs = TimeAuditReview.objects.select_related("project__owner", "reviewer", "claimed_by").exclude(status="pending").order_by("-created_at")
        page = max(1, int(request.GET.get("page", 1)))
        total = qs.count()
        rows = list(qs[(page - 1) * PAGE_LIMIT : page * PAGE_LIMIT])
        pages = max(1, -(-total // PAGE_LIMIT))
        flagged = set(ProjectFlag.objects.filter(project_id__in=qs.values("project_id")).values_list("project_id", flat=True))
        return {
            "rows": [serialize_review_row(r, flagged) for r in rows],
            "pagy": {
                "count": total,
                "page": page,
                "limit": PAGE_LIMIT,
                "pages": pages,
                "next": page + 1 if page * PAGE_LIMIT < total else None,
                "prev": page - 1 if page > 1 else None,
            },
        }

    props = {
        "start_reviewing_path": reverse("time_audit_next"),
        "ticket_eligible": False,
        "stats_keys": ["turnaround"],
        "sla_days": 3,
        "stats": _turnaround_stats(),
        "pending_reviews": pending_rows(),
        "all_reviews": pagy_props()["rows"],
        "pagy": pagy_props()["pagy"],
    }
    return inertia.render(request, "admin/reviews/time_audits/index", props)


@require_GET
@require_timelapse_reviewer
def time_audit_next(request):
    skip_ids = request.GET.get("skip", "")
    skip = [int(s) for s in skip_ids.split(",") if s.strip().isdigit()]

    qs = _get_reviews_with_unreviewed_timelapses(status_filter="pending").exclude(id__in=skip).order_by("created_at", "id")
    nxt = qs.first()

    if nxt:
        url = reverse("time_audit_show", args=[nxt.id])
        if skip_ids:
            url += "?" + urlencode({"skip": skip_ids})
        return redirect(url)
    return redirect(reverse("time_audit_index"))


@require_GET
@require_timelapse_reviewer
def time_audit_show(request, review_id):
    review = get_object_or_404(
        TimeAuditReview.objects.select_related("project__owner", "reviewer", "claimed_by"),
        id=review_id,
    )
    project = review.project

    # Claim lifecycle, mirroring Fallout: one claim at a time, redirect away if lost.
    claimed = review.claimed_by_user(request.user)
    if review.status == "pending" and not claimed:
        claimed = _claim(review, request.user)
    if not claimed and review.status == "pending" and not request.user.is_superuser:
        return redirect(reverse("time_audit_next"))

    # Get ALL journals for this project that have LookoutSessions (timelapses)
    # Split into unreviewed (new entries) and reviewed (previous entries)
    all_journals_with_sessions = Journal.objects.filter(
        project=project,
        timelapses__isnull=False
    ).distinct().prefetch_related("timelapses").order_by("created_at", "id")

    reviewed_journal_ids = set(review.reviewed_journal_ids or [])
    unreviewed_journals = [j for j in all_journals_with_sessions if j.id not in reviewed_journal_ids]
    reviewed_journals = [j for j in all_journals_with_sessions if j.id in reviewed_journal_ids]

    new_entries = [serialize_ta_journal_entry(j, request) for j in unreviewed_journals]
    previous_entries = [serialize_ta_journal_entry(j, request) for j in reviewed_journals]

    props = {
        "mode": "ship",
        "review": serialize_review_detail(review),
        "project": serialize_ta_project_context(project, request),
        "new_entries": new_entries,
        "previous_entries": previous_entries,
        "reviewer_notes": serialize_reviewer_notes(
            ReviewerNote.objects.filter(project=project).select_related("author")[:100]
        ),
        "reviewer_notes_path": reverse("reviewer_notes", args=[project.id]),
        "project_flagged": ProjectFlag.objects.filter(project_id=project.id).exists(),
        "can": {"update": review.status == "pending"},
        "skip": request.GET.get("skip"),
        "heartbeat_path": reverse("time_audit_heartbeat", args=[review.id]),
        "next_path": reverse("time_audit_next"),
        "index_path": reverse("time_audit_index"),
        "update_path": reverse("time_audit_update", args=[review.id]),
        "update_key": "time_audit_review",
    }
    return inertia.render(request, "admin/reviews/time_audits/show", props)


@require_http_methods(["POST"])
@require_timelapse_reviewer
def time_audit_heartbeat(request, review_id):
    review = get_object_or_404(TimeAuditReview, id=review_id)
    if review.claimed_by_user(request.user):
        review.extend_claim()
        return JsonResponse({"ok": True, "expires_at": review.claim_expires_at.isoformat()})
    return JsonResponse({"error": "claim_lost"}, status=409)


def _stamp_annotation_reviewer(annotations, current_user_id):
    """Mirror Fallout's stamp_annotation_reviewer: record who annotated each
    recording the first time it is saved."""
    if not isinstance(annotations, dict):
        return annotations
    recordings = annotations.get("recordings", {})
    if not isinstance(recordings, dict):
        recordings = {}
    for data in recordings.values():
        if not isinstance(data, dict):
            continue
        if not data.get("reviewer_id"):
            data["reviewer_id"] = current_user_id
    annotations["recordings"] = recordings
    return annotations


def _link_only_feedback(feedback):
    text = (feedback or "").strip()
    if not text:
        return False
    return all(bool(re.fullmatch(r"https?://\S+", t)) for t in text.split())


def _validated_annotations(review, annotations):
    """Validate Fallout's video-time annotations against this project's Lookouts."""
    if not isinstance(annotations, dict):
        raise ValueError("Annotations must be an object.")

    # Get all LookoutSessions for this project's journals
    sessions = {
        str(session.id): session
        for journal in review.project.journals.prefetch_related("timelapses").all()
        for session in journal.timelapses.all()
    }
    recordings = annotations.get("recordings", {})
    if not isinstance(recordings, dict):
        raise ValueError("Recording annotations must be an object.")

    cleaned = {"recordings": {}}
    for recording_id, data in recordings.items():
        session = sessions.get(str(recording_id))
        if session is None or not isinstance(data, dict):
            raise ValueError("Annotations include a Lookout that is not on this project.")

        segments = data.get("segments", [])
        if not isinstance(segments, list):
            raise ValueError("Lookout segments must be a list.")
        ranges = []
        clean_segments = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError("Each Lookout segment must be an object.")
            segment_type = segment.get("type")
            if segment_type not in ("removed", "deflated"):
                raise ValueError("Lookout segments must be removed or deflated.")
            try:
                start = float(segment["start_seconds"])
                end = float(segment["end_seconds"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("Lookout segments need numeric start and end times.")
            if start < 0 or end <= start or end > session.video_seconds:
                raise ValueError("A Lookout segment is outside its video range.")
            if start.is_integer() and end.is_integer():
                start, end = int(start), int(end)
            if not isinstance(segment.get("reason"), str) or not segment["reason"].strip():
                raise ValueError("Every Lookout segment needs a reason.")
            reason = segment["reason"].strip()
            if len(reason) > 1000:
                raise ValueError("Lookout segment reasons cannot exceed 1000 characters.")
            percent = segment.get("deflated_percent", 100 if segment_type == "removed" else None)
            if segment_type == "deflated":
                try:
                    percent = int(percent)
                except (TypeError, ValueError):
                    raise ValueError("Deflation percentages must be whole numbers.")
                if not 0 <= percent <= 100:
                    raise ValueError("Deflation percentages must be between 0 and 100.")
            else:
                percent = 100
            ranges.append((start, end))
            clean_segments.append({
                "recording_id": session.id,
                "start_seconds": start,
                "end_seconds": end,
                "type": segment_type,
                "reason": reason,
                "deflated_percent": percent,
            })
        if first_overlap(ranges):
            raise ValueError("Lookout segments cannot overlap.")

        clean_data = {
            key: data[key]
            for key in ("description", "stretch_multiplier", "reviewer_id")
            if key in data
        }
        clean_data["segments"] = clean_segments
        cleaned["recordings"][str(session.id)] = clean_data
    return cleaned, sessions


def _approved_seconds(sessions, annotations):
    removed = 0
    for recording_id, data in annotations["recordings"].items():
        for segment in data.get("segments", []):
            tracked_range = video_to_tracked(
                segment["end_seconds"] - segment["start_seconds"]
            )
            percent = segment.get("deflated_percent", 100)
            removed += round(tracked_range * percent / 100)
    return max(sum(session.tracked_seconds for session in sessions) - removed, 0)


def _sync_legacy_reviews(review, annotations, reviewer, feedback):
    """Write one Atlantis review/removal set per journal, once only."""
    journals = list(review.project.journals.prefetch_related("timelapses").order_by("created_at", "id"))
    segments_by_session = {
        int(recording_id): data.get("segments", [])
        for recording_id, data in annotations["recordings"].items()
    }
    for journal in journals:
        legacy_review, created = TimelapseReview.objects.get_or_create(
            journal=journal,
            defaults={
                "reviewer": reviewer,
                "internal_notes": (feedback or "Approved in Fallout time audit")[:1000],
            },
        )
        if not created:
            continue
        removals = []
        for session in journal.timelapses.all():
            for segment in segments_by_session.get(session.id, []):
                removals.append(TimelapseRemoval(
                    review=legacy_review,
                    session=session,
                    start_seconds=video_to_tracked(segment["start_seconds"]),
                    end_seconds=min(
                        video_to_tracked(segment["end_seconds"]), session.tracked_seconds
                    ),
                    deduction_percent=segment.get("deflated_percent", 100),
                    reason=segment["reason"],
                ))
        TimelapseRemoval.objects.bulk_create(removals)


@require_http_methods(["PATCH", "PUT"])
@require_timelapse_reviewer
def time_audit_update(request, review_id):
    review = get_object_or_404(TimeAuditReview, id=review_id)

    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        data = {}
    payload = data.get("time_audit_review", data) or {}

    annotations = payload.get("annotations")
    if annotations is not None:
        try:
            annotations, sessions = _validated_annotations(review, annotations)
        except ValueError as exc:
            return JsonResponse({"errors": {"annotations": [str(exc)]}}, status=422)
        annotations = _stamp_annotation_reviewer(annotations, request.user.id)

    feedback = payload.get("feedback", review.feedback)
    if _link_only_feedback(feedback):
        if request.headers.get("X-Inertia"):
            return HttpResponseRedirect(reverse("time_audit_show", args=[review.id]))
        return JsonResponse(
            {"errors": {"feedback": ["Feedback cannot be only a link. Please explain your time audit decision."]}},
            status=422,
        )

    status = payload.get("status", review.status)
    if status not in STATUSES:
        status = review.status

    # Get journals that have timelapses and are being reviewed in this update
    # (journals with sessions that haven't been marked as reviewed yet)
    all_journals_with_sessions = Journal.objects.filter(
        project=review.project,
        timelapses__isnull=False
    ).distinct()
    reviewed_journal_ids = set(review.reviewed_journal_ids or [])
    unreviewed_journals = [j for j in all_journals_with_sessions if j.id not in reviewed_journal_ids]
    journal_ids = [j.id for j in unreviewed_journals]

    if annotations is not None:
        review.annotations = annotations
    if "feedback" in payload:
        review.feedback = feedback
    if annotations is not None:
        review.approved_public_seconds = _approved_seconds(
            sessions.values(), annotations
        )
    review.status = status
    if status in ("approved", "returned", "rejected", "cancelled"):
        review.reviewed_at = timezone.now()
        if not review.reviewer_id:
            review.reviewer = request.user
        # Mark journals as reviewed when the review is completed
        if journal_ids:
            review.mark_journals_reviewed(journal_ids)

    review.claimed_by = request.user
    review.claim_expires_at = timezone.now() + CLAIM_TTL
    with transaction.atomic():
        locked_review = TimeAuditReview.objects.select_for_update().get(id=review.id)
        if status == "approved" and locked_review.status == "approved":
            pass
        else:
            review.save()
            if status == "approved":
                _sync_legacy_reviews(review, annotations or {"recordings": {}}, request.user, feedback)

    if request.headers.get("X-Inertia"):
        if status == "pending":
            url = reverse("time_audit_show", args=[review.id])
            if request.GET.get("skip"):
                url += "?" + urlencode({"skip": request.GET["skip"]})
            return HttpResponseRedirect(url)
        return redirect(reverse("time_audit_next"))

    return JsonResponse({"ok": True})


@require_POST
@require_timelapse_reviewer
def reviewer_notes_create(request, project_id):
    from atlantis_site.models import Project

    project = get_object_or_404(Project, id=project_id)
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    note_data = body.get("reviewer_note", body) or {}
    note = ReviewerNote.objects.create(
        project=project,
        author=request.user,
        body=(note_data.get("body") or "").strip(),
        ship_id=note_data.get("ship_id"),
        review_stage=note_data.get("review_stage") or "",
    )
    note.refresh_from_db()
    return JsonResponse(serialize_reviewer_notes([note])[0], status=201)


@require_POST
@require_timelapse_reviewer
def project_flags_create(request):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    flag_data = body.get("project_flag", body) or {}
    project_id = flag_data.get("project_id")
    reason = (flag_data.get("reason") or "").strip()
    if not project_id or not reason:
        return JsonResponse({"errors": {"reason": ["A flag reason is required."]}}, status=422)
    from atlantis_site.models import Project, Ship
    project = get_object_or_404(Project, id=project_id)
    ship = Ship.objects.filter(id=flag_data.get("ship_id"), project=project).first()
    flag = ProjectFlag.objects.create(
        project=project,
        ship=ship,
        reviewer=request.user,
        review_stage=(flag_data.get("review_stage") or "")[:64],
        reason=reason,
    )
    return JsonResponse({"id": flag.id, "project_id": flag.project_id, "reason": flag.reason}, status=201)


@require_http_methods(["PATCH", "PUT", "DELETE"])
@require_timelapse_reviewer
def reviewer_notes_update(request, project_id, note_id):
    note = get_object_or_404(ReviewerNote, id=note_id, project_id=project_id)
    if request.method == "DELETE":
        note.delete()
        return JsonResponse({"ok": True})
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        body = {}
    note_data = body.get("reviewer_note", body) or {}
    if "body" in note_data:
        note.body = (note_data.get("body") or "").strip()
        note.save(update_fields=["body", "updated_at"])
    note.refresh_from_db()
    return JsonResponse(serialize_reviewer_notes([note])[0])


@require_GET
def admin_project_redirect(request, project_id):
    from atlantis_site.models import Project

    project = get_object_or_404(Project, id=project_id)
    return HttpResponseRedirect(reverse("project_detail", args=[project.id]))


@require_GET
def admin_user_redirect(request, user_id):
    return HttpResponseRedirect(reverse("user_profile", args=[user_id]))