from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

import json

from ...models import (
    AirtableSubmission, InternalComment, Profile, Project, Ship, T1, T2, T3,
    PAYOUT_MULTIPLIER_DEFAULT, PAYOUT_MULTIPLIER_MAX, PAYOUT_MULTIPLIER_MIN,
    PAYOUT_MULTIPLIER_STEP, PEARLS_PER_HOUR,
)
from ...checklists import T1_CHECKLIST, ticked, unticked, unticked_message
from ...submissions import build_override_justification, submit_ship
from ..helpers import check_perms, send_slack_dm, send_slack_message, slack_mention, record_audit, get_model_info, build_journal_timeline, reviewer_leaderboard, approved_minutes_for_journals, build_review_history, payable_minutes_for_ship, payout_buckets, ship_payout, rate_limit, safe_redirect_back, display_name, INT_FIELD_MAX, INT_FIELD_MIN
from ...challenge import brackets_for, draw_brackets
from .queue import (
    QUEUES, annotate_recordings, dash_context, decorate_rows, go_to_next,
    journal_stats, owner_snapshot, parse_skip, preflight_checks, review_context,
    ship_snapshot, sibling_reviews,
)

INTERNAL_COMMENT_MAX_LENGTH = 1000
T1_FIELD_MAX_LENGTH = 1000
T2_FIELD_MAX_LENGTH = 1000

TIMELAPSE_PENDING_MESSAGE = (
    "That ship's timelapses haven't finished internal review yet. It'll appear "
    "in the queue once they have."
)

# The T1 desk and a ship's T1 page, which a reviewer lead can read (it is where
# rollbacks are made from) without being able to decide anything on them.
T1_VIEW_PERMS = [
    "atlantis_site.t1_review",
    "atlantis_site.t2_review",
    "atlantis_site.organizer",
    "atlantis_site.t3_review",
    "atlantis_site.reviewer_lead",
]

T1_DECIDE_PERMS = [
    "atlantis_site.t1_review",
    "atlantis_site.t2_review",
    "atlantis_site.organizer",
    "atlantis_site.t3_review",
]

ROLLBACK_PERMS = ["atlantis_site.reviewer_lead", "atlantis_site.organizer"]

# Leaves room in the InternalComment the rollback writes for the line that says
# whose review it was.
ROLLBACK_REASON_MAX_LENGTH = 800

COMMENT_PERMS = [
    "atlantis_site.t1_review",
    "atlantis_site.t2_review",
    "atlantis_site.t3_review",
    "atlantis_site.organizer",
]

def parse_payout_multiplier(raw):
    """
    Read the T3 pearl-multiplier slider. Returns (multiplier, error); the
    multiplier is snapped to the slider's step so a hand-crafted POST can't
    store a value the form could never produce.
    """
    raw = (raw or "").strip()
    if not raw:
        return PAYOUT_MULTIPLIER_DEFAULT, None

    try:
        multiplier = Decimal(raw)
    except InvalidOperation:
        return None, f"Expected a number for the pearl multiplier, got {raw}"

    # Decimal happily parses "NaN" and "Infinity", and quantize() raises on
    # the latter — neither may reach the comparison below.
    if not multiplier.is_finite():
        return None, f"Expected a number for the pearl multiplier, got {raw}"

    multiplier = multiplier.quantize(PAYOUT_MULTIPLIER_STEP, rounding=ROUND_HALF_EVEN)
    if not PAYOUT_MULTIPLIER_MIN <= multiplier <= PAYOUT_MULTIPLIER_MAX:
        return None, (
            f"Pearl multiplier must be between {PAYOUT_MULTIPLIER_MIN}x and "
            f"{PAYOUT_MULTIPLIER_MAX}x. (got: {multiplier}x)"
        )
    return multiplier, None

def project_link(project):
    return f"<https://atlantis.hackclub.com/projects/{project.id}|{project.title}>"

def feedback_line(feedback):
    """
    Slack's italics don't survive a line break, so feedback written as more
    than one paragraph came out wrapped in literal underscores. Quote it
    instead: `>>>` blockquotes everything after it, newlines and all, which
    is why this has to be the last thing in the message.
    """
    feedback = (feedback or "").strip()
    if not feedback:
        return "They didn't leave any feedback."
    return f"Here's what they said about it:\n>>> {feedback}"

