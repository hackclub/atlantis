from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.conf import settings
from django.db import transaction

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

from ...models import (
    AirtableSubmission, InternalComment, Profile, Project, Ship, T1, T2, T3,
    PAYOUT_MULTIPLIER_DEFAULT, PAYOUT_MULTIPLIER_MAX, PAYOUT_MULTIPLIER_MIN,
    PAYOUT_MULTIPLIER_STEP, PEARLS_PER_HOUR,
)
from ...submissions import build_override_justification, submit_ship
from ..helpers import check_perms, send_slack_dm, send_slack_message, slack_mention, record_audit, get_model_info, layers_for_minutes, build_journal_timeline, reviewer_leaderboard, approved_minutes_for_journals, format_minutes, build_review_history, rate_limit, safe_redirect_back, timelapse_cleared_ships

INTERNAL_COMMENT_MAX_LENGTH = 1000
T1_FIELD_MAX_LENGTH = 1000
T2_FIELD_MAX_LENGTH = 1000

TIMELAPSE_PENDING_MESSAGE = (
    "That ship's timelapses haven't finished internal review yet. It'll appear "
    "in the queue once they have."
)

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
    if not feedback:
        return "They didn't leave any feedback."
    return f"Here's what they said about it: _{feedback}_"

def ping_review_checkpoint(ship, reviewer, tier, outcome, feedback):
    """
    Rejections are posted in the review checkpoint channel with the shipper and
    the reviewer both pinged, instead of DM'd, so the two can talk it over.
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
            f"created ({submission.error}). Check the table before resubmitting — "
            "this one will not retry on its own.",
        )
    else:
        messages.error(
            request,
            f"The project was finalized but its Airtable record was not created: "
            f"{submission.error} The submit_airtable command will retry it.",
        )

@staff_member_required
@check_perms(["atlantis_site.t1_review", "atlantis_site.t2_review", "atlantis_site.organizer", "atlantis_site.t3_review"])
def review_dash(request):
    ships = (
        timelapse_cleared_ships(Ship.objects.filter(status=Ship.ShipStatus.T1_QUEUE))
        .select_related("project", "project__owner", "project__owner__hackclub_profile")
        .order_by("-created_at")
    )
    for ship in ships:
        ship.time_spent_display = format_minutes(
            approved_minutes_for_journals(ship.project.journals.all())
        )
    return render(request, "root/review.html", {
        "ships": ships,
        "leaderboard": reviewer_leaderboard("t1_reviews"),
    })

@staff_member_required
@check_perms(["atlantis_site.t1_review", "atlantis_site.t2_review", "atlantis_site.organizer", "atlantis_site.t3_review"])
def review_project(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
    if not ship.timelapse_cleared:
        messages.error(request, TIMELAPSE_PENDING_MESSAGE)
        return redirect("review_dash")
    journals = ship.project.journals.order_by('-id')
    timeline = build_journal_timeline(journals, ship.project.ships.all())
    try:
        hasMake = bool(get_model_info(ship.project.printablesUrl.split('/model/')[1].split('-')[0])["makesCount"])
    except:
        hasMake = False

    return render(request, "root/review_project.html", {
        "ship": ship,
        "journals": journals,
        "timeline": timeline,
        "review_history": build_review_history(ship),
        "hasMake": hasMake,
    })

@require_POST
@staff_member_required
@check_perms(["atlantis_site.t1_review", "atlantis_site.t2_review", "atlantis_site.organizer", "atlantis_site.t3_review"])
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

        if approved:
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

    if approved:
        owner_slack_id = ship.project.owner.hackclub_profile.slack_id
        if owner_slack_id:
            send_slack_dm(f"Your project {project_link(ship.project)} has been T1 reviewed and approved! {feedback_line(feedback)}", owner_slack_id)
    else:
        ping_review_checkpoint(ship, reviewer, "T1", "rejected", feedback)

    record_audit(request, "t1_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t1_id": t1.id,
        "project": ship.project.title,
        "approved": approved,
        "new_ship_status": ship.status,
    })

    messages.success(request, f'Successfully reviewed project "{ship.project.title}" with approved = {approved}!')
    return redirect("review_dash")

@staff_member_required
@check_perms(["atlantis_site.t2_review", "atlantis_site.organizer", "atlantis_site.t3_review"])
def ysws_review_dash(request):
    ships = (
        Ship.objects.filter(status=Ship.ShipStatus.T2_QUEUE)
        .select_related("project", "project__owner", "project__owner__hackclub_profile")
        .order_by("-created_at")
    )
    for ship in ships:
        ship.time_spent_display = format_minutes(
            approved_minutes_for_journals(ship.project.journals.all())
        )
    return render(request, "root/ysws_review.html", {
        "ships": ships,
        "leaderboard": reviewer_leaderboard("t2_reviews"),
    })

@staff_member_required
@check_perms(["atlantis_site.t2_review", "atlantis_site.organizer", "atlantis_site.t3_review"])
def ysws_review_project(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
    journals = ship.project.journals.order_by('-id')
    timeline = build_journal_timeline(journals, ship.project.ships.all())
    # Ship-scoped, matching what t2_decision validates the deduction against —
    # the sidebar's pearl preview has to agree with the ceiling the POST
    # handler will enforce.
    logged_time = approved_minutes_for_journals(ship.journals.all())
    return render(request, "root/ysws_review_project.html", {
        "ship": ship,
        "journals": journals,
        "timeline": timeline,
        "review_history": build_review_history(ship),
        "logged_time": logged_time,
        "base_layers": layers_for_minutes(logged_time),
        "pearls_per_hour": PEARLS_PER_HOUR,
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
        journals = ship.journals.order_by("-id")

        total_time = approved_minutes_for_journals(journals)
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

    if decision == T2.Decision.APPROVE:
        owner_slack_id = ship.project.owner.hackclub_profile.slack_id
        if owner_slack_id:
            send_slack_dm(f"Your project {project_link(ship.project)} has been T2 reviewed and {message}! {feedback_line(feedback)}", owner_slack_id)
    else:
        ping_review_checkpoint(ship, reviewer, "T2", message, feedback)

    record_audit(request, "t2_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t2_id": t2.id,
        "project": ship.project.title,
        "decision": decision,
        "deductions": deductions,
        "new_ship_status": ship.status,
    })

    messages.success(request, f'Successfully reviewed project "{ship.project.title}" with decision {decision} and a deduction of {deductions} minutes!')
    return redirect("ysws_review_dash")

@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.t3_review"])
def fraud_review_dash(request):
    ships = (
        Ship.objects.filter(status=Ship.ShipStatus.T3_QUEUE)
        .select_related("project", "project__owner", "project__owner__hackclub_profile")
        .order_by("-created_at")
    )
    for ship in ships:
        ship.time_spent_display = format_minutes(
            approved_minutes_for_journals(ship.project.journals.all())
        )
    return render(request, "root/fraud_review.html", {
        "ships": ships,
        "leaderboard": reviewer_leaderboard("t3_reviews"),
    })

@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.t3_review"])
def fraud_review_project(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
    journals = ship.project.journals.order_by('-id')
    timeline = build_journal_timeline(journals, ship.project.ships.all())
    logged_time = approved_minutes_for_journals(ship.journals.all())

    latest_t2 = ship.t2_reviews.order_by('-id').first()
    deductions = latest_t2.deductions if latest_t2 else 0
    total_time = max(logged_time - deductions, 0)

    return render(request, "root/fraud_review_project.html", {
        "ship": ship,
        "journals": journals,
        "timeline": timeline,
        "review_history": build_review_history(ship),
        "logged_time": logged_time,
        "deductions": deductions,
        "total_time": total_time,
        "base_layers": layers_for_minutes(total_time),
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
        match decision:
            case T3.Decision.RETURN_T1:
                ship.status = Ship.ShipStatus.T1_QUEUE
                message = "returned to T1 reviewers"
            case T3.Decision.RETURN_T2:
                ship.status = Ship.ShipStatus.T2_QUEUE
                message = "returned to T2 reviewers"
            case T3.Decision.APPROVE:
                ship.status = Ship.ShipStatus.FINALIZED
                profile = Profile.objects.select_for_update().get(user=ship.project.owner)
                payout_layers = layers_for_minutes(payout_time, payout_multiplier)
                profile.layers += payout_layers
                profile.save(update_fields=["layers"])
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
        )

    # Outside the transaction on purpose: the ship is committed as finalized
    # before anything is sent to Airtable, so a submission that fails leaves a
    # finalized ship and a retryable row rather than rolling the finalization
    # back. submit_ship is safe to call again and refuses to send twice.
    submission = submit_ship(ship) if decision == T3.Decision.APPROVE else None

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
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
        "new_ship_status": ship.status,
        "airtable_status": submission.status if submission else "",
        "airtable_record_id": submission.record_id if submission else "",
        "airtable_error": submission.error if submission else "",
    })

    outcome = (
        f" — {payout_layers} pearls at a {payout_multiplier}x multiplier"
        if decision == T3.Decision.APPROVE else ""
    )
    messages.success(request, f"Sucessfully reviewed project '{ship.project.title}' with decision {decision}{outcome}")
    if submission:
        report_submission(request, submission)
    return redirect("fraud_review_dash")

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

    owner_slack_id = project.owner.hackclub_profile.slack_id
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

    owner_slack_id = project.owner.hackclub_profile.slack_id
    if owner_slack_id:
        send_slack_dm(f"Your project <https://atlantis.hackclub.com/projects/{project_id}|{project.title}> has been unlocked.", owner_slack_id)

    return safe_redirect_back(request)