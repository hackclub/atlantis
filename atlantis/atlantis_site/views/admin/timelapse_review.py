"""Internal timelapse review.

Every journal lands in this queue when it's created and stays there until a
timelapse reviewer signs it off, optionally cutting ranges of unearned time out
of the Lookout footage first. The whole flow is invisible to the project owner:
nothing here notifies them, nothing here renders on a page they can load, and a
project still ships normally while its journals sit in the queue. What waiting
does hold up is the regular (T1) review queue — see timelapse_cleared_ships.
"""

from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.db import transaction

from ...models import (
    Journal, TimelapseRemoval, TimelapseReview, first_overlap, format_timecode,
    parse_timecode,
)
from ..helpers import check_perms, display_name, record_audit, reviewer_leaderboard

# Its own permission, not a tier of the T1/T2/T3 ladder. Organizers keep their
# skeleton key; nobody else gets this by holding another review perm.
TIMELAPSE_REVIEW_PERMS = [
    "atlantis_site.timelapse_review",
    "atlantis_site.organizer",
]

REASON_MAX_LENGTH = 500
INTERNAL_NOTES_MAX_LENGTH = 1000
# Well past anything a real review needs, and it keeps a crafted POST from
# turning one form submission into thousands of rows.
MAX_REMOVALS = 50

RECENT_REVIEWS = 25


class RemovalError(Exception):
    """A posted range we refuse to record. The message is shown to the reviewer."""


def _parse_removals(request, sessions):
    """Build the unsaved TimelapseRemoval rows for a posted review.

    `sessions` maps id -> LookoutSession for the journal being reviewed. The
    rows arrive as four parallel lists, one entry per range the reviewer added.
    """
    session_ids = request.POST.getlist("removal_session")
    starts = request.POST.getlist("removal_start")
    ends = request.POST.getlist("removal_end")
    reasons = request.POST.getlist("removal_reason")

    if not len(session_ids) == len(starts) == len(ends) == len(reasons):
        raise RemovalError("That form didn't come through cleanly. Reload and try again.")

    if len(session_ids) > MAX_REMOVALS:
        raise RemovalError(f"At most {MAX_REMOVALS} removed ranges per journal.")

    removals = []
    for position, row in enumerate(zip(session_ids, starts, ends, reasons), start=1):
        raw_session, raw_start, raw_end, raw_reason = (value.strip() for value in row)

        # An untouched row is an unused input, not a mistake.
        if not raw_start and not raw_end and not raw_reason:
            continue

        try:
            session = sessions[int(raw_session)]
        except (ValueError, KeyError):
            raise RemovalError(f"Range {position} isn't on a Lookout attached to this lapse.")

        start = parse_timecode(raw_start)
        end = parse_timecode(raw_end)
        if start is None or end is None:
            raise RemovalError(
                f"Range {position}: couldn't read that range. Use m:ss or h:mm:ss."
            )
        if end <= start:
            raise RemovalError(f"Range {position} has to end after it starts.")
        # The one guard that keeps an adjusted duration from going negative:
        # you cannot remove time the Lookout never tracked.
        if end > session.tracked_seconds:
            raise RemovalError(
                f"Range {position} runs past the end of that Lookout "
                f"({format_timecode(session.tracked_seconds)} tracked)."
            )
        if not raw_reason:
            raise RemovalError(f"Range {position} needs a justification.")
        if len(raw_reason) > REASON_MAX_LENGTH:
            raise RemovalError(
                f"Range {position}'s justification is too long "
                f"(max {REASON_MAX_LENGTH} characters)."
            )

        removals.append(TimelapseRemoval(
            session=session,
            start_seconds=start,
            end_seconds=end,
            reason=raw_reason,
        ))

    # Overlapping ranges would double-count the same seconds against the
    # shipper, so they're rejected rather than merged — per Lookout, since
    # offsets only mean anything within one session.
    for session_id in {removal.session_id for removal in removals}:
        overlap = first_overlap(
            (removal.start_seconds, removal.end_seconds)
            for removal in removals
            if removal.session_id == session_id
        )
        if overlap:
            start, end = overlap
            raise RemovalError(
                f"{format_timecode(start)}-{format_timecode(end)} overlaps another "
                "removed range on the same Lookout."
            )

    return removals


def _decorate_sessions(journal, review):
    """The journal's Lookouts, oldest first, each carrying its removed ranges."""
    removals = list(review.removals.all()) if review else []
    sessions = list(journal.timelapses.order_by("created_at"))
    for session in sessions:
        session.review_removals = [r for r in removals if r.session_id == session.id]
    return sessions


