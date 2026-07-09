from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum
from django.core.files.storage import default_storage
from django.db import transaction
from django.conf import settings
from django.core.exceptions import PermissionDenied
from math import floor

from ...models import (
    Project, Ship, Print, Journal, ALLOWED_EDITORS, EDITOR_FILE_EXTENSIONS, detect_editor_from_filename, detect_editor_from_link
)
from ..helpers import is_valid_printables_url, get_model_info

import os

@login_required
def projects(request):
    projects = request.user.projects.filter(deleted=False).order_by("id")
    profile = request.user.hackclub_profile
    return render(request, "layered_site/projects.html", {"projects": projects, "profile": profile})

@login_required
@require_POST
def create_project(request):
    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    printables_url = request.POST.get("printables_url", "").strip()
    locked = False

    if not title:
        messages.error(request, "Title is required.")
        return redirect("projects")
    
    if not description:
        messages.error(request, "Description is required")
        return redirect("projects")

    project = Project.objects.create(
        owner = request.user,
        title = title,
        description = description,
        printablesUrl = printables_url,
        locked = locked
    )

    return redirect("projects")


@login_required
@require_POST
def edit_project(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id, deleted=False)

    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    printables_url = request.POST.get("printables_url", "").strip()

    if not title:
        messages.error(request, "Title is required.")
        return redirect("projects")
    
    if not description:
        messages.error(request, "Description is required")
        return redirect("projects")

    project.title = title
    project.description = description
    project.printablesUrl = printables_url
    project.save()

    return redirect("projects")


@login_required
@require_POST
def update_editor_model(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id, deleted=False)

    if project.locked:
        messages.error(request, "You cannot edit a locked project.")
        return redirect("project_detail", project_id=project_id)

    editor_model_file = request.FILES.get("editor_model_file")
    editor_model_link = request.POST.get("editor_model_link", "").strip()

    if editor_model_file:
        if not detect_editor_from_filename(editor_model_file.name):
            messages.error(request, f"Unsupported editor model file. Supported editors: {', '.join(ALLOWED_EDITORS)}.")
            return redirect("project_detail", project_id=project_id)
        editor_model_key = default_storage.save(
            f"editor_models/{os.path.basename(editor_model_file.name)}", editor_model_file
        )
        project.editor_model_url = default_storage.url(editor_model_key)
    elif editor_model_link:
        if not editor_model_link.lower().startswith(("http://", "https://")):
            messages.error(request, "Editor model link must be a valid URL.")
            return redirect("project_detail", project_id=project_id)
        if not detect_editor_from_link(editor_model_link):
            messages.error(request, f"Unsupported editor model link. Supported editors: {', '.join(ALLOWED_EDITORS)}.")
            return redirect("project_detail", project_id=project_id)
        project.editor_model_url = editor_model_link
    else:
        messages.error(request, "Upload a file or provide a link for the editor model.")
        return redirect("project_detail", project_id=project_id)

    project.save()
    messages.success(request, "Editor model updated successfully.")
    return redirect("project_detail", project_id=project_id)


@login_required
@require_POST
def delete_project(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id)

    project.deleted = True
    project.save()

    return redirect("projects")

