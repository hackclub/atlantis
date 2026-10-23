"""Internal timelapse review.

Every journal lands in this queue when it's created and stays there until a
reviewer signs it off, optionally cutting ranges of unearned time out of the
the footage first. The review itself is invisible to the project owner: nothing
here renders on a page they can load, and a project still ships normally while
its journals sit in the queue. The one thing they do hear is when cuts made in
a week pass an hour, since that time then has to be made up (see
notify_makeup). What waiting does hold up is the regular (T1) review queue —
see timelapse_cleared_ships.

A signed-off review can be corrected afterwards by a reviewer lead from Review
Audit: its descriptions rewritten, its cuts removed or added to. Every
correction writes a TimelapseReviewEdit alongside the change.

The unit of work is a *project*, not a journal. Every lapse on a project is the
same person recording the same build, and judging them one at a time meant
re-learning that context on every visit and paying a page load between each.
One project is one page: all of its unreviewed lapses, all of their recordings,
one pass, one decision. The rows written are still one TimelapseReview per
journal — that part of the record is unchanged.

What a pass produces, per recording, is a description and any number of removed
ranges. The description is required: a recording nobody wrote a line about is
a recording nobody watched, and it is what a T1/T2/T3 reviewer reads instead of
watching the hour again. The notes on the pass as a whole are optional — the
space for something that spans the project rather than one recording.
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.db import transaction
from django.db.models import Exists, OuterRef

from ... import challenge, weeks
from ...models import (
    Journal, Project, Ship, TimelapseAnnotation, TimelapseRemoval, TimelapseReview,
    TimelapseReviewEdit, first_overlap, format_timecode, parse_timecode,
    tracked_to_video, video_to_tracked,
)
from ..helpers import (
    staff_member_required, check_perms, display_name, format_minutes, record_audit, reviewer_leaderboards,
    run_in_background, send_slack_dm,
)
from .queue import (
    QUEUES, dash_context, decorate_lapses, decorate_rows, filter_rows, go_to_next,
    is_own_item, next_item_id, owner_snapshot, parse_skip, refuse_own_item,
    review_context, session_narrowing,
)

# Its own permission, not a tier of the T1/T2/T3 ladder. Organizers keep their
# skeleton key; nobody else gets this by holding another review perm.
TIMELAPSE_REVIEW_PERMS = [
    "atlantis_site.timelapse_review",
    "atlantis_site.organizer",
]

# Correcting a review someone else signed off is oversight, not review work,
# so it belongs to whoever can read Review Audit, not to the reviewers.
REVIEW_EDIT_PERMS = [
    "atlantis_site.reviewer_lead",
    "atlantis_site.organizer",
]
EDIT_NOTE_MAX_LENGTH = 500

REASON_MAX_LENGTH = 1000
INTERNAL_NOTES_MAX_LENGTH = 1000
# One or two lines about a piece of footage, not an essay: the reasons on the
# cuts carry the specifics, and this is the summary someone downstream reads.
DESCRIPTION_MAX_LENGTH = 500

# What a reviewer reaches for most often, offered as one click rather than
# typed out every time. Free text is still the field — these only fill it in.
REMOVAL_REASONS = [
    "AFK / idle screen",
    "Non-project activity",
    "Duplicate session",
    "Unrelated browsing",
    "Other",
]
# Well past anything a real pass needs, and it keeps a crafted POST from
# turning one form submission into thousands of rows. Per submission, which is
# now a whole project rather than a single lapse.
MAX_REMOVALS = 200


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


def _holding_ships(projects):
    """Narrow queued projects to those whose waiting lapses hold a ship out of T1.

    The same rule as the desk's `?shipped=1` filter: an unreviewed lapse that
    is on a ship keeps that ship out of the T1 queue until it's signed off.
    """
    return projects.filter(Exists(
        _unreviewed(Journal.objects.filter(project=OuterRef("pk"), ship__isnull=False))
    ))


def _pending_lapses(project):
    """The project's unreviewed lapses, oldest first, footage attached."""
    return list(
        _unreviewed(Journal.objects.filter(project=project))
        .prefetch_related("timelapses")
        .order_by("created_at", "id")
    )