def ping_review_checkpoint(ship, reviewer, tier, outcome, feedback):
    """
    Every T1/T2 decision is posted in the review checkpoint channel with the
    shipper and the reviewer both pinged, so the two can talk it over. This is
    the only notification a review sends — nothing about T1 or T2 is DM'd, so
    a decision the shipper wants to argue with always lands somewhere they can
    reply. Only T3, which ends the ship's journey, DMs them.
    """
    if not settings.REVIEW_CHECKPOINT_ID:
        return False

    project = ship.project
    return send_slack_message(
        f"{slack_mention(project.owner)} your project {project_link(project)} has been "
        f"{tier} reviewed by {slack_mention(reviewer)} and {outcome}. {feedback_line(feedback)}",
        settings.REVIEW_CHECKPOINT_ID,
    )

def report_submission(request, submission):
    """Tell the reviewer what became of the Airtable record.

    Finalization has already happened by the time this runs, so none of these
    are errors that undo anything — they say whether HQ has the project yet and
    what to do if not.
    """
    Status = AirtableSubmission.Status
    if submission.status == Status.SUBMITTED:
        note = f" Note: {submission.notes}" if submission.notes else ""
        messages.success(request, f"Submitted to Airtable as {submission.record_id}.{note}")
    elif submission.status == Status.SENDING:
        messages.warning(
            request,
            "Airtable never answered, so it's unclear whether the record was "
            f"created ({submission.error}). Check the table before resubmitting. "
            "This one will not retry on its own.",
        )
    else:
        messages.error(
            request,
            f"The project was finalized but its Airtable record was not created: "
            f"{submission.error} The submit_airtable command will retry it.",
        )

def t1_rollback_block(t1):
    """Why this T1 review can't be rolled back, or None when it can.

    A rollback undoes one decision and puts the ship back in the T1 queue as if
    it had never been made. That is only honest while nothing has been built on
    top of the decision: once the ship has moved on, been looked at by a later
    tier, or been reshipped, putting it back would undo other people's work
    too, and that is a different conversation.
    """
    ship = t1.ship
    expected = Ship.ShipStatus.T2_QUEUE if t1.approved else Ship.ShipStatus.REJECTED
    if ship.status != expected:
        return f"the ship is now {ship.get_status_display().lower()}"

    latest = ship.t1_reviews.order_by("-reviewed_at", "-id").first()
    if latest.id != t1.id:
        return "a later T1 review has replaced it"

    later_tier = (
        ship.t2_reviews.filter(reviewed_at__gte=t1.reviewed_at).exists()
        or ship.t3_reviews.filter(reviewed_at__gte=t1.reviewed_at).exists()
    )
    if later_tier:
        return "a later tier has reviewed the ship since"

    if not t1.approved and ship.project.ships.filter(id__gt=ship.id).exists():
        return "the project has been reshipped since"

    return None

def can_roll_back(user):
    return any(user.has_perm(perm) for perm in ROLLBACK_PERMS)

@staff_member_required
@check_perms(T1_VIEW_PERMS)
def review_dash(request):
    ships = decorate_rows("t1", QUEUES["t1"].pending())
    context = dash_context(request, "t1", ships)

    # Only worked out for someone who can act on it: it is a few queries a row.
    if can_roll_back(request.user):
        reviews = T1.objects.select_related("ship", "ship__project").in_bulk(
            [row["id"] for row in context["all_reviews"]]
        )
        for row in context["all_reviews"]:
            t1 = reviews.get(row["id"])
            row["rollback_block"] = t1_rollback_block(t1) if t1 else "it no longer exists"
            row["rollback_url"] = reverse("t1_rollback", args=[row["id"]])

    return render(request, "root/review.html", {
        "ships": ships,
        "leaderboard": reviewer_leaderboard("t1_reviews"),
        "can_roll_back": can_roll_back(request.user),
        "rollback_reason_max": ROLLBACK_REASON_MAX_LENGTH,
        **context,
    })

@staff_member_required
@check_perms(T1_DECIDE_PERMS)
def review_next(request):
    """Open the next T1 ship, or return to the desk when the queue is clear."""
    return go_to_next(request, "t1", parse_skip(request))