@login_required
def project_detail(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id, deleted=False)
    user = request.user
    profile = request.user.hackclub_profile
    ships = project.ships.order_by('-created_at')
    journals = project.journals.order_by('-id')

    total_time = journals.aggregate(total=Sum('time_spent'))['total'] or 0
    time_spent = f"{floor(total_time / 60)}h {total_time % 60}m"

    latest_ship = ships.first()
    ship_pending = latest_ship is not None and latest_ship.status not in (Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED)

    if project.locked:
        can_ship = False
        ship_disabled_reason = "This project is locked and cannot be shipped."
    elif not is_valid_printables_url(project.printablesUrl):
        can_ship = False
        ship_disabled_reason = "You need a valid Printables URL before you can ship."
    elif not project.editor_model_url:
        can_ship = False
        ship_disabled_reason = "You need to upload or link your editor model before you can ship."
    elif ship_pending:
        can_ship = False
        ship_disabled_reason = "Your most recent ship must be finalized or rejected before you can reship."
    elif not project.journals.exists():
        can_ship = False
        ship_disabled_reason = "You must have at least one journal entry before you can ship."
    else:
        can_ship = True
        ship_disabled_reason = ""
    
    if project.printablesUrl:
        try:
            printablesData = get_model_info(project.printablesUrl.split('/model/')[1].split('-')[0])
        except:
            printablesData = {"makesCount": 0}
    else:
        printablesData = {"makesCount": 0}
    
    def get_latest_feedback(ship):
        candidates = []
        t1 = ship.t1_reviews.order_by('-reviewed_at').first()
        if t1 and t1.feedback:
            candidates.append((t1.reviewed_at, t1.feedback))
        t2 = ship.t2_reviews.order_by('-reviewed_at').first()
        if t2 and t2.feedback:
            candidates.append((t2.reviewed_at, t2.feedback))
        pr = ship.prints.exclude(decision=Print.Decision.PRINTING).order_by('-finished_time').first()
        if pr and pr.feedback and pr.finished_time:
            candidates.append((pr.finished_time, pr.feedback))
        return max(candidates, key=lambda x: x[0])[1] if candidates else ""

    ships_with_feedback = [(ship, get_latest_feedback(ship)) for ship in ships]

    return render(request, "layered_site/project_detail.html", {
        "project": project,
        "user": user,
        "profile": profile,
        "ships": ships,
        "ships_with_feedback": ships_with_feedback,
        "journals": journals,
        "time_spent": time_spent,
        "can_ship": can_ship,
        "ship_disabled_reason": ship_disabled_reason,
        "printablesData": printablesData,
        "allowed_editors": ALLOWED_EDITORS,
        "allowed_editor_extensions": ",".join(EDITOR_FILE_EXTENSIONS.keys()),
    })

@login_required
def explore(request):
    profile = request.user.hackclub_profile

    projects_unlocked = Project.objects.filter(deleted=False).exclude(locked=True)
    projects = projects_unlocked.exclude(owner=request.user)

    return render(request, "layered_site/explore.html", {'profile': profile, 'projects': projects})

@login_required
def project_detail_explore(request, project_id):
    project = get_object_or_404(Project, id=project_id, deleted=False)
    if project.locked and not request.user.has_perm("layered_site.organizer"):
        raise PermissionDenied

    user = request.user
    profile = user.hackclub_profile
    ships = project.ships.order_by("-created_at")
    journals = project.journals.order_by("-id")

    total_time = journals.aggregate(total=Sum('time_spent'))['total'] or 0
    time_spent = f"{floor(total_time / 60)}h {total_time % 60}m"

    if project.printablesUrl:
        try:
            printablesData = get_model_info(project.printablesUrl.split('/model/')[1].split('-')[0])
        except:
            printablesData = {"makesCount": 0}
    else:
        printablesData = {"makesCount": 0}

    return render(request, "layered_site/project_detail_explore.html", {
        "project": project,
        "user": user,
        "profile": profile,
        "ships": ships,
        "journals": journals,
        "time_spent": time_spent,
        "printablesData": printablesData,
    })

