"""Internal Lookout review.

Every journal lands in this queue when it's created and stays there until a
reviewer signs it off, optionally cutting ranges of unearned time out of the
Lookout footage first. The whole flow is invisible to the project owner:
nothing here notifies them, nothing here renders on a page they can load, and a
project still ships normally while its journals sit in the queue. What waiting
does hold up is the regular (T1) review queue — see timelapse_cleared_ships.

The unit of work is a *project*, not a journal. Every lapse on a project is the
same person recording the same build, and judging them one at a time meant
re-learning that context on every visit and paying a page load between each.
One project is one page: all of its unreviewed lapses, all of their Lookouts,
one pass, one decision. The rows written are still one TimelapseReview per
journal — that part of the record is unchanged.
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Exists, OuterRef

from ...models import (
    Journal, Project, TimelapseRemoval, TimelapseReview, first_overlap,
    format_timecode, parse_timecode, video_to_tracked,
)
from ..helpers import (
    check_perms, display_name, format_minutes, record_audit, reviewer_leaderboard,
)
from .queue import (
    QUEUES, dash_context, decorate_lapses, decorate_rows, go_to_next,
    owner_snapshot, parse_skip, review_context,
)

# Its own permission, not a tier of the T1/T2/T3 ladder. Organizers keep their
# skeleton key; nobody else gets this by holding another review perm.
TIMELAPSE_REVIEW_PERMS = [
    "atlantis_site.timelapse_review",
    "atlantis_site.organizer",
]

REASON_MAX_LENGTH = 1000
INTERNAL_NOTES_MAX_LENGTH = 1000
# Well past anything a real pass needs, and it keeps a crafted POST from
# turning one form submission into thousands of rows. Per submission, which is
# now a whole project rather than a single lapse.
MAX_REMOVALS = 200

RECENT_REVIEWS = 25

# How many of a project's lapses the desk lists under it before it stops and
# says how many more there are. The desk is a queue, not the review page.
DESK_LAPSE_PREVIEW = 5


class RemovalError(Exception):
    """A posted range we refuse to record. The message is shown to the reviewer."""


def _unreviewed(lapses):
    """Narrow a Journal queryset to the lapses nobody has signed off yet.

    The obvious spelling is `timelapse_review__isnull=True`, and it compiles to
    a LEFT OUTER JOIN onto the review table. That is fine to read and
    impossible to lock: Postgres rejects `FOR UPDATE` the moment a nullable
    side of an outer join is in the lock set, and the sign-off below re-reads
    these rows under select_for_update. So the absence is asked as a NOT
    EXISTS, which is a subquery rather than a join, and the same queryset
    serves both the page and the locked read.
    """
    return lapses.filter(~Exists(
        TimelapseReview.objects.filter(journal=OuterRef("pk"))
    ))


def _locked_pending_lapses(project):
    """The project's unreviewed lapses, locked for the length of a sign-off."""
    return (
        _unreviewed(Journal.objects.filter(project=project))
        .select_for_update()
        .order_by("created_at", "id")
    )


def _pending_lapses(project):
    """The project's unreviewed lapses, oldest first, footage attached."""
    return list(
        _unreviewed(Journal.objects.filter(project=project))
        .prefetch_related("timelapses")
        .order_by("created_at", "id")
    )


