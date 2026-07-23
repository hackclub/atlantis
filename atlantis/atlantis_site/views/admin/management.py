from django.shortcuts import render, redirect
from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q, Sum
from django.contrib import messages

import os

from ...models import Project
from ..helpers import check_perms, is_valid_image_url, record_audit, is_valid_printables_url, is_valid_editor_model_url

@staff_member_required
@check_perms(["atlantis_site.organizer"])
def users(request):
    user_model = get_user_model()
    users = user_model.objects.all().prefetch_related("groups").order_by("id")

    search_query = request.GET.get("q", "").strip()
    if search_query:
        users = users.filter(hackclub_profile__slack_username__icontains=search_query)

    default_pfp_url = os.environ["DEFAULT_PFP"]
    all_groups = Group.objects.all()

    return render(request, "root/users.html", {
        "users": users,
        "default_pfp_url": default_pfp_url,
        "all_groups": all_groups,
        "search_query": search_query,
    })

@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer"])
def edit_user(request, user_id):    
    user_model = get_user_model()
    targetUser = get_object_or_404(user_model, id=user_id)
    targetProfile = targetUser.hackclub_profile

    previous = {
        "username": targetUser.username,
        "email": targetUser.email,
        "first_name": targetUser.first_name,
        "last_name": targetUser.last_name,
        "slack_username": targetProfile.slack_username,
        "slack_id": targetProfile.slack_id,
        "slack_pfp_url": targetProfile.slack_pfp_url,
        "layers": targetProfile.layers,
        "groups": list(targetUser.groups.values_list("name", flat=True)),
    }

    new = {
        "username": request.POST.get("editSub"),
        "email": request.POST.get("editEmail"),
        "first_name": request.POST.get("editFirstName"),
        "last_name": request.POST.get("editLastName"),
        "slack_username": request.POST.get("editUsername"),
        "slack_id": request.POST.get("editSlackId"),
        "slack_pfp_url": request.POST.get("editSlackPfpUrl"),
        "layers": request.POST.get("editLayers"),
        "groups": request.POST.getlist("groups")
    }

    unrequired_items = ["slack_pfp_url", "groups"]
    for key, value in new.items():
        if not value and key not in unrequired_items:
            messages.error(request, f"{key.capitalize()} is required!")       

    targetUser.username = new["username"]
    targetUser.email = new["email"]
    targetUser.first_name = new["first_name"]
    targetUser.last_name = new["last_name"]
    targetProfile.slack_username = new["slack_username"]
    targetProfile.slack_id = new["slack_id"]

    new_layers_raw = new["layers"]
    try:
        new_layers = int(new_layers_raw)
        targetProfile.layers = new_layers
    except (ValueError, TypeError):
        pass

    new_pfp = new["slack_pfp_url"]
    targetProfile.slack_pfp_url = new_pfp if is_valid_image_url(new_pfp) else targetUser.hackclub_profile.slack_pfp_url

    new_groups = new["groups"]
    targetUser.groups.set(new_groups)
    targetUser.is_staff = targetUser.groups.exists()

    targetProfile.save()
    targetUser.save()

    record_audit(request, "edit_user", target=f"User #{targetUser.id} ({targetUser.hackclub_profile.slack_username})", metadata={
        "user_id": targetUser.id,
        "previous": previous,
        "new": new
    })

    return redirect("users")

@staff_member_required
@check_perms(["atlantis_site.organizer"])
def manage_projects(request):
    projects = Project.objects.select_related("owner", "owner__hackclub_profile").order_by("id")

    search_query = request.GET.get("q", "").strip()
    if search_query:
        projects = projects.filter(
            Q(title__icontains=search_query)
            | Q(owner__hackclub_profile__slack_username__icontains=search_query)
        )

    for project in projects:
        total_time = project.journals.aggregate(total=Sum("time_spent"))["total"] or 0
        project.time_spent_display = f"{total_time // 60}h {total_time % 60}m"
        project.journal_count = project.journals.count()
        latest_ship = project.ships.order_by("-created_at").first()
        project.status_display = latest_ship.get_status_display() if latest_ship else "No ships yet"

    default_pfp_url = os.environ["DEFAULT_PFP"]

    return render(request, "root/manage_projects.html", {
        "projects": projects,
        "default_pfp_url": default_pfp_url,
        "search_query": search_query,
    })

@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer"])
def admin_edit_project(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    previous = {
        "title": project.title,
        "description": project.description,
        "printablesUrl": project.printablesUrl,
        "editor_model_url": project.editor_model_url,
        "deleted": project.deleted,
    }

    title = request.POST.get("editTitle", "").strip()
    description = request.POST.get("editDescription", "").strip()
    printablesUrl = request.POST.get("editPrintablesUrl", "").strip()
    editor_model_url = request.POST.get("editEditorModelUrl", "").strip()
    deleted = request.POST.get("editDeleted") == "1"

    if len(title) > 60:
        messages.error(request, "Title too long (max 60 chars)")
        return redirect("manage_projects")
    
    if len(description) > 1000:
        messages.error(request, "Description too long (max 1000 chars)")
        return redirect("manage_projects")
    
    if not is_valid_printables_url(printablesUrl) and printablesUrl:
        messages.error(request, "Invalid printables URL")
        return redirect("manage_projects")
    
    if not is_valid_editor_model_url(editor_model_url) and editor_model_url:
        messages.error(request, "Invalid editor model URL")
        return redirect("manage_projects")
    
    project.title = title
    project.description = description
    project.printablesUrl = printablesUrl
    project.editor_model_url = editor_model_url
    project.deleted = deleted

    project.save()

    record_audit(request, "edit_project", target=f"Project #{project.id} ({project.title})", metadata={
        "project_id": project.id,
        "previous": previous,
        "new": {
            "title": project.title,
            "description": project.description,
            "printablesUrl": project.printablesUrl,
            "editor_model_url": project.editor_model_url,
            "deleted": project.deleted,
        },
    })

    return redirect("manage_projects")

@staff_member_required
@check_perms(["atlantis_site.organizer"])
@require_POST
def db_delete_project(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    title = project.title

    journals = project.journals.all()
    ships = project.ships.all()

    try:
        for journal in journals:
            journal.delete()

        for ship in ships:
            ship.delete()

        project.delete()
    except Exception as e:
        messages.error(request, f"DB delete failed, {e}")
        return redirect("manage_projects")
    
    messages.success(request, f"Removed {title} from the DB")
    return redirect("manage_projects")