@login_required
def create_journal(request, project_id):
    if request.method != 'POST':
        return redirect("project_detail", project_id=project_id)
    
    if not settings.ALLOW_JOURNALING and not request.user.has_perm("layered_site.organizer"):
        messages.error(request, "Journaling is disallowed on this instance!")
        return redirect("project_detail", project_id=project_id)

    project = get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)

    if project.locked:
        messages.error(request, "You cannot create a journal on a locked project.")
        return redirect("projects")
    time_spent_raw = request.POST.get("time_spent", "0").strip()
    try:
        time_spent = int(time_spent_raw)

        if time_spent > 240:
            messages.error(request, "Time spent must not be greater than 4 hours!")
            return redirect("project_detail", project_id=project_id)
        if time_spent <= 30:
            messages.error(request, "You must spend at least 30 minutes on your journal entry!")
            return redirect("project_detail", project_id=project_id)
    except ValueError:
        messages.error(request, "Journal time spent must be an integer!")
        return redirect("project_detail", project_id=project_id)
    
    title = request.POST.get("title", "").strip()
    text = request.POST.get("text", "").strip()

    if len(text) <= 200:
        messages.error(request, "Journals must have at least 200 characters!")
        return redirect("project_detail", project_id=project_id)
    elif len(text) >= 2000:
        messages.error(request, "Journals must not be greater than 2000 characters!")
        return redirect("project_detail", project_id=project_id)

    image_file = request.FILES.get("image")
    model_file = request.FILES.get("STL")

    if not image_file:
        messages.error(request, "An image is required.")
        return redirect("project_detail", project_id=project_id)
    if not model_file:
        messages.error(request, "An STL model is required.")
        return redirect("project_detail", project_id=project_id)

    if not (image_file.content_type or "").startswith("image/"):
        messages.error(request, "Uploaded image must be an image file.")
        return redirect("project_detail", project_id=project_id)
    if not os.path.basename(model_file.name).lower().endswith(".stl"):
        messages.error(request, "Uploaded model must be an STL file.")
        return redirect("project_detail", project_id=project_id)

    image_key = default_storage.save(f"images/{os.path.basename(image_file.name)}", image_file)
    model_key = default_storage.save(f"models/{os.path.basename(model_file.name)}", model_file)

    image_url = default_storage.url(image_key)
    model_url = default_storage.url(model_key)

    Journal.objects.create(
        project=project,
        time_spent=time_spent,
        title=title,
        text=text,
        image_url=image_url,
        model_url=model_url
    )

    messages.success(request, "Journal entry created successfully")
    return redirect("project_detail", project_id=project_id)
    
@login_required
def ship_project(request, project_id):
    # remember to check if the weight is greater than the time spent x 100
    if request.method != 'POST':
        return redirect("project_detail", project_id=project_id)
    
    project = get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)
    if project.locked:
        messages.error(request, "This project is locked. You cannot ship a locked project.")
        return redirect("projects")
    if not is_valid_printables_url(project.printablesUrl):
        messages.error(request, "you need a printables URL to ship!")
        return redirect("projects")
    if not project.editor_model_url:
        messages.error(request, "you need to upload or link your editor model before you can ship!")
        return redirect("projects")
    if not project.description:
        messages.error(request, "your project must have a description before you can ship!")
        return redirect("projects")
    unassigned_journals = project.journals.filter(ship__isnull=True)
    if not unassigned_journals.exists():
        messages.error(request, "your project must have at least one journal to be shipped")
        return redirect("projects")
    if (unassigned_journals.aggregate(total=Sum('time_spent'))['total'] or 0) <= 180:
        messages.error(request, "you must have atleast 3 hours of logged time before you can ship!")
        return redirect("projects")

    latest_ship = project.ships.order_by('-created_at').first()
    if latest_ship and latest_ship.status not in (Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED):
        messages.error(request, "You cannot reship until your most recent ship has been finalized or rejected.")
        return redirect("project_detail", project_id=project_id)

    if latest_ship:
        journals = project.journals.filter(ship__isnull=True)
        if (journals.aggregate(total=Sum('time_spent'))['total'] or 0) <= 120:
            messages.error(request, "Can't ship again without at least 2 hours of work!")
            return redirect("projects")

    with transaction.atomic():
        ship = Ship.objects.create(
            project = project,
            status = Ship.ShipStatus.T1_QUEUE
        )
        project.journals.filter(ship__isnull=True).update(ship=ship)

    messages.success(request, f'Successfully shipped project "{project.title}"!')
    return redirect("projects")