def _parse_removals(request, sessions):
    """Build the unsaved TimelapseRemoval rows for a posted pass.

    `sessions` maps id -> LookoutSession across every lapse being signed off,
    so one form can carry cuts from several lapses at once. The rows arrive as
    four parallel lists, one entry per range the reviewer added.

    Every offset posted here is read off the compiled video, the only timeline
    the reviewer can see, and the video runs sixty times faster than the
    session it was stitched from. So the ranges are validated against the
    video's length and stored as the tracked seconds they stand for: cutting
    0:56-1:11 out of the player takes fifteen minutes off the lapse, not
    fifteen seconds.
    """
    session_ids = request.POST.getlist("removal_session")
    starts = request.POST.getlist("removal_start")
    ends = request.POST.getlist("removal_end")
    reasons = request.POST.getlist("removal_reason")

    if not len(session_ids) == len(starts) == len(ends) == len(reasons):
        raise RemovalError("That form didn't come through cleanly. Reload and try again.")

    if len(session_ids) > MAX_REMOVALS:
        raise RemovalError(f"At most {MAX_REMOVALS} removed ranges per pass.")

    removals = []
    # (session id, start, end) on the video timeline, for the overlap check and
    # the messages about it: the reviewer recognises what they typed.
    video_ranges = []
    for position, row in enumerate(zip(session_ids, starts, ends, reasons), start=1):
        raw_session, raw_start, raw_end, raw_reason = (value.strip() for value in row)

        # An untouched row is an unused input, not a mistake.
        if not raw_start and not raw_end and not raw_reason:
            continue

        try:
            session = sessions[int(raw_session)]
        except (ValueError, KeyError):
            raise RemovalError(
                f"Range {position} isn't on a Lookout attached to this project."
            )

        start = parse_timecode(raw_start)
        end = parse_timecode(raw_end)
        if start is None or end is None:
            raise RemovalError(
                f"Range {position}: couldn't read that range. Use m:ss or h:mm:ss."
            )
        if end <= start:
            raise RemovalError(f"Range {position} has to end after it starts.")
        # The one guard that keeps an adjusted duration from going negative:
        # you cannot remove footage the video doesn't have.
        if end > session.video_seconds:
            raise RemovalError(
                f"Range {position} runs past the end of that Lookout's video "
                f"({session.video_duration_display} long, "
                f"{format_timecode(session.tracked_seconds)} tracked)."
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
            start_seconds=video_to_tracked(start),
            # The last second of video can stand for a part-minute of tracking
            # (a session's tracked time is whole minutes minus its first
            # bucket), so the end is clamped rather than trusted to convert
            # inside the session.
            end_seconds=min(video_to_tracked(end), session.tracked_seconds),
            reason=raw_reason,
        ))
        video_ranges.append((session.id, start, end))

    # Overlapping ranges would double-count the same seconds against the
    # shipper, so they're rejected rather than merged — per Lookout, since
    # offsets only mean anything within one session.
    for session_id in {session_id for session_id, _, _ in video_ranges}:
        overlap = first_overlap(
            (start, end)
            for candidate_id, start, end in video_ranges
            if candidate_id == session_id
        )
        if overlap:
            start, end = overlap
            raise RemovalError(
                f"{format_timecode(start)}-{format_timecode(end)} overlaps another "
                "removed range on the same Lookout."
            )

    return removals


def _reviewed_lapses(project):
    """The project's signed-off lapses, most recent first, with their cuts.

    Shown read-only under the pass being made: a reviewer deciding what counts
    on lapse seven should be able to see what was ruled on lapses one to six
    without leaving the page.
    """
    reviewed = list(
        Journal.objects
        .filter(project=project, timelapse_review__isnull=False)
        .select_related("timelapse_review", "timelapse_review__reviewer")
        .prefetch_related("timelapses", "timelapse_review__removals")
        .order_by("-timelapse_review__reviewed_at")
    )
    for lapse in reviewed:
        review = lapse.timelapse_review
        removals = list(review.removals.all())
        lapse.reviewer_name = display_name(review.reviewer)
        for session in lapse.timelapses.all():
            session.review_removals = [r for r in removals if r.session_id == session.id]
    return reviewed


@staff_member_required
@check_perms(TIMELAPSE_REVIEW_PERMS)
def timelapse_review_dash(request):
    """The desk: one row per project, its waiting lapses listed underneath."""
    projects = decorate_rows("lookout", QUEUES["lookout"].pending())
    for project in projects:
        project.preview_lapses = project.lapses[:DESK_LAPSE_PREVIEW]
        project.more_lapses = project.lapse_count - len(project.preview_lapses)

    reviewed = list(
        Journal.objects
        .filter(project__deleted=False, timelapse_review__isnull=False)
        .select_related(
            "project", "timelapse_review", "timelapse_review__reviewer",
            "timelapse_review__reviewer__hackclub_profile",
        )
        .prefetch_related("timelapse_review__removals")
        .order_by("-timelapse_review__reviewed_at")[:RECENT_REVIEWS]
    )

    waiting_lapses = sum(project.lapse_count for project in projects)
    return render(request, "root/timelapse_review.html", {
        "projects": projects,
        "reviewed": reviewed,
        "leaderboard": reviewer_leaderboard("timelapse_reviews"),
        **dash_context(request, "lookout", projects, extra_stats=[{
            "label": "Lapses",
            "value": str(waiting_lapses),
            "sub": "across those projects",
        }]),
    })


@staff_member_required
@check_perms(TIMELAPSE_REVIEW_PERMS)
def timelapse_review_next(request):
    """Open the next project with waiting lapses, or return to the desk."""
    return go_to_next(request, "lookout", parse_skip(request))