def _parse_removals(request, sessions):
    """Build the unsaved TimelapseRemoval rows for a posted pass.

    `sessions` maps id -> Timelapse across every lapse being signed off,
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
                f"Range {position} isn't on a recording attached to this project."
            )

        start = parse_timecode(raw_start)
        end = parse_timecode(raw_end)
        if start is None or end is None:
            raise RemovalError(
                f"Range {position}: couldn't read that range. Use m:ss or h:mm:ss."
            )
        if end <= start:
            raise RemovalError(f"Range {position} has to end after it starts.")
        # You cannot remove footage the video doesn't have.
        if end > session.video_seconds:
            raise RemovalError(
                f"Range {position} runs past the end of that recording's video "
                f"({session.video_duration_display} long, "
                f"{format_timecode(session.tracked_seconds)} tracked)."
            )
        # The video can outrun the time it was credited for: its length is
        # measured off the file itself, while the tracked total is whatever
        # the recorder reported, and the two disagree whenever a lapse was
        # credited less time than it has frames. Clamping the end alone is
        # what keeps the deduction inside the tracked time, so a range that
        # begins at or past that point clamps into a range ending before it
        # starts — which the database refuses outright rather than storing a
        # negative deduction. A recording credited 0h0m is the whole of its
        # own timeline that way: every range over it is past the end.
        start_seconds = video_to_tracked(start)
        # The last second of video can stand for a part-minute of tracking
        # (a session's tracked time is whole minutes minus its first bucket),
        # so the end is clamped rather than trusted to convert inside the
        # session.
        end_seconds = min(video_to_tracked(end), session.tracked_seconds)
        if end_seconds <= start_seconds:
            raise RemovalError(
                f"Range {position} is past the end of the time that recording "
                f"tracked ({format_timecode(session.tracked_seconds)}), so there "
                "is nothing there to remove."
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
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            reason=raw_reason,
            # Every range cut from here on is owed back; see
            # challenge.makeup_minutes_by_week.
            counts_toward_makeup=True,
        ))
        video_ranges.append((session.id, start, end))

    # Overlapping ranges would double-count the same seconds against the
    # shipper, so they're rejected rather than merged — per recording, since
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
                "removed range on the same recording."
            )

    return removals


def _parse_descriptions(request, lapses):
    """`{session id: description}` for a posted pass, or raise.

    One per recording in the pass, every one of them required. A recording
    nobody wrote a line about is a recording nobody watched, and the point of
    the field is that the next reviewer down the pipeline can read what this
    one saw without watching the hour again themselves.

    Takes lapses rather than sessions so a missing one can be named by the
    entry it is on, which is what the reviewer is looking at.
    """
    descriptions = {}
    for lapse in lapses:
        for session in lapse.timelapses.all():
            value = request.POST.get(f"description_{session.id}", "").strip()
            if not value:
                raise RemovalError(
                    "Every recording needs a description before the pass can be "
                    f'approved. The one on "{lapse.title}" doesn\'t have one yet.'
                )
            if len(value) > DESCRIPTION_MAX_LENGTH:
                raise RemovalError(
                    f'A recording description on "{lapse.title}" is too long '
                    f"(max {DESCRIPTION_MAX_LENGTH} characters)."
                )
            descriptions[session.id] = value
    return descriptions


def _live_makeup(user):
    """Minutes `user` owes in makeup on the week in progress, 0 outside one."""
    index = weeks.current_week()
    if index is None:
        return 0
    return challenge.makeup_minutes_by_week(user).get(index, 0)


def notify_makeup(owner, project, before):
    """DM the owner if a change just raised what they owe this week.

    `before` is _live_makeup from before the change. Nothing is sent while
    the week's cuts are under the threshold, since nothing is owed then, or
    when the change didn't raise the amount: a correction that removes a cut
    isn't news that needs a DM.
    """
    after = _live_makeup(owner)
    if after <= before:
        return
    profile = getattr(owner, "hackclub_profile", None)
    if profile is None or not profile.slack_id:
        return
    run_in_background(
        send_slack_dm,
        f"{format_minutes(after)} need to be made up because of deflations to your project "
        f"<https://atlantis.hackclub.com/projects/{project.id}|{project.title}>. That's on "
        f"top of this week's {weeks.WEEKLY_HOURS} hours, so this week's bar now needs "
        f"{format_minutes(weeks.WEEKLY_MINUTES + after)}.",
        profile.slack_id,
    )


def _recording_payload(session, removals=(), description=""):
    """One recording, as the review page's JavaScript needs it.

    Everything is in the compiled video's own timeline, because that is the
    only one the reviewer can see or scrub: the ranges they draw, the
    inactivity the checker found, and the length both are measured against.
    The conversion to tracked seconds happens once, on the way into the
    database, and once more here on the way back out.
    """
    return {
        "id": session.id,
        "videoSeconds": session.video_seconds,
        "trackedSeconds": session.tracked_seconds or 0,
        "activityChecked": session.activity_checked,
        "inactivePercentage": session.inactive_percentage or 0.0,
        "inactiveSegments": [
            {
                "start": int(segment.get("start") or 0),
                "end": int(segment.get("end") or 0),
            }
            for segment in (session.inactive_segments or [])
        ],
        # Only ever populated for a lapse that has already been signed off:
        # a pass in progress keeps its ranges in the form, not here.
        "segments": [
            {
                "start": tracked_to_video(removal.start_seconds),
                "end": tracked_to_video(removal.end_seconds),
                "reason": removal.reason,
                "removedSeconds": removal.duration_seconds,
            }
            for removal in removals
        ],
        "description": description,
    }


def _page_payload(project, pending, reviewed):
    """The whole page's footage, as one blob for the annotation editor."""
    entries = []
    for lapse in pending:
        entries.append({
            "id": lapse.id,
            "editable": True,
            "recordings": [
                _recording_payload(session) for session in lapse.timelapses.all()
            ],
        })
    for lapse in reviewed:
        entries.append({
            "id": lapse.id,
            "editable": False,
            "recordings": [
                _recording_payload(
                    session,
                    removals=getattr(session, "review_removals", []),
                    description=getattr(session, "review_description", ""),
                )
                for session in lapse.timelapses.all()
            ],
        })
    return {
        "projectId": project.id,
        "reasons": REMOVAL_REASONS,
        "descriptionMaxLength": DESCRIPTION_MAX_LENGTH,
        "reasonMaxLength": REASON_MAX_LENGTH,
        "entries": entries,
    }


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
        .prefetch_related(
            "timelapses", "timelapse_review__removals",
            "timelapse_review__annotations",
        )
        .order_by("-timelapse_review__reviewed_at")
    )
    # The same annotations decorate_lapses puts on the pending ones, so both
    # halves of the page render through one partial.
    decorate_lapses(reviewed, QUEUES["lookout"].sla_days)
    for lapse in reviewed:
        review = lapse.timelapse_review
        removals = list(review.removals.all())
        descriptions = {
            annotation.session_id: annotation.description
            for annotation in review.annotations.all()
        }
        lapse.reviewer_name = display_name(review.reviewer)
        lapse.removed_total = sum(removal.duration_seconds for removal in removals)
        for session in lapse.timelapses.all():
            session.review_removals = [r for r in removals if r.session_id == session.id]
            session.review_description = descriptions.get(session.id, "")
            # Off the prefetch rather than off the model's aggregating
            # properties: the page draws every recording of every signed-off
            # lapse, and those are two queries apiece.
            session.review_removed_seconds = sum(
                removal.duration_seconds for removal in session.review_removals
            )
            session.review_removed_display = format_timecode(session.review_removed_seconds)
            session.review_approved_display = format_minutes(
                max((session.tracked_seconds or 0) - session.review_removed_seconds, 0) // 60
            )
    return reviewed


@staff_member_required
@check_perms(TIMELAPSE_REVIEW_PERMS)
def timelapse_review_dash(request):
    """The desk: one row per project, counting the lapses waiting on it.

    The lapses aren't listed row by row — a sitting covers the project's whole
    pass, so the queue's job is to name the project and say how much is in it.

    `?filter=` narrows the table (see filter_rows) — `shipped` to projects
    whose waiting lapses are holding a ship out of T1, the tab it opens on
    (`?filter=all` for the rest). Only the table: the
    stats above it still describe the whole queue, and each row keeps its
    place number in it.
    """
    projects = decorate_rows("lookout", QUEUES["lookout"].pending())
    waiting_lapses = sum(project.lapse_count for project in projects)
    context = dash_context(request, "lookout", projects, extra_stats=[{
        "label": "Lapses",
        "value": str(waiting_lapses),
        "phrase": "lapses across them",
    }])
    projects, filters = filter_rows(request, "lookout", projects)
    context.update(filters, pending_count=len(projects))
    return render(request, "root/timelapse_review.html", {
        **context,
        "projects": projects,
        "leaderboard": reviewer_leaderboards("timelapse_reviews", hours=True),
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
    if is_own_item("lookout", project, request.user):
        return refuse_own_item(request, "lookout")
    pending = decorate_lapses(_pending_lapses(project), QUEUES["lookout"].sla_days)
    reviewed = _reviewed_lapses(project)

    tracked_seconds = sum(lapse.tracked_seconds_total for lapse in pending)
    pending_sessions = [
        session for lapse in pending for session in lapse.timelapses.all()
    ]
    # Only the footage this pass covers: an unchecked recording on a lapse
    # somebody already signed off is nobody's problem now.
    unchecked = sum(1 for session in pending_sessions if not session.activity_checked)
    return render(request, "root/timelapse_review_project.html", {
        "project": project,
        "owner": owner_snapshot(project.owner),
        "pending": pending,
        "reviewed": reviewed,
        "lapse_count": len(pending),
        "recording_count": len(pending_sessions),
        "recording_count": sum(lapse.recording_count for lapse in pending),
        "screenshot_count": sum(
            session.screenshot_count for session in pending_sessions
        ),
        "unchecked_count": unchecked,
        "tracked_seconds": tracked_seconds,
        "tracked_display": format_minutes(tracked_seconds // 60),
        "held_ships": sorted({lapse.ship_id for lapse in pending if lapse.ship_id}),
        "reason_max_length": REASON_MAX_LENGTH,
        "description_max_length": DESCRIPTION_MAX_LENGTH,
        "internal_notes_max_length": INTERNAL_NOTES_MAX_LENGTH,
        "removal_reasons": REMOVAL_REASONS,
        "payload": _page_payload(project, pending, reviewed),
        **review_context(
            request, "lookout", project,
            claimable=bool(pending),
            waiting_since=pending[0].created_at if pending else None,
            # Opened from a ship review's "watch" link: look, don't claim.
            peek=request.GET.get("peek") == "1",
        ),
    })


@require_POST
@staff_member_required
@check_perms(TIMELAPSE_REVIEW_PERMS)
def timelapse_decision(request, project_id):
    """Sign off every lapse on the project in one pass."""
    project = get_object_or_404(Project, id=project_id, deleted=False)
    if is_own_item("lookout", project, request.user):
        return refuse_own_item(request, "lookout")

    lapses = _pending_lapses(project)
    if not lapses:
        messages.error(request, "Every lapse on that project has already been reviewed.")
        return redirect("timelapse_review_dash")

    # Optional, unlike the per-recording descriptions below: those carry the
    # account of the pass, and this is the space for anything that spans the
    # whole project rather than one piece of footage.
    internal_notes = request.POST.get("internal_notes", "").strip()
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
        descriptions = _parse_descriptions(request, lapses)
    except RemovalError as exc:
        messages.error(request, str(exc))
        return redirect("timelapse_review_project", project_id=project_id)

    reviewed_here = {lapse.id for lapse in lapses}
    makeup_before = _live_makeup(project.owner)

    with transaction.atomic():
        # Re-read inside the transaction: two reviewers opening the same project
        # is the ordinary way to get here, and a lapse someone else signed off
        # in the meantime is theirs, not ours to write again.
        #
        # Narrowed to the lapses this pass was actually made against, too. A
        # lapse journalled while the page was open has no description in this
        # POST, and signing it off undescribed is worse than leaving it: the
        # queue re-offers what it hasn't seen, and nothing re-offers a review
        # that was already written.
        pending = [
            journal for journal in _locked_pending_lapses(project)
            if journal.id in reviewed_here
        ]
        if not pending:
            messages.error(request, "Every lapse on that project has already been reviewed.")
            return redirect("timelapse_review_dash")

        reviews = {
            journal.id: TimelapseReview.objects.create(
                journal=journal,
                reviewer=request.user,
                # One pass, one set of notes, recorded against each lapse it
                # covered. What is specific to a recording lives on that
                # recording's description, and what is specific to a cut lives
                # on the cut's own reason.
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

        # Same rule for the descriptions: one per recording, on the lapses this
        # pass actually got to write.
        annotations = [
            TimelapseAnnotation(
                review=reviews[sessions[session_id].journal_id],
                session_id=session_id,
                description=description,
            )
            for session_id, description in descriptions.items()
            if sessions[session_id].journal_id in reviews
        ]
        TimelapseAnnotation.objects.bulk_create(annotations)

    removed_seconds = sum(removal.duration_seconds for removal in kept)
    dropped = len(removals) - len(kept)

    # No notify_followers, and no DM about the review itself: timelapse review
    # is internal. The one exception is the owner hearing how much they now
    # have to make up, once this week's cuts pass the threshold.
    if kept:
        notify_makeup(project.owner, project, makeup_before)
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
            "descriptions": {
                str(annotation.session_id): annotation.description
                for annotation in annotations
            },
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

    # On to the next project rather than back to the desk. A session started
    # from a narrowed desk walks that table; otherwise one holding a ship out
    # of T1 goes first, since that's a shipper waiting on us, and only once
    # those are clear the rest of the queue in its usual order.
    skip = parse_skip(request) + [project.id]
    if session_narrowing(request, "lookout") is not None:
        return go_to_next(request, "lookout", skip)
    holding = _holding_ships(QUEUES["lookout"].pending())
    if next_item_id("lookout", skip, request.user, holding) is not None:
        return go_to_next(request, "lookout", skip, items=holding)
    return go_to_next(request, "lookout", skip)


# ---------------------------------------------------------------- corrections

def _review_for_edit(review_id):
    return get_object_or_404(
        TimelapseReview.objects.select_related(
            "reviewer", "reviewer__hackclub_profile",
            "journal", "journal__ship", "journal__project",
            "journal__project__owner", "journal__project__owner__hackclub_profile",
        ),
        id=review_id,
    )


def _removal_record(removal):
    """A removal as the edit history keeps it, so a deleted one can still be read."""
    return {
        "id": removal.id,
        "session_id": removal.session_id,
        "start_seconds": removal.start_seconds,
        "end_seconds": removal.end_seconds,
        "reason": removal.reason,
        "counts_toward_makeup": removal.counts_toward_makeup,
        "created_at": removal.created_at.isoformat(),
    }


def _edit_lines(changes, numbers):
    """One sentence per thing an edit changed, for the history table.

    `numbers` maps session id to the "Recording N" the page calls it. Ranges
    are given on the video's timeline, as the reviewer typed them.
    """
    def recording(session_id):
        return f"recording {numbers.get(session_id, '#' + str(session_id))}"

    def cut(row):
        start = format_timecode(tracked_to_video(row["start_seconds"]))
        end = format_timecode(tracked_to_video(row["end_seconds"]))
        length = format_timecode(row["end_seconds"] - row["start_seconds"])
        return f"{start}-{end} ({length}) on {recording(row['session_id'])}: {row['reason']}"

    lines = [
        f'Description on {recording(change["session_id"])}: "{change["before"]}" → "{change["after"]}"'
        for change in changes.get("descriptions", [])
    ]
    lines += [f"Removed deflation {cut(row)}" for row in changes.get("removals_deleted", [])]
    lines += [f"Added deflation {cut(row)}" for row in changes.get("removals_added", [])]
    return lines


def _refuse_own_review(request, review):
    """A reviewer lead may not correct the review of their own project."""
    if review.journal.project.owner_id != request.user.id:
        return None
    messages.error(request, "You can't edit a review of your own project.")
    return redirect("review_audit")


@staff_member_required
@check_perms(REVIEW_EDIT_PERMS)
def lapse_review_edit(request, review_id):
    """One signed-off review, with every recording's description and cuts editable."""
    review = _review_for_edit(review_id)
    refused = _refuse_own_review(request, review)
    if refused:
        return refused

    journal = review.journal
    project = journal.project
    removals = list(review.removals.all())
    descriptions = {
        annotation.session_id: annotation.description
        for annotation in review.annotations.all()
    }
    recordings = list(journal.timelapses.all())
    for session in recordings:
        session.review_removals = [r for r in removals if r.session_id == session.id]
        session.review_description = descriptions.get(session.id, "")
        removed = sum(r.duration_seconds for r in session.review_removals)
        session.review_removed_display = format_timecode(removed)
        session.review_approved_display = format_minutes(
            max((session.tracked_seconds or 0) - removed, 0) // 60
        )

    edits = list(review.edits.select_related("editor", "editor__hackclub_profile"))
    numbers = {session.id: index for index, session in enumerate(recordings, start=1)}
    for edit in edits:
        edit.editor_name = display_name(edit.editor)
        edit.lines = _edit_lines(edit.changes, numbers)
        edit.removed_before_display = format_timecode(edit.changes.get("removed_seconds_before", 0))
        edit.removed_after_display = format_timecode(edit.changes.get("removed_seconds_after", 0))

    return render(request, "root/lapse_review_edit.html", {
        "review": review,
        "journal": journal,
        "project": project,
        "owner_name": display_name(project.owner),
        "reviewer_name": display_name(review.reviewer),
        "recordings": recordings,
        "removed_display": format_timecode(sum(r.duration_seconds for r in removals)),
        "edits": edits,
        # Pearls for a finalized ship were paid off the approved time as it
        # stood then. A correction changes the record and the makeup owed, but
        # it doesn't reach back into a payout that has already happened.
        "payout_done": bool(journal.ship and journal.ship.status == Ship.ShipStatus.FINALIZED),
        "blank_rows": range(3),
        "removal_reasons": REMOVAL_REASONS,
        "reason_max_length": REASON_MAX_LENGTH,
        "description_max_length": DESCRIPTION_MAX_LENGTH,
        "note_max_length": EDIT_NOTE_MAX_LENGTH,
    })


@require_POST
@staff_member_required
@check_perms(REVIEW_EDIT_PERMS)
def lapse_review_save(request, review_id):
    """Apply one correction: descriptions, cuts removed and cuts added, all or nothing."""
    review = _review_for_edit(review_id)
    refused = _refuse_own_review(request, review)
    if refused:
        return refused
    project = review.journal.project
    back = redirect("lapse_review_edit", review_id=review.id)

    note = request.POST.get("edit_note", "").strip()
    if not note:
        messages.error(request, "Say why you're changing this review.")
        return back
    if len(note) > EDIT_NOTE_MAX_LENGTH:
        messages.error(request, f"That note is too long (max {EDIT_NOTE_MAX_LENGTH} characters).")
        return back

    sessions = {session.id: session for session in review.journal.timelapses.all()}
    try:
        added = _parse_removals(request, sessions)
    except RemovalError as exc:
        messages.error(request, str(exc))
        return back

    makeup_before = _live_makeup(project.owner)

    with transaction.atomic():
        # Locked so two leads correcting the same review can't each check their
        # new ranges against cuts the other is deleting.
        review = TimelapseReview.objects.select_for_update().get(pk=review.pk)
        existing = {removal.id: removal for removal in review.removals.all()}
        removed_before = sum(r.duration_seconds for r in existing.values())

        delete_ids = set()
        for raw in request.POST.getlist("delete_removal"):
            try:
                removal_id = int(raw)
            except ValueError:
                continue
            if removal_id not in existing:
                messages.error(request, "One of the cuts you removed isn't on this review any more. Reload and try again.")
                return back
            delete_ids.add(removal_id)

        # The new ranges were checked against each other on the way in. They
        # also mustn't overlap a cut that is staying, or the same seconds
        # would be charged twice.
        staying = [r for r in existing.values() if r.id not in delete_ids]
        for removal in added:
            for other in staying:
                if (
                    other.session_id == removal.session.id
                    and removal.start_seconds < other.end_seconds
                    and other.start_seconds < removal.end_seconds
                ):
                    messages.error(
                        request,
                        f"{removal.video_range_display} overlaps a cut already on that recording "
                        f"({other.video_range_display}).",
                    )
                    return back

        annotations = {a.session_id: a for a in review.annotations.all()}
        description_changes = []
        for session_id, session in sessions.items():
            key = f"description_{session_id}"
            if key not in request.POST:
                continue
            value = request.POST[key].strip()
            before = annotations[session_id].description if session_id in annotations else ""
            if value == before:
                continue
            if not value:
                messages.error(request, "A recording's description can be rewritten but not left empty.")
                return back
            if len(value) > DESCRIPTION_MAX_LENGTH:
                messages.error(
                    request,
                    f"A recording description is too long (max {DESCRIPTION_MAX_LENGTH} characters).",
                )
                return back
            description_changes.append({"session_id": session_id, "before": before, "after": value})

        if not (description_changes or delete_ids or added):
            messages.error(request, "Nothing on the review was changed.")
            return back

        for change in description_changes:
            annotation = annotations.get(change["session_id"])
            if annotation is None:
                # A review from before descriptions were required has none to edit.
                TimelapseAnnotation.objects.create(
                    review=review, session_id=change["session_id"], description=change["after"],
                )
            else:
                annotation.description = change["after"]
                annotation.save(update_fields=["description"])

        deleted = [_removal_record(existing[removal_id]) for removal_id in sorted(delete_ids)]
        TimelapseRemoval.objects.filter(id__in=delete_ids).delete()

        for removal in added:
            removal.review = review
        TimelapseRemoval.objects.bulk_create(added)

        removed_after = (
            removed_before
            - sum(existing[removal_id].duration_seconds for removal_id in delete_ids)
            + sum(removal.duration_seconds for removal in added)
        )
        changes = {
            "descriptions": description_changes,
            "removals_deleted": deleted,
            "removals_added": [
                {
                    "session_id": removal.session_id,
                    "start_seconds": removal.start_seconds,
                    "end_seconds": removal.end_seconds,
                    "reason": removal.reason,
                }
                for removal in added
            ],
            "removed_seconds_before": removed_before,
            "removed_seconds_after": removed_after,
        }
        edit = TimelapseReviewEdit.objects.create(
            review=review, editor=request.user, note=note, changes=changes,
        )

    record_audit(
        request,
        "edit_timelapse_review",
        target=f"Project #{project.id} ({project.title})",
        metadata={
            "project_id": project.id,
            "journal_id": review.journal_id,
            "review_id": review.id,
            "edit_id": edit.id,
            "note": note,
            **changes,
        },
    )
    if added:
        notify_makeup(project.owner, project, makeup_before)

    messages.success(
        request,
        f"Review updated. Time removed went from {format_timecode(removed_before)} "
        f"to {format_timecode(removed_after)}.",
    )
    return back