@staff_member_required
@check_perms(T1_VIEW_PERMS)
def review_project(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
    if not ship.timelapse_cleared:
        messages.error(request, TIMELAPSE_PENDING_MESSAGE)
        return redirect("review_dash")
    journals = annotate_recordings(ship.project.journals.order_by('-id'))
    timeline = build_journal_timeline(journals, ship.project.ships.all())
    try:
        hasMake = bool(get_model_info(ship.project.printablesUrl.split('/model/')[1].split('-')[0])["makesCount"])
    except:
        hasMake = False

    owner = owner_snapshot(ship.project.owner)
    subject = ship_snapshot(ship)
    return render(request, "root/review_project.html", {
        "ship": ship,
        "journals": journals,
        "timeline": timeline,
        "review_history": build_review_history(ship),
        "hasMake": hasMake,
        "owner": owner,
        "logged_time": approved_minutes_for_journals(ship.project.journals.all()),
        "subject": subject,
        "siblings": sibling_reviews(ship),
        "journal_stats": journal_stats(journals),
        "preflight": preflight_checks(ship, subject, owner, has_make=hasMake),
        "t1_checklist": T1_CHECKLIST,
        # A lead who can't decide is only reading, and shouldn't hold a claim
        # that turns the reviewers who can away.
        **review_context(request, "t1", ship, claimable=(
            ship.status == Ship.ShipStatus.T1_QUEUE
            and any(request.user.has_perm(perm) for perm in T1_DECIDE_PERMS)
        )),
    })

@require_POST
@staff_member_required
@check_perms(T1_DECIDE_PERMS)
def t1_decision(request, ship_id): 
    reviewer = request.user
    feedback = request.POST.get("feedback", "").strip()
    internal_notes = request.POST.get("internal_notes", "").strip()

    if len(feedback) > T1_FIELD_MAX_LENGTH or len(internal_notes) > T1_FIELD_MAX_LENGTH:
        messages.error(request, f"Feedback or internal notes too long (max {T1_FIELD_MAX_LENGTH} char)")
        return redirect("review_project", ship_id=ship_id)

    approved_raw = request.POST.get("approved", "").strip()

    if approved_raw not in ("approved", "denied"):
        messages.error(request, f"How did we get here? (approved: {approved_raw})")
        return redirect("review_project", ship_id=ship_id)

    approved = approved_raw == "approved"

    with transaction.atomic():
        ship = get_object_or_404(Ship.objects.select_for_update(), id=ship_id)

        if not ship.status == Ship.ShipStatus.T1_QUEUE:
            messages.error(request, "ship not in T1 queue")
            return redirect("review_dash")

        if not ship.timelapse_cleared:
            messages.error(request, TIMELAPSE_PENDING_MESSAGE)
            return redirect("review_dash")

        # Only an approval is held to the checklist. A rejection is already a
        # reviewer saying something is wrong, and making them tick eight boxes
        # to say so would only teach them to tick eight boxes. Last of the
        # gates, so a ship that was never reviewable here is told that rather
        # than sent off to read a checklist about it.
        if approved:
            missing = unticked(T1_CHECKLIST, request)
            if missing:
                messages.error(request, unticked_message(
                    missing,
                    "Work through the review checklist before approving; still unchecked:",
                ))
                return redirect("review_project", ship_id=ship_id)
            ship.status = Ship.ShipStatus.T2_QUEUE
        else:
            ship.status = Ship.ShipStatus.REJECTED

        ship.save()

        t1 = T1.objects.create(
            reviewer=reviewer,
            ship=ship,
            feedback=feedback,
            internal_notes=internal_notes,
            approved=approved
        )

    ping_review_checkpoint(ship, reviewer, "T1", "approved" if approved else "rejected", feedback)

    record_audit(request, "t1_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t1_id": t1.id,
        "project": ship.project.title,
        "approved": approved,
        "new_ship_status": ship.status,
        # What the reviewer confirmed they looked at. On an approval this is
        # the whole list by definition; it is recorded anyway so a ship that
        # turns out to be bad can be traced back to a reviewer who said they
        # checked, rather than to nothing at all.
        "checklist": ticked(T1_CHECKLIST, request),
    })

    # Straight on to the next ship in the queue rather than back to the desk:
    # the desk is a place to start from, not somewhere to pass through between
    # every review.
    return go_to_next(request, "t1", parse_skip(request) + [ship.id])