@staff_member_required
@check_perms(TIMELAPSE_REVIEW_PERMS)
def timelapse_review_project(request, project_id):
    project = get_object_or_404(
        Project.objects.select_related("owner", "owner__hackclub_profile"),
        id=project_id,
        deleted=False,
    )
    pending = decorate_lapses(_pending_lapses(project), QUEUES["lookout"].sla_days)
    reviewed = _reviewed_lapses(project)

    tracked_seconds = sum(lapse.tracked_seconds_total for lapse in pending)
    return render(request, "root/timelapse_review_project.html", {
        "project": project,
        "owner": owner_snapshot(project.owner),
        "pending": pending,
        "reviewed": reviewed,
        "lapse_count": len(pending),
        "lookout_count": sum(lapse.lookout_count for lapse in pending),
        "screenshot_count": sum(
            session.screenshot_count
            for lapse in pending for session in lapse.timelapses.all()
        ),
        "tracked_seconds": tracked_seconds,
        "tracked_display": format_minutes(tracked_seconds // 60),
        "held_ships": sorted({lapse.ship_id for lapse in pending if lapse.ship_id}),
        "reason_max_length": REASON_MAX_LENGTH,
        **review_context(
            request, "lookout", project,
            claimable=bool(pending),
            waiting_since=pending[0].created_at if pending else None,
        ),
    })


@require_POST
@staff_member_required
@check_perms(TIMELAPSE_REVIEW_PERMS)
def timelapse_decision(request, project_id):
    """Sign off every lapse on the project in one pass."""
    project = get_object_or_404(Project, id=project_id, deleted=False)

    lapses = _pending_lapses(project)
    if not lapses:
        messages.error(request, "Every lapse on that project has already been reviewed.")
        return redirect("timelapse_review_dash")

    internal_notes = request.POST.get("internal_notes", "").strip()
    if not internal_notes:
        messages.error(request, "This pass needs a justification before it can be approved.")
        return redirect("timelapse_review_project", project_id=project_id)
    if len(internal_notes) > INTERNAL_NOTES_MAX_LENGTH:
        messages.error(
            request,
            f"Internal notes too long (max {INTERNAL_NOTES_MAX_LENGTH} characters).",
        )
        return redirect("timelapse_review_project", project_id=project_id)

    sessions = {
        session.id: session
        for lapse in lapses
        for session in lapse.timelapses.all()
    }
    try:
        removals = _parse_removals(request, sessions)
    except RemovalError as exc:
        messages.error(request, str(exc))
        return redirect("timelapse_review_project", project_id=project_id)

    with transaction.atomic():
        # Re-read inside the transaction: two reviewers opening the same project
        # is the ordinary way to get here, and a lapse someone else signed off
        # in the meantime is theirs, not ours to write again.
        pending = list(_locked_pending_lapses(project))
        if not pending:
            messages.error(request, "Every lapse on that project has already been reviewed.")
            return redirect("timelapse_review_dash")

        reviews = {
            journal.id: TimelapseReview.objects.create(
                journal=journal,
                reviewer=request.user,
                # One pass, one justification, recorded against each lapse it
                # covered. What is specific to a cut lives on the cut's own
                # reason, which is required per range.
                internal_notes=internal_notes,
            )
            for journal in pending
        }

        kept = []
        for removal in removals:
            review = reviews.get(removal.session.journal_id)
            # A range on a lapse that was signed off by someone else mid-pass
            # has nowhere to go; the rest of the pass still stands.
            if review is None:
                continue
            removal.review = review
            kept.append(removal)
        TimelapseRemoval.objects.bulk_create(kept)

    removed_seconds = sum(removal.duration_seconds for removal in kept)
    dropped = len(removals) - len(kept)

    # No send_slack_dm and no notify_followers, deliberately: Lookout review is
    # internal, and the shipper is not told that it happened or what it cost
    # them.
    record_audit(
        request,
        "timelapse_review",
        target=f"Project #{project.id} ({project.title})",
        metadata={
            "project_id": project.id,
            "project": project.title,
            "journal_ids": [journal.id for journal in pending],
            "review_ids": [review.id for review in reviews.values()],
            "removed_seconds": removed_seconds,
            "removals": [
                {
                    "session_id": removal.session_id,
                    "journal_id": removal.session.journal_id,
                    "start_seconds": removal.start_seconds,
                    "end_seconds": removal.end_seconds,
                    "reason": removal.reason,
                }
                for removal in kept
            ],
        },
    )

    lapses = f"{len(pending)} lapse{'' if len(pending) == 1 else 's'}"
    if kept:
        messages.success(
            request,
            f'Approved {lapses} on "{project.title}" with {format_timecode(removed_seconds)} '
            f"removed across {len(kept)} range{'' if len(kept) == 1 else 's'}.",
        )
    else:
        messages.success(request, f'Approved {lapses} on "{project.title}" with no time removed.')
    if dropped:
        messages.warning(
            request,
            f"{dropped} range{'' if dropped == 1 else 's'} were dropped: another reviewer "
            "signed off the lapse they were on while this pass was open.",
        )

    # On to the next project rather than back to the desk.
    return go_to_next(request, "lookout", parse_skip(request) + [project.id])
