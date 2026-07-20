from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum

from ...models import Profile, Project, Ship, T1, T2, T3
from ..helpers import check_perms, send_slack_dm, record_audit, get_model_info, layers_for_minutes, build_journal_timeline, reviewer_leaderboard

@staff_member_required
@check_perms(["layered_site.t1_review", "layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"])
def review_dash(request):
    ships = (
        Ship.objects.filter(status=Ship.ShipStatus.T1_QUEUE)
        .select_related("project", "project__owner", "project__owner__hackclub_profile")
        .order_by("-created_at")
    )
    for ship in ships:
        total_time = ship.project.journals.aggregate(total=Sum("time_spent"))["total"] or 0
        ship.time_spent_display = f"{total_time // 60}h {total_time % 60}m"
    return render(request, "root/review.html", {
        "ships": ships,
        "leaderboard": reviewer_leaderboard("t1_reviews"),
    })

@staff_member_required
@check_perms(["layered_site.t1_review", "layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"])
def review_project(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
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
        "hasMake": hasMake,
    })

@require_POST
@staff_member_required
@check_perms(["layered_site.t1_review", "layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"])
def t1_decision(request, ship_id): 
    reviewer = request.user
    feedback = request.POST.get("feedback", "").strip()
    internal_notes = request.POST.get("internal_notes", "").strip()

    if len(feedback) > 100 or len(internal_notes) > 100:
        messages.error(request, "Feedback or internal notes too long (max 100 char)")
        return redirect("review_project", ship_id=ship_id)

    print_requested = "print" in request.POST
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

        if approved:
            ship.status = Ship.ShipStatus.PRINT_QUEUE if print_requested else Ship.ShipStatus.T2_QUEUE
        else:
            ship.status = Ship.ShipStatus.REJECTED

        ship.save()

        t1 = T1.objects.create(
            reviewer=reviewer,
            ship=ship,
            feedback=feedback,
            internal_notes=internal_notes,
            print=print_requested,
            approved=approved
        )

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    if owner_slack_id:
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been T1 reviewed and {"approved" if approved else "rejected"}! Here's what they said about it: _{feedback}_", owner_slack_id)

    record_audit(request, "t1_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t1_id": t1.id,
        "project": ship.project.title,
        "approved": approved,
        "print_requested": print_requested,
        "new_ship_status": ship.status,
    })

    messages.success(request, f'Successfully reviewed project "{ship.project.title}" with approved = {approved} and print = {print_requested}!')
    return redirect("review_dash")

@staff_member_required
@check_perms(["layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"])
def ysws_review_dash(request):
    ships = (
        Ship.objects.filter(status=Ship.ShipStatus.T2_QUEUE)
        .select_related("project", "project__owner", "project__owner__hackclub_profile")
        .order_by("-created_at")
    )
    for ship in ships:
        total_time = ship.project.journals.aggregate(total=Sum("time_spent"))["total"] or 0
        ship.time_spent_display = f"{total_time // 60}h {total_time % 60}m"
    return render(request, "root/ysws_review.html", {
        "ships": ships,
        "leaderboard": reviewer_leaderboard("t2_reviews"),
    })

@staff_member_required
@check_perms(["layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"])
def ysws_review_project(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
    journals = ship.project.journals.order_by('-id')
    timeline = build_journal_timeline(journals, ship.project.ships.all())
    return render(request, "root/ysws_review_project.html", {
        "ship": ship,
        "journals": journals,
        "timeline": timeline,
    })

@require_POST
@staff_member_required
@check_perms(["layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"])
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

    if len(feedback) > 100 or len(justification) > 400:
        messages.error(request, "Feedback or justification length too long (feedback max 100, justification max 400)")
        return redirect("ysws_review_dash")

    with transaction.atomic():
        ship = get_object_or_404(Ship.objects.select_for_update(), id=ship_id)
        journals = ship.journals.order_by("-id")

        total_time = journals.aggregate(total=Sum('time_spent'))['total'] or 0
        if total_time <= deductions:
            messages.error(request, f"Deduction too large. (total_time: {total_time}, deductions: {deductions})")
            return redirect("ysws_review_dash")

        if not ship.status == Ship.ShipStatus.T2_QUEUE:
            messages.error(request, "ship not in T2 queue")
            return redirect("ysws_review_dash")

        match decision:
            case T2.Decision.APPROVE:
                ship.status = Ship.ShipStatus.T3_QUEUE
                message = "approved"
            case T2.Decision.RETURN_PRINT:
                ship.status = Ship.ShipStatus.PRINT_QUEUE
                message = "returned to the printers"
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

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been T2 reviewed and {message}! Here's what they said about it: _{feedback}_", owner_slack_id)

    record_audit(request, "t2_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t2_id": t2.id,
        "project": ship.project.title,
        "decision": decision,
        "deductions": deductions,
        "new_ship_status": ship.status,
    })

    messages.success(request, f'Successfully reviewed project "{ship.project.title}" with decision {decision} and deduction of {deductions} minutes!')
    return redirect("ysws_review_dash")