@require_POST
@staff_member_required
@check_perms(ROLLBACK_PERMS)
def t1_rollback(request, t1_id):
    """Undo a T1 decision and put the ship back in the T1 queue.

    The review row is deleted rather than marked, so nothing downstream — the
    shipper's feedback, the leaderboard, the desk stats — goes on counting a
    decision that was taken back. What it said is not lost: the audit entry
    carries all of it, and an internal comment on the ship tells the next
    reviewer that there was one and why it went.
    """
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "Say why the review is being rolled back.")
        return safe_redirect_back(request)
    if len(reason) > ROLLBACK_REASON_MAX_LENGTH:
        messages.error(request, f"Rollback reason too long (max {ROLLBACK_REASON_MAX_LENGTH} characters).")
        return safe_redirect_back(request)

    with transaction.atomic():
        # The ship is locked before the review is read, so two leads rolling
        # back the same review can't both get past the checks below.
        ship = get_object_or_404(Ship.objects.select_for_update(), t1_reviews__id=t1_id)
        t1 = get_object_or_404(
            T1.objects.select_related("reviewer", "reviewer__hackclub_profile"), id=t1_id, ship=ship,
        )

        block = t1_rollback_block(t1)
        if block:
            messages.error(request, f"That T1 review can't be rolled back: {block}.")
            return safe_redirect_back(request)

        previous_status = ship.status
        verdict = "approval" if t1.approved else "rejection"
        snapshot = {
            "ship_id": ship.id,
            "t1_id": t1.id,
            "project": ship.project.title,
            "reviewer": t1.reviewer.username,
            "reviewed_at": t1.reviewed_at.isoformat(),
            "approved": t1.approved,
            "feedback": t1.feedback,
            "internal_notes": t1.internal_notes,
            "reason": reason,
            "previous_ship_status": previous_status,
            "new_ship_status": Ship.ShipStatus.T1_QUEUE,
        }

        reviewed_on = timezone.localtime(t1.reviewed_at)
        InternalComment.objects.create(
            ship=ship,
            author=request.user,
            text=(
                f"Rolled back {display_name(t1.reviewer)}'s T1 {verdict} "
                f"from {reviewed_on:%b} {reviewed_on.day}, {reviewed_on.year}: {reason}"
            ),
        )
        t1.delete()
        ship.status = Ship.ShipStatus.T1_QUEUE
        ship.save()

    record_audit(request, "t1_rollback", target=f"Ship #{ship.id} ({ship.project.title})", metadata=snapshot)

    # The shipper was told about the decision in the checkpoint channel, so
    # they are told there that it no longer stands.
    if settings.REVIEW_CHECKPOINT_ID:
        send_slack_message(
            f"{slack_mention(ship.project.owner)} the T1 {verdict} of your project "
            f"{project_link(ship.project)} has been rolled back by {slack_mention(request.user)}. "
            f"It's back in the T1 queue and will be reviewed again.",
            settings.REVIEW_CHECKPOINT_ID,
        )

    messages.success(request, f"Rolled back the T1 {verdict} of {ship.project.title}. It's back in the T1 queue.")
    return safe_redirect_back(request)

@staff_member_required
@check_perms(["atlantis_site.t2_review", "atlantis_site.organizer", "atlantis_site.t3_review"])
def ysws_review_dash(request):
    ships = decorate_rows("t2", QUEUES["t2"].pending())
    return render(request, "root/ysws_review.html", {
        "ships": ships,
        "leaderboard": reviewer_leaderboard("t2_reviews"),
        **dash_context(request, "t2", ships),
    })

@staff_member_required
@check_perms(["atlantis_site.t2_review", "atlantis_site.organizer", "atlantis_site.t3_review"])
def ysws_review_next(request):
    return go_to_next(request, "t2", parse_skip(request))