@staff_member_required
@check_perms(TIMELAPSE_REVIEW_PERMS)
def timelapse_review_dash(request):
    base = (
        Journal.objects
        .filter(project__deleted=False)
        .select_related("project", "project__owner", "project__owner__hackclub_profile")
        .prefetch_related("timelapses")
    )
    # Oldest first: it's a queue, and a journal sitting here is holding its
    # project out of T1.
    pending = base.filter(timelapse_review__isnull=True).order_by("created_at")
    reviewed = (
        base.filter(timelapse_review__isnull=False)
        .select_related("timelapse_review", "timelapse_review__reviewer")
        .prefetch_related("timelapse_review__removals")
        .order_by("-timelapse_review__reviewed_at")[:RECENT_REVIEWS]
    )

    return render(request, "root/timelapse_review.html", {
        "pending": pending,
        "reviewed": reviewed,
        "leaderboard": reviewer_leaderboard("timelapse_reviews"),
    })


@staff_member_required
@check_perms(TIMELAPSE_REVIEW_PERMS)
def timelapse_review_journal(request, journal_id):
    journal = get_object_or_404(
        Journal.objects.select_related(
            "project", "project__owner", "project__owner__hackclub_profile", "ship"
        ),
        id=journal_id,
    )
    review = journal.timelapse_review_or_none

    return render(request, "root/timelapse_review_journal.html", {
        "journal": journal,
        "project": journal.project,
        "review": review,
        "reviewer_name": display_name(review.reviewer) if review else "",
        "sessions": _decorate_sessions(journal, review),
        "reason_max_length": REASON_MAX_LENGTH,
    })


@require_POST
@staff_member_required
@check_perms(TIMELAPSE_REVIEW_PERMS)
def timelapse_decision(request, journal_id):
    journal = get_object_or_404(Journal.objects.select_related("project"), id=journal_id)
    sessions = {session.id: session for session in journal.timelapses.all()}

    internal_notes = request.POST.get("internal_notes", "").strip()
    if len(internal_notes) > INTERNAL_NOTES_MAX_LENGTH:
        messages.error(
            request,
            f"Internal notes too long (max {INTERNAL_NOTES_MAX_LENGTH} characters).",
        )
        return redirect("timelapse_review_journal", journal_id=journal_id)

    try:
        removals = _parse_removals(request, sessions)
    except RemovalError as exc:
        messages.error(request, str(exc))
        return redirect("timelapse_review_journal", journal_id=journal_id)

    with transaction.atomic():
        journal = get_object_or_404(
            Journal.objects.select_for_update().select_related("project"), id=journal_id
        )
        # A review is written once and never edited, so it can be trusted as the
        # audit record. Two reviewers opening the same journal is the ordinary
        # way to get here.
        if TimelapseReview.objects.filter(journal=journal).exists():
            messages.error(request, "That lapse's timelapse has already been reviewed.")
            return redirect("timelapse_review_dash")

        review = TimelapseReview.objects.create(
            journal=journal,
            reviewer=request.user,
            internal_notes=internal_notes,
        )
        for removal in removals:
            removal.review = review
        TimelapseRemoval.objects.bulk_create(removals)

    removed_seconds = sum(removal.duration_seconds for removal in removals)

    # No send_slack_dm and no notify_followers, deliberately: timelapse review
    # is internal, and the shipper is not told that it happened or what it cost
    # them.
    record_audit(
        request,
        "timelapse_review",
        target=f"Journal #{journal.id} ({journal.project.title})",
        metadata={
            "journal_id": journal.id,
            "review_id": review.id,
            "project_id": journal.project_id,
            "project": journal.project.title,
            "removed_seconds": removed_seconds,
            "removals": [
                {
                    "session_id": removal.session_id,
                    "start_seconds": removal.start_seconds,
                    "end_seconds": removal.end_seconds,
                    "reason": removal.reason,
                }
                for removal in removals
            ],
        },
    )

    if removals:
        messages.success(
            request,
            f'Approved "{journal.title}" with {format_timecode(removed_seconds)} removed '
            f"across {len(removals)} range{'' if len(removals) == 1 else 's'}.",
        )
    else:
        messages.success(request, f'Approved "{journal.title}" with no time removed.')

    return redirect("timelapse_review_dash")