@staff_member_required
@check_perms(["layered_site.organizer", "layered_site.t3_review"])
def fraud_review_dash(request):
    ships = (
        Ship.objects.filter(status=Ship.ShipStatus.T3_QUEUE)
        .select_related("project", "project__owner", "project__owner__hackclub_profile")
        .order_by("-created_at")
    )
    for ship in ships:
        total_time = ship.project.journals.aggregate(total=Sum("time_spent"))["total"] or 0
        ship.time_spent_display = f"{total_time // 60}h {total_time % 60}m"
    return render(request, "root/fraud_review.html", {
        "ships": ships,
        "leaderboard": reviewer_leaderboard("t3_reviews"),
    })

@staff_member_required
@check_perms(["layered_site.organizer", "layered_site.t3_review"])
def fraud_review_project(request, ship_id):
    ship = get_object_or_404(Ship, id=ship_id)
    journals = ship.project.journals.order_by('-id')
    timeline = build_journal_timeline(journals, ship.project.ships.all())
    logged_time = ship.journals.aggregate(total=Sum('time_spent'))['total'] or 0

    latest_t2 = ship.t2_reviews.order_by('-id').first()
    deductions = latest_t2.deductions if latest_t2 else 0
    total_time = max(logged_time - deductions, 0)

    return render(request, "root/fraud_review_project.html", {
        "ship": ship,
        "journals": journals,
        "timeline": timeline,
        "logged_time": logged_time,
        "deductions": deductions,
        "total_time": total_time
    })

@require_POST
@staff_member_required
@check_perms(["layered_site.organizer", "layered_site.t3_review"])
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
            case T3.Decision.RETURN_PRINT:
                ship.status = Ship.ShipStatus.PRINT_QUEUE
                message = "returned to printers"
            case T3.Decision.APPROVE:
                ship.status = Ship.ShipStatus.FINALIZED
                profile = Profile.objects.select_for_update().get(user=ship.project.owner)
                payout_layers = layers_for_minutes(payout_time)
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
            airtable_time=airtable_time
        )

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been finalized and you've received {payout_layers} layers for it!", owner_slack_id) if decision == T3.Decision.APPROVE else send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been {message}!", owner_slack_id)

    record_audit(request, "t3_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t3_id": t3.id,
        "project": ship.project.title,
        "decision": decision,
        "payout_time": payout_time,
        "airtable_time": airtable_time,
        "payout_layers": payout_layers,
        "new_ship_status": ship.status,
    })

    messages.success(request, f"Sucessfully reviewed project '{ship.project.title}' with decision {decision}")
    return redirect("fraud_review_dash")

@staff_member_required
@require_POST
@check_perms(["layered_site.organizer", "layered_site.t2_review", "layered_site.t3_review"])
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
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{project_id}|{project.title}> has been locked.", owner_slack_id)

    return redirect(request.META.get("HTTP_REFERER", "/"))

@staff_member_required
@require_POST
@check_perms(["layered_site.organizer", "layered_site.t2_review", "layered_site.t3_review"])
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
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{project_id}|{project.title}> has been unlocked.", owner_slack_id)

    return redirect(request.META.get("HTTP_REFERER", "/"))