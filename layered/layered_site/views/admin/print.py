from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from django.shortcuts import get_object_or_404

from ...models import Ship, Print
from ..helpers import check_perms, record_audit, send_slack_dm, is_valid_image_url

@staff_member_required
@check_perms(["layered_site.printer", "layered_site.organizer"])
def print_dash(request):
    user = request.user
    claimed_prints = (
        Print.objects.filter(
            printer=user,
            unclaimed_time__isnull=True,
            finished_time__isnull=True,
        )
        .select_related("ship", "ship__project")
        .order_by("-claimed_time")
    )
    queued_ships = Ship.objects.filter(status=Ship.ShipStatus.PRINT_QUEUE).select_related("project")
    return render(request, "root/print.html", {
        "claimed_prints": claimed_prints,
        "ships": queued_ships,
        "user": user
    })

@staff_member_required
@require_POST
@check_perms(["layered_site.printer", "layered_site.organizer"])
def claim_print(request, ship_id):
    user = request.user
    with transaction.atomic():
        ship = get_object_or_404(Ship.objects.select_for_update(), id=ship_id)

        if not ship.status == Ship.ShipStatus.PRINT_QUEUE:
            messages.error(request, "print not in print queue")
            return redirect("print_dash")
        if ship.prints.filter(unclaimed_time__isnull=True, finished_time__isnull=True).exists():
            messages.error(request, "already claimed")
            return redirect("print_dash")

        ship.status = Ship.ShipStatus.BEING_PRINTED
        ship.save()

        new_print = Print.objects.create(
            printer=user,
            ship=ship
        )

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    if owner_slack_id:
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> is being printed!", owner_slack_id)

    record_audit(request, "claim_print", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "print_id": new_print.id,
        "project": ship.project.title,
        "new_ship_status": ship.status,
    })

    messages.success(request, f"Claimed print '{ship.project.title}'.")
    return redirect("print_project", ship_id=ship_id)

@staff_member_required
@require_POST
@check_perms(["layered_site.printer", "layered_site.organizer"])
def unclaim_print(request, ship_id):
    user = request.user
    ship = get_object_or_404(Ship, id=ship_id)
    if not ship.status == Ship.ShipStatus.BEING_PRINTED:
        messages.error(request, "print is not being printed")
        return redirect("print_dash")

    active_print = ship.prints.filter(
        unclaimed_time__isnull=True,
        finished_time__isnull=True
    ).order_by("-id").first()

    if not active_print:
        messages.error(request, "no active print found")
        return redirect("print_dash")
    if not active_print.printer == user:
        messages.error(request, "this print isn't claimed by you!")
        return redirect("print_dash")

    active_print.unclaimed_time = timezone.now()
    active_print.decision = Print.Decision.UNCLAIMED
    active_print.save()

    ship.status = Ship.ShipStatus.PRINT_QUEUE
    ship.save()

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    if owner_slack_id:
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> is no longer being printed!", owner_slack_id)

    record_audit(request, "unclaim_print", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "print_id": active_print.id,
        "project": ship.project.title,
        "new_ship_status": ship.status,
    })

    messages.success(request, f"Unclaimed print '{ship.project.title}'")
    return redirect("print_dash")

@staff_member_required
@check_perms(["layered_site.printer", "layered_site.organizer"])
def print_project(request, ship_id):    
    ship = get_object_or_404(Ship, id=ship_id)
    journals = ship.project.journals.order_by('-id')
    if ship.prints.exists():
        current_print = ship.prints.all().order_by("-id").first()
    else:
        current_print = None

    return render(request, "root/print_project.html", {
        "current_print": current_print,
        "ship": ship,
        "journals": journals,
        "can_claim": ship.status == Ship.ShipStatus.PRINT_QUEUE,
    })

@staff_member_required
@require_POST
@check_perms(["layered_site.printer", "layered_site.organizer"])
def print_decision(request, ship_id):
    weight_raw = request.POST.get("weight", "0").strip()
    image_url = request.POST.get("image_url", "").strip()

    if not is_valid_image_url(image_url):
        messages.error(request, "Invalid image URL")
        return redirect("print_project", ship_id=ship_id)

    try:
        weight = int(weight_raw)
    except ValueError:
        messages.error(request, f"Weight must be a whole number, received {weight_raw})")
        return redirect("print_project", ship_id=ship_id)

    feedback = request.POST.get("feedback", "").strip()
    internal_notes = request.POST.get("internal_notes", "").strip()
    decision = request.POST.get("decision", "").strip()

    with transaction.atomic():
        ship = get_object_or_404(Ship.objects.select_for_update(), id=ship_id)

        if not ship.status == Ship.ShipStatus.BEING_PRINTED:
            messages.error(request, "print not being printed")
            return redirect("print_dash")

        active_print = ship.prints.filter(
            unclaimed_time__isnull=True,
            finished_time__isnull=True
        ).order_by("-id").first()

        if not active_print:
            messages.error(request, "no active print found")
            return redirect("print_project", ship_id=ship_id)

        active_print.finished_time = timezone.now()
        active_print.weight = weight
        active_print.internal_notes = internal_notes
        active_print.feedback = feedback
        active_print.decision = decision
        active_print.image_url = image_url
        active_print.save()

        match decision:
            case Print.Decision.RETURN_T1:
                ship.status = Ship.ShipStatus.T1_QUEUE
            case Print.Decision.APPROVE:
                ship.status = Ship.ShipStatus.T2_QUEUE
            case _:
                messages.error(request, f"Invalid decision (got: {decision})")
                return redirect("print_dash")

        ship.save()

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    if owner_slack_id:
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been printed and {"sent back to T1" if decision == Print.Decision.RETURN_T1 else "approved"}! Here's what they said about it: _{feedback}_", owner_slack_id)

    record_audit(request, "print_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "print_id": active_print.id,
        "project": ship.project.title,
        "decision": decision,
        "weight": weight,
        "new_ship_status": ship.status,
    })

    messages.success(
        request,
        f"Print {ship.project.title} with {decision}"
    )

    return redirect("print_dash")