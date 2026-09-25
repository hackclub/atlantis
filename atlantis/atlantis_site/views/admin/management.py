from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.db import transaction
from django.contrib import messages

import os

from ... import airtable
from ...models import Profile, Project
from ..helpers import check_perms, is_valid_image_url, record_audit, is_valid_printables_url, is_valid_editor_model_url, tracked_minutes_for_journals, format_minutes, INT_FIELD_MAX, INT_FIELD_MIN, field_max_length, too_long

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
def backfill_emails(request):
    if not airtable.emails_configured():
        missing = ", ".join(airtable.missing_settings(airtable.EMAILS_REQUIRED_SETTINGS))
        messages.error(request, f"Airtable Emails table is not configured (missing {missing}).")
        return redirect("users")

    # One row per address: the upsert is keyed on Email, and Airtable refuses a
    # batch in which two records would land on the same row.
    people = {}
    for user in get_user_model().objects.order_by("id"):
        contact = airtable.email_contact(user)
        if contact:
            people.setdefault(contact[1].lower(), contact)

    airtable.upsert_emails_in_background(list(people.values()), "backfill")

    record_audit(request, "backfill_emails", target="Airtable Emails", metadata={
        "count": len(people),
    })
    messages.success(request, f"Sending {len(people)} user(s) to the Airtable Emails table in the background.")
    return redirect("users")

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
        "username": (request.POST.get("editSub") or "").strip(),
        "email": (request.POST.get("editEmail") or "").strip(),
        "first_name": (request.POST.get("editFirstName") or "").strip(),
        "last_name": (request.POST.get("editLastName") or "").strip(),
        "slack_username": (request.POST.get("editUsername") or "").strip(),
        "slack_id": (request.POST.get("editSlackId") or "").strip(),
        "slack_pfp_url": (request.POST.get("editSlackPfpUrl") or "").strip(),
        "layers": (request.POST.get("editLayers") or "").strip(),
        "groups": request.POST.getlist("groups")
    }

    # Every one of these is a required field landing in a NOT NULL column, so
    # the complaint has to end the request. Reporting it and writing the row
    # anyway put a None into `username` and answered the form with a 500.
    unrequired_items = ["slack_pfp_url", "groups"]
    for key, value in new.items():
        if not value and key not in unrequired_items:
            messages.error(request, f"{key.capitalize()} is required!")
            return redirect("users")

    # Widths, for the same reason: Postgres answers an over-long value with a
    # DataError, and the reviewer gets a 500 instead of the field to shorten.
    for key, model, field in (
        ("username", user_model, "username"),
        ("email", user_model, "email"),
        ("first_name", user_model, "first_name"),
        ("last_name", user_model, "last_name"),
        ("slack_username", Profile, "slack_username"),
        ("slack_id", Profile, "slack_id"),
    ):
        if too_long(new[key], model, field):
            messages.error(request, f"{key.capitalize()} too long (max {field_max_length(model, field)} chars)!")
            return redirect("users")

    # `username` is unique, and a collision is an IntegrityError — a 500 on
    # what is really just a name somebody else already has.
    if user_model.objects.filter(username=new["username"]).exclude(id=targetUser.id).exists():
        messages.error(request, f'Another user already has the username "{new["username"]}".')
        return redirect("users")

    try:
        new_layers = int(new["layers"])
    except (ValueError, TypeError):
        messages.error(request, "Layers must be a whole number!")
        return redirect("users")

    if not INT_FIELD_MIN <= new_layers <= INT_FIELD_MAX:
        messages.error(request, f"Layers must be between {INT_FIELD_MIN} and {INT_FIELD_MAX}.")
        return redirect("users")

    targetUser.username = new["username"]
    targetUser.email = new["email"]
    targetUser.first_name = new["first_name"]
    targetUser.last_name = new["last_name"]
    targetProfile.slack_username = new["slack_username"]
    targetProfile.slack_id = new["slack_id"]
    targetProfile.layers = new_layers

    new_pfp = new["slack_pfp_url"]
    keep_pfp = (
        too_long(new_pfp, Profile, "slack_pfp_url")
        or not is_valid_image_url(new_pfp)
    )
    targetProfile.slack_pfp_url = targetProfile.slack_pfp_url if keep_pfp else new_pfp

    # Resolved to rows rather than handed to set() as raw strings: a posted id
    # that isn't a number is a ValueError and one that names no group is an
    # IntegrityError on the through table, and both are 500s.
    new_groups = list(Group.objects.filter(id__in=[
        gid for gid in new["groups"] if gid.isdigit()
    ]))
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
        project.time_spent_display = format_minutes(
            tracked_minutes_for_journals(project.journals.all())
        )
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

    # Both URL validators vouch for strings wider than the columns that hold
    # them, so the overflow would land as a DataError rather than a message.
    for label, value, field in (
        ("Printables URL", printablesUrl, "printablesUrl"),
        ("Editor model URL", editor_model_url, "editor_model_url"),
    ):
        if too_long(value, Project, field):
            messages.error(request, f"{label} too long (max {field_max_length(Project, field)} chars)")
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

def _purge_project(project):
    """Hard-delete a project with its journals and ships. Returns audit metadata."""
    metadata = {
        "project_id": project.id,
        "title": project.title,
        "owner_id": project.owner_id,
        "owner_username": project.owner.username,
        "journals_deleted": project.journals.count(),
        "ships_deleted": project.ships.count(),
    }

    with transaction.atomic():
        for journal in project.journals.all():
            journal.delete()

        for ship in project.ships.all():
            ship.delete()

        project.delete()

    return metadata

@staff_member_required
@check_perms(["atlantis_site.organizer"])
@require_POST
def db_delete_project(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    title = project.title

    try:
        metadata = _purge_project(project)
    except Exception as e:
        messages.error(request, f"DB delete failed, {e}")
        return redirect("manage_projects")

    record_audit(request, "db_delete_project", target=f"Project #{project_id} ({title})", metadata={
        **metadata,
        "bulk": False,
    })

    messages.success(request, f"Removed {title} from the DB")
    return redirect("manage_projects")

@staff_member_required
@check_perms(["atlantis_site.organizer"])
@require_POST
def db_delete_projects(request):
    project_ids = []
    for raw_id in request.POST.getlist("project_ids"):
        try:
            project_ids.append(int(raw_id))
        except (ValueError, TypeError):
            continue

    if not project_ids:
        messages.error(request, "No projects selected")
        return redirect("manage_projects")

    projects = Project.objects.filter(id__in=project_ids)
    if not projects:
        messages.error(request, "None of the selected projects exist")
        return redirect("manage_projects")

    deleted = 0
    for project in projects:
        title = project.title

        try:
            metadata = _purge_project(project)
        except Exception as e:
            messages.error(request, f"DB delete failed for {title}, {e}")
            continue

        # One entry per project, same action as a single delete, so the audit log
        # stays filterable by project regardless of how the delete was triggered.
        record_audit(request, "db_delete_project", target=f"Project #{metadata['project_id']} ({title})", metadata={
            **metadata,
            "bulk": True,
            "batch_ids": project_ids,
        })
        deleted += 1

    if deleted:
        messages.success(request, f"Removed {deleted} project(s) from the DB")

    return redirect("manage_projects")