@staff_member_required
@check_perms(["atlantis_site.t2_review", "atlantis_site.organizer", "atlantis_site.t3_review"])
def ysws_review_project(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
    journals = annotate_recordings(ship.project.journals.order_by('-id'))
    timeline = build_journal_timeline(journals, ship.project.ships.all())
    # The unpaid work this ship is answerable for, matching what t2_decision
    # validates the deduction against — the sidebar's pearl preview has to
    # agree with the ceiling the POST handler will enforce.
    logged_time = payable_minutes_for_ship(ship)
    base_layers, payout_lines, _drawn = ship_payout(ship, logged_time)
    owner = owner_snapshot(ship.project.owner)
    subject = ship_snapshot(ship)
    return render(request, "root/ysws_review_project.html", {
        "ship": ship,
        "journals": journals,
        "timeline": timeline,
        "review_history": build_review_history(ship),
        "logged_time": logged_time,
        "base_layers": base_layers,
        "payout_lines": payout_lines,
        "payout_buckets": json.dumps(payout_buckets(ship)),
        "pearls_per_hour": PEARLS_PER_HOUR,
        "owner": owner,
        "subject": subject,
        "siblings": sibling_reviews(ship),
        "journal_stats": journal_stats(journals),
        "preflight": preflight_checks(ship, subject, owner),
        **review_context(request, "t2", ship, claimable=ship.status == Ship.ShipStatus.T2_QUEUE),
    })

@require_POST
@staff_member_required
@check_perms(["atlantis_site.t2_review", "atlantis_site.organizer", "atlantis_site.t3_review"])
def t2_decision(request, ship_id):
    reviewer = request.user
    decision = request.POST.get("decision", "").strip()
    deductions = request.POST.get("deductions", "0").strip()

    try:
        deductions = int(deductions) if deductions else 0
    except ValueError:
        messages.error(request, f"Expected integer, got {deductions}")
        return redirect("ysws_review_dash")

    if deductions < 0:
        messages.error(request, f"Deductions can't be negative. (deductions: {deductions})")
        return redirect("ysws_review_dash")

    feedback = request.POST.get("feedback", "").strip()
    justification = request.POST.get("justification", "").strip()

    if len(feedback) > T2_FIELD_MAX_LENGTH or len(justification) > T2_FIELD_MAX_LENGTH:
        messages.error(request, f"Feedback or justification length too long (max {T2_FIELD_MAX_LENGTH} char)")
        return redirect("ysws_review_dash")

    with transaction.atomic():
        ship = get_object_or_404(Ship.objects.select_for_update(), id=ship_id)

        total_time = payable_minutes_for_ship(ship)
        if total_time < deductions:
            messages.error(request, f"Deduction too large. (total_time: {total_time}, deductions: {deductions})")
            return redirect("ysws_review_dash")

        if not ship.status == Ship.ShipStatus.T2_QUEUE:
            messages.error(request, "ship not in T2 queue")
            return redirect("ysws_review_dash")

        match decision:
            case T2.Decision.APPROVE:
                ship.status = Ship.ShipStatus.T3_QUEUE
                message = "approved"
            case T2.Decision.RETURN_T1:
                ship.status = Ship.ShipStatus.T1_QUEUE
                message = "returned to T1 reviewers"
            case _:
                messages.error(request, f"How did we get here? (decision: {decision})")
                return redirect("ysws_review_dash")

        ship.save()

        t2 = T2.objects.create(
            ship=ship,
            reviewer=reviewer,
            decision=decision,
            deductions=deductions,
            feedback=feedback,
            justification=justification
        )

    ping_review_checkpoint(ship, reviewer, "T2", message, feedback)

    record_audit(request, "t2_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t2_id": t2.id,
        "project": ship.project.title,
        "decision": decision,
        "deductions": deductions,
        "new_ship_status": ship.status,
    })

    return go_to_next(request, "t2", parse_skip(request) + [ship.id])

@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.t3_review"])
def fraud_review_dash(request):
    ships = decorate_rows("t3", QUEUES["t3"].pending())
    return render(request, "root/fraud_review.html", {
        "ships": ships,
        "leaderboard": reviewer_leaderboard("t3_reviews"),
        **dash_context(request, "t3", ships),
    })

@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.t3_review"])
def fraud_review_next(request):
    return go_to_next(request, "t3", parse_skip(request))

@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.t3_review"])
def fraud_review_project(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
    journals = annotate_recordings(ship.project.journals.order_by('-id'))
    timeline = build_journal_timeline(journals, ship.project.ships.all())
    logged_time = payable_minutes_for_ship(ship)

    latest_t2 = ship.t2_reviews.order_by('-id').first()
    deductions = latest_t2.deductions if latest_t2 else 0
    total_time = max(logged_time - deductions, 0)
    base_layers, payout_lines, _drawn = ship_payout(ship, total_time)

    owner = owner_snapshot(ship.project.owner)
    subject = ship_snapshot(ship)
    return render(request, "root/fraud_review_project.html", {
        "ship": ship,
        "journals": journals,
        "timeline": timeline,
        "review_history": build_review_history(ship),
        "logged_time": logged_time,
        "deductions": deductions,
        "total_time": total_time,
        "base_layers": base_layers,
        "payout_lines": payout_lines,
        "payout_buckets": json.dumps(payout_buckets(ship)),
        "pearls_per_hour": PEARLS_PER_HOUR,
        "multiplier_min": PAYOUT_MULTIPLIER_MIN,
        "multiplier_max": PAYOUT_MULTIPLIER_MAX,
        "multiplier_step": PAYOUT_MULTIPLIER_STEP,
        "multiplier_default": PAYOUT_MULTIPLIER_DEFAULT,
        # Airtable's override-hours justification: the T2 reviewer's words,
        # then every Lookout on the ship with the ranges cut from it and why.
        # Nothing else shows a T3 reviewer the timelapse review in full.
        "override_justification": build_override_justification(ship),
        "airtable_submission": AirtableSubmission.objects.filter(ship=ship).first(),
        "owner": owner,
        "subject": subject,
        "siblings": sibling_reviews(ship),
        "journal_stats": journal_stats(journals),
        "preflight": preflight_checks(ship, subject, owner),
        **review_context(request, "t3", ship, claimable=ship.status == Ship.ShipStatus.T3_QUEUE),
    })

@require_POST
@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.t3_review"])
def t3_decision(request, ship_id):
    reviewer = request.user
    decision = request.POST.get("decision", "").strip()
    internal_notes = request.POST.get("internal_notes", "").strip()

    payout_time_raw = request.POST.get("payout_time", "0").strip()
    airtable_time_raw = request.POST.get("airtable_time", "0").strip()

    try:
        payout_time = int(payout_time_raw)
    except ValueError:
        messages.error(request, f"Expected integer, receieved {payout_time_raw}")
        return redirect("fraud_review_project", ship_id=ship_id)

    try:
        airtable_time = int(airtable_time_raw)
    except ValueError:
        messages.error(request, f"Expected integer, receieved {airtable_time_raw}")
        return redirect("fraud_review_project", ship_id=ship_id)

    # Both land in plain integer columns, and payout_time is multiplied up into
    # a pearl balance that lands in a third. Past the range the driver raises
    # NumericValueOutOfRange, which is a 500 rather than a number to correct.
    for label, value in (("Payout time", payout_time), ("Airtable time", airtable_time)):
        if not INT_FIELD_MIN <= value <= INT_FIELD_MAX:
            messages.error(request, f"{label} is out of range.")
            return redirect("fraud_review_project", ship_id=ship_id)

    payout_multiplier, multiplier_error = parse_payout_multiplier(request.POST.get("payout_multiplier"))
    if multiplier_error:
        messages.error(request, multiplier_error)
        return redirect("fraud_review_project", ship_id=ship_id)

    with transaction.atomic():
        ship = get_object_or_404(Ship.objects.select_for_update(), id=ship_id)

        if not ship.status == Ship.ShipStatus.T3_QUEUE:
            messages.error(request, "ship not in T3 queue")
            return redirect("fraud_review_dash")

        payout_layers = 0
        payout_detail = []
        match decision:
            case T3.Decision.RETURN_T1:
                ship.status = Ship.ShipStatus.T1_QUEUE
                message = "returned to T1 reviewers"
            case T3.Decision.RETURN_T2:
                ship.status = Ship.ShipStatus.T2_QUEUE
                message = "returned to T2 reviewers"
            case T3.Decision.APPROVE:
                # A shipper who never came through the HCA login has no profile
                # row; paying them out used to be a DoesNotExist.
                owner_profile, _ = Profile.objects.get_or_create(user=ship.project.owner)
                profile = Profile.objects.select_for_update().get(pk=owner_profile.pk)
                # Priced inside the lock and against brackets read inside it:
                # two ships of the same shipper finalizing at once must not
                # both spend the same week's base-rate allowance.
                payout_layers, payout_lines, drawn = ship_payout(
                    ship,
                    payout_time,
                    payout_multiplier,
                    brackets_for(ship.project.owner),
                )
                if not INT_FIELD_MIN <= profile.layers + payout_layers <= INT_FIELD_MAX:
                    messages.error(request, "That payout would put the shipper's pearl balance out of range.")
                    return redirect("fraud_review_project", ship_id=ship_id)
                ship.status = Ship.ShipStatus.FINALIZED
                profile.layers += payout_layers
                profile.save(update_fields=["layers"])
                draw_brackets(ship.project.owner, drawn)
                payout_detail = [
                    {"week": line.label, "minutes": line.minutes, "rate": str(line.rate)}
                    for line in payout_lines
                ]
            case _:
                messages.error(request, f"Invalid decision (received decision: {decision})")
                return redirect("fraud_review_dash")

        ship.save()

        t3 = T3.objects.create(
            ship=ship,
            reviewer=reviewer,
            decision=decision,
            internal_notes=internal_notes,
            payout_time=payout_time,
            airtable_time=airtable_time,
            payout_multiplier=payout_multiplier,
            payout_layers=payout_layers,
        )

    # Outside the transaction on purpose: the ship is committed as finalized
    # before anything is sent to Airtable, so a submission that fails leaves a
    # finalized ship and a retryable row rather than rolling the finalization
    # back. submit_ship is safe to call again and refuses to send twice.
    submission = submit_ship(ship) if decision == T3.Decision.APPROVE else None

    owner_profile = getattr(ship.project.owner, "hackclub_profile", None)
    owner_slack_id = owner_profile.slack_id if owner_profile else ""
    send_slack_dm(f"Your project <https://atlantis.hackclub.com/projects/{ship.project.id}|{ship.project.title}> has been finalized and you've received {payout_layers} pearls for it!", owner_slack_id) if decision == T3.Decision.APPROVE else send_slack_dm(f"Your project <https://atlantis.hackclub.com/projects/{ship.project.id}|{ship.project.title}> has been {message}!", owner_slack_id)

    record_audit(request, "t3_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t3_id": t3.id,
        "project": ship.project.title,
        "decision": decision,
        "payout_time": payout_time,
        "airtable_time": airtable_time,
        # str: metadata is a plain JSONField and Decimal isn't serialisable.
        "payout_multiplier": str(payout_multiplier),
        "payout_layers": payout_layers,
        # Which weeks the pearls came out of and at what rate — the only
        # record of how a split payout was arrived at.
        "payout_breakdown": payout_detail,
        "new_ship_status": ship.status,
        "airtable_status": submission.status if submission else "",
        "airtable_record_id": submission.record_id if submission else "",
        "airtable_error": submission.error if submission else "",
    })

    if submission:
        report_submission(request, submission)
    return go_to_next(request, "t3", parse_skip(request) + [ship.id])

@staff_member_required
@require_POST
@check_perms(COMMENT_PERMS)
@rate_limit("internal_comment", 2)
def add_internal_comment(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
    text = request.POST.get("text", "").strip()

    if not text:
        messages.error(request, "An internal comment can't be empty.")
        return safe_redirect_back(request)

    if len(text) > INTERNAL_COMMENT_MAX_LENGTH:
        messages.error(request, f"Internal comment too long (max {INTERNAL_COMMENT_MAX_LENGTH} characters).")
        return safe_redirect_back(request)

    comment = InternalComment.objects.create(ship=ship, author=request.user, text=text)

    record_audit(request, "internal_comment", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "comment_id": comment.id,
        "project": ship.project.title,
    })

    messages.success(request, "Internal comment added.")
    return safe_redirect_back(request)

@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer", "atlantis_site.t2_review", "atlantis_site.t3_review"])
def lock_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, deleted=False)
    
    project.locked = True
    project.save()

    record_audit(request, "lock_project", target=f"Project #{project.id} ({project.title})", metadata={
        "project_id": project.id,
        "project": project.title,
        "owner": project.owner.username,
    })

    owner_profile = getattr(project.owner, "hackclub_profile", None)
    owner_slack_id = owner_profile.slack_id if owner_profile else ""
    if owner_slack_id:
        send_slack_dm(f"Your project <https://atlantis.hackclub.com/projects/{project_id}|{project.title}> has been locked.", owner_slack_id)

    return safe_redirect_back(request)

@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer", "atlantis_site.t2_review", "atlantis_site.t3_review"])
def unlock_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, deleted=False)
    
    project.locked = False
    project.save()

    record_audit(request, "unlock_project", target=f"Project #{project.id} ({project.title})", metadata={
        "project_id": project.id,
        "project": project.title,
        "owner": project.owner.username,
    })

    owner_profile = getattr(project.owner, "hackclub_profile", None)
    owner_slack_id = owner_profile.slack_id if owner_profile else ""
    if owner_slack_id:
        send_slack_dm(f"Your project <https://atlantis.hackclub.com/projects/{project_id}|{project.title}> has been unlocked.", owner_slack_id)

    return safe_redirect_back(request)