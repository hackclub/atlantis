from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Sum
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone
from django.http import FileResponse, Http404

from botocore.exceptions import ClientError

from datetime import timedelta

import mimetypes

from ...models import (
    Project, Ship, Journal, LookoutSession, ALLOWED_EDITORS, EDITOR_FILE_EXTENSIONS, detect_editor_from_filename, detect_editor_from_link
)
from ... import lookout
from .timelapse import _apply_session_payload
from ..helpers import (
    is_valid_printables_url, get_model_info, validate_file_size,
    sniff_image_extension, random_storage_key,
    notify_followers, rate_limit, tracked_minutes_for_journals, format_minutes,
    can_bypass_ship_requirements,
)

import os

def _attachable_timelapses(project, user, ids=None):
    """Finished Lookouts the user can still attach to a new lapse."""
    qs = LookoutSession.objects.filter(
        project=project,
        owner=user,
        status=LookoutSession.Status.COMPLETE,
        journal__isnull=True,
    )
    if ids is not None:
        qs = qs.filter(id__in=ids)
    return qs


# Our copy of a Lookout only ever got refreshed by the recorder page, so
# recording and then closing the tab left the session stuck mid-flight with its
# time unattachable. The project page now asks Lookout itself, throttled so a
# reload storm can't hammer the API.
LOOKOUT_REFRESH_AFTER = timedelta(seconds=20)
LOOKOUT_REFRESH_LIMIT = 3


def _refresh_lookouts(sessions):
    """Bring our copy of any unfinished Lookout up to date, in place."""
    stale_before = timezone.now() - LOOKOUT_REFRESH_AFTER
    asked = 0
    for session in sessions:
        if session.is_complete or session.status == LookoutSession.Status.FAILED:
            continue
        if session.updated_at > stale_before or asked >= LOOKOUT_REFRESH_LIMIT:
            continue
        asked += 1
        try:
            data = lookout.get_internal_session(session.session_id)
        except lookout.LookoutError:
            # Lookout being unreachable must not take the project page down with
            # it, and if one call failed the rest will too — each costs a
            # 10 second timeout, so stop asking.
            break
        _apply_session_payload(
            session,
            data.get("session"),
            data.get("trackedSeconds"),
            data.get("screenshotCount"),
        )


# The project page is an open book, so its content is dealt out into pages
# rather than scrolled. Page 0 is the project itself (the left page of the
# first spread) and every page after it is a leaf of lapses.
LAPSES_PER_PAGE = 3


def _book_pages(journals, allow_new):
    """Lay the lapses out as book pages.

    Returns the page list the template renders: the project page, then the
    lapses three to a page, oldest first. Writing a new one is the last entry
    in that run, so it sits in the space the next lapse will fill and moves
    down the book as the log grows.
    """
    pages = [{"kind": "project"}]

    entries = [{"type": "lapse", "journal": journal} for journal in journals]
    if allow_new:
        entries.append({"type": "compose"})
    chunks = [
        entries[i:i + LAPSES_PER_PAGE]
        for i in range(0, len(entries), LAPSES_PER_PAGE)
    ] or [[]]
    pages += [{"kind": "log", "entries": chunk} for chunk in chunks]

    # Pages are dealt two to a spread, so the book always needs an even count.
    if len(pages) % 2:
        pages.append({"kind": "blank"})

    return pages

@login_required
def projects(request):
    projects = list(request.user.projects.filter(deleted=False).order_by("id"))
    profile = request.user.hackclub_profile

    # Every book cover shows tracked time, so total it for all of them in one
    # query rather than one per book.
    tracked_seconds = dict(
        LookoutSession.objects.filter(journal__project__in=projects)
        .order_by()  # Meta.ordering would otherwise land in the GROUP BY
        .values_list("journal__project")
        .annotate(total=Sum("tracked_seconds"))
    )

    for project in projects:
        project.tracked_hours = f"{tracked_seconds.get(project.id, 0) / 3600:.1f}h"

    return render(request, "atlantis_site/projects.html", {"projects": projects, "profile": profile})

@login_required
@require_POST
@rate_limit("create_project", 2)
def create_project(request):
    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    printables_url = request.POST.get("printables_url", "").strip()
    locked = False

    if not title:
        messages.error(request, "Title is required.")
        return redirect("projects")
    
    if len(title) > 60:
        messages.error(request, "Title too long (max 60 chars)")
        return redirect("projects")
    
    if not description:
        messages.error(request, "Description is required")
        return redirect("projects")
    
    if len(description) > 1000:
        messages.error(request, "Description too long (max 1000 chars)")
        return redirect("projects")

    if printables_url and not is_valid_printables_url(printables_url):
        messages.error(request, "Printables URL must be a valid printables.com link.")
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
@rate_limit("edit_project", 2)
def edit_project(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id, deleted=False)

    if project.locked:
        messages.error(request, "You cannot edit a locked project.")
        return redirect("projects")

    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    printables_url = request.POST.get("printables_url", "").strip()

    if not title:
        messages.error(request, "Title is required.")
        return redirect("projects")
    
    if len(title) > 60:
        messages.error(request, "Title too long (max 60 chars)")
        return redirect("projects")
    
    if not description:
        messages.error(request, "Description is required")
        return redirect("projects")
    
    if len(description) > 1000:
        messages.error(request, "Description too long (max 1000 chars)")
        return redirect("projects")

    if printables_url and not is_valid_printables_url(printables_url):
        messages.error(request, "Printables URL must be a valid printables.com link.")
        return redirect("projects")

    project.title = title
    project.description = description
    project.printablesUrl = printables_url
    project.save()

    return redirect("projects")


@login_required
@require_POST
@rate_limit("update_editor_model", 3)
def update_editor_model(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id, deleted=False)

    if project.locked:
        messages.error(request, "You cannot edit a locked project.")
        return redirect("project_detail", project_id=project_id)

    editor_model_file = request.FILES.get("editor_model_file")
    editor_model_link = request.POST.get("editor_model_link", "").strip()

    if editor_model_file:
        if settings.ALLOW_JOURNALING:
            if not detect_editor_from_filename(editor_model_file.name):
                messages.error(request, f"Unsupported editor model file. Supported editors: {', '.join(ALLOWED_EDITORS)}.")
                return redirect("project_detail", project_id=project_id)
            
            if not validate_file_size(editor_model_file, 50):
                messages.error(request, f"Editor model file too large. Max 50MB.")
                return redirect("project_detail", project_id=project_id)
            
            editor_ext = os.path.splitext(editor_model_file.name)[1].lower()
            editor_model_key = default_storage.save(
                random_storage_key("editor_models", editor_ext), editor_model_file
            )
        else:
            messages.error(request, "File uploads are currently disabled.")
            return redirect("project_detail", project_id=project_id)

        # Store the object key (not a URL) — the bucket is private and served
        # through serve_media. External links are kept verbatim below.
        project.editor_model_url = editor_model_key
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
@rate_limit("update_project_image", 2)
def update_project_image(request, project_id):
    """Store the screenshot shown on the project's book cover."""
    project = get_object_or_404(request.user.projects, id=project_id, deleted=False)

    # "detail" is the only alternative — never trust the value as a URL.
    back = redirect("project_detail", project_id=project_id) if request.POST.get("next") == "detail" else redirect("projects")

    if project.locked:
        messages.error(request, "You cannot edit a locked project.")
        return back

    if not settings.ALLOW_JOURNALING:
        messages.error(request, "File uploads are currently disabled.")
        return back

    image_file = request.FILES.get("image")
    if not image_file:
        messages.error(request, "Choose a screenshot to upload.")
        return back

    if not validate_file_size(image_file, 5):
        messages.error(request, "Max file size for images is 5MB.")
        return back

    image_ext = sniff_image_extension(image_file)
    if not image_ext:
        messages.error(request, "Uploaded image must be a valid PNG, JPEG, GIF, or WEBP file.")
        return back

    # Store the object key (not a URL) — the bucket is private and served
    # through serve_media.
    project.image_url = default_storage.save(random_storage_key("images", image_ext), image_file)
    project.save()

    messages.success(request, "Screenshot updated successfully.")
    return back


@login_required
@require_POST
@rate_limit("delete_project", 2)
def delete_project(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id, deleted=False)

    if project.locked:
        messages.error(request, "You cannot delete a locked project.")
        return redirect("projects")

    in_flight = project.ships.exclude(
        status__in=(Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED)
    ).exists()
    if in_flight:
        messages.error(request, "You cannot delete a project while a ship is under review. Wait until it is finalized or rejected.")
        return redirect("projects")

    project.deleted = True
    project.save()

    return redirect("projects")

@login_required
def project_detail(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id, deleted=False)
    user = request.user
    profile = request.user.hackclub_profile
    ships = list(project.ships.order_by('-created_at'))
    # Oldest first: the book reads front to back, and the empty space for the
    # next lapse is at the end of the run.
    journals = project.journals.order_by('id')

    time_spent = format_minutes(tracked_minutes_for_journals(journals))

    latest_ship = ships[0] if ships else None
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
    elif not project.image_url:
        can_ship = False
        ship_disabled_reason = "You need to upload a screenshot of your project before you can ship."
    elif ship_pending:
        can_ship = False
        ship_disabled_reason = "Your most recent ship must be finalized or rejected before you can reship."
    elif not project.journals.exists() and not can_bypass_ship_requirements(request.user):
        can_ship = False
        ship_disabled_reason = "You need at least one lapse before you can ship."
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
        return max(candidates, key=lambda x: x[0])[1] if candidates else ""

    for ship in ships:
        ship.latest_feedback = get_latest_feedback(ship)

    timelapses = list(project.timelapses.filter(owner=request.user).select_related("journal"))
    _refresh_lookouts(timelapses)
    # Re-read after the refresh: one of them may have just finished.
    attachable_timelapses = list(_attachable_timelapses(project, request.user))
    # Recordings that aren't ready to attach yet still need somewhere to be
    # picked back up from, so the book lists them alongside the picker.
    unfinished_timelapses = [
        timelapse for timelapse in timelapses if not timelapse.is_complete
    ]
    # However many are mid-flight, they are worth one line between them: which
    # one to pick back up. Listing each is the same sentence over and over.
    lookout_status = None
    recordable = [t for t in unfinished_timelapses if t.is_recordable]
    processing = [t for t in unfinished_timelapses if t.is_processing]
    failed = [t for t in unfinished_timelapses if t not in recordable and t not in processing]
    if recordable:
        lookout_status = {
            "label": "recording" if len(recordable) == 1 else f"{len(recordable)} recording",
            "url": reverse("record_timelapse", args=[recordable[0].pk]),
        }
    elif processing:
        lookout_status = {
            "label": "building" if len(processing) == 1 else f"{len(processing)} building",
            "url": reverse("record_timelapse", args=[processing[0].pk]),
        }
    elif failed:
        lookout_status = {
            "label": "failed" if len(failed) == 1 else f"{len(failed)} failed",
            "url": "",
        }

    # Arriving from an old recorder link (or straight off starting one without
    # JS) names the session the book should pop the recorder open on.
    record_session_url = ""
    requested = request.GET.get("record", "")
    if requested.isdigit() and any(str(t.pk) == requested for t in timelapses):
        record_session_url = reverse("record_timelapse", args=[int(requested)])

    pages = _book_pages(journals, allow_new=not project.locked)

    return render(request, "atlantis_site/project_detail.html", {
        "project": project,
        "user": user,
        "profile": profile,
        "ships": ships,
        "journals": journals,
        "pages": pages,
        "time_spent": time_spent,
        "can_ship": can_ship,
        "ship_disabled_reason": ship_disabled_reason,
        "printablesData": printablesData,
        "allowed_editors": ALLOWED_EDITORS,
        "allowed_editor_extensions": ",".join(EDITOR_FILE_EXTENSIONS.keys()),
        "pickable_timelapses": attachable_timelapses,
        "unfinished_timelapses": unfinished_timelapses,
        "lookout_status": lookout_status,
        "record_session_url": record_session_url,
    })

@login_required
def explore(request):
    profile = request.user.hackclub_profile

    projects_unlocked = Project.objects.filter(deleted=False).exclude(locked=True)
    projects = projects_unlocked.exclude(owner=request.user)

    return render(request, "atlantis_site/explore.html", {'profile': profile, 'projects': projects})

@login_required
def project_detail_explore(request, project_id):
    project = get_object_or_404(Project, id=project_id, deleted=False)
    if project.locked and not request.user.has_perm("atlantis_site.organizer"):
        raise PermissionDenied

    user = request.user
    profile = user.hackclub_profile
    ships = project.ships.order_by("-created_at")
    # Oldest first: the public page is the same book, and it reads front to back.
    journals = project.journals.order_by("id")

    time_spent = format_minutes(tracked_minutes_for_journals(journals))

    if project.printablesUrl:
        try:
            printablesData = get_model_info(project.printablesUrl.split('/model/')[1].split('-')[0])
        except:
            printablesData = {"makesCount": 0}
    else:
        printablesData = {"makesCount": 0}

    # No writing space: a reader can turn the pages but not add to them.
    pages = _book_pages(journals, allow_new=False)

    is_following = project.followers.filter(pk=user.pk).exists()
    follower_count = project.followers.count()

    return render(request, "atlantis_site/project_detail_explore.html", {
        "project": project,
        "user": user,
        "profile": profile,
        "ships": ships,
        "journals": journals,
        "pages": pages,
        "time_spent": time_spent,
        "printablesData": printablesData,
        "is_following": is_following,
        "follower_count": follower_count,
    })


@login_required
@require_POST
@rate_limit("follow_project", 1)
def follow_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, deleted=False)
    if project.locked and not request.user.has_perm("atlantis_site.organizer"):
        raise PermissionDenied
    if project.owner == request.user:
        messages.error(request, "You can't follow your own project.")
        return redirect("project_detail_explore", project_id=project_id)

    project.followers.add(request.user)
    messages.success(request, f'You are now following "{project.title}". You\'ll be notified of new journal entries and ships.')
    return redirect("project_detail_explore", project_id=project_id)


@login_required
@require_POST
@rate_limit("unfollow_project", 1)
def unfollow_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, deleted=False)
    project.followers.remove(request.user)
    messages.success(request, f'You have unfollowed "{project.title}".')
    return redirect("project_detail_explore", project_id=project_id)

@login_required
@rate_limit("create_journal", 3)
def create_journal(request, project_id):
    if request.method != 'POST':
        return redirect("project_detail", project_id=project_id)
    
    if not settings.ALLOW_JOURNALING and not request.user.has_perm("atlantis_site.organizer"):
        messages.error(request, "Journaling is disallowed on this instance!")
        return redirect("project_detail", project_id=project_id)

    project = get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)

    if project.locked:
        messages.error(request, "You cannot create a journal on a locked project.")
        return redirect("projects")

    # Time is never self-reported — an entry's time is the sum of the Lookout
    # timelapses attached to it, so at least one is required.
    try:
        timelapse_ids = {int(raw) for raw in request.POST.getlist("timelapses")}
    except ValueError:
        messages.error(request, "Invalid Lookout selection.")
        return redirect("project_detail", project_id=project_id)

    if not timelapse_ids:
        messages.error(request, "Attach at least one finished Lookout to your lapse!")
        return redirect("project_detail", project_id=project_id)

    if _attachable_timelapses(project, request.user, timelapse_ids).count() != len(timelapse_ids):
        messages.error(request, "One or more of those Lookouts can't be attached. Refresh and try again.")
        return redirect("project_detail", project_id=project_id)

    title = request.POST.get("title", "").strip()

    if not title:
        messages.error(request, "Your lapse needs a title.")
        return redirect("project_detail", project_id=project_id)

    image_file = request.FILES.get("image")
    model_file = request.FILES.get("STL")

    if not image_file:
        messages.error(request, "An image is required.")
        return redirect("project_detail", project_id=project_id)
    if not model_file:
        messages.error(request, "An STL model is required.")
        return redirect("project_detail", project_id=project_id)

    if not os.path.basename(model_file.name).lower().endswith(".stl"):
        messages.error(request, "Uploaded model must be an STL file.")
        return redirect("project_detail", project_id=project_id)

    if not validate_file_size(image_file, 5):
        messages.error(request, "Max file size for images is 5MB.")
        return redirect("project_detail", project_id=project_id)
    if not validate_file_size(model_file, 50):
        messages.error(request, "Max file size for STL files is 50MB.")
        return redirect("project_detail", project_id=project_id)

    image_ext = sniff_image_extension(image_file)
    if not image_ext:
        messages.error(request, "Uploaded image must be a valid PNG, JPEG, GIF, or WEBP file.")
        return redirect("project_detail", project_id=project_id)

    image_key = default_storage.save(random_storage_key("images", image_ext), image_file)
    model_key = default_storage.save(random_storage_key("models", ".stl"), model_file)

    # Store the object keys (not URLs) — the bucket is private and served
    # through serve_media.
    with transaction.atomic():
        available = _attachable_timelapses(
            project, request.user, timelapse_ids
        ).select_for_update()
        if available.count() != len(timelapse_ids):
            messages.error(request, "One or more of those Lookouts can't be attached. Refresh and try again.")
            return redirect("project_detail", project_id=project_id)

        journal = Journal.objects.create(
            project=project,
            title=title,
            image_url=image_key,
            model_url=model_key
        )
        available.update(journal=journal)

    notify_followers(
        request,
        project,
        f'A project you follow, "{project.title}", has had a new journal entry! Check it out!'
    )

    messages.success(request, "Lapse added successfully")
    return redirect("project_detail", project_id=project_id)
    
@login_required
@rate_limit("ship_project", 3)
def ship_project(request, project_id):
    # remember to check if the weight is greater than the time spent x 100
    if request.method != 'POST':
        return redirect("project_detail", project_id=project_id)
    
    project = get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)
    if project.locked:
        messages.error(request, "This project is locked. You cannot ship a locked project.")
        return redirect("projects")
    if not is_valid_printables_url(project.printablesUrl):
        messages.error(request, "You need a printables URL to ship!")
        return redirect("projects")
    if not project.editor_model_url:
        messages.error(request, "You need to upload or link your editor model before you can ship!")
        return redirect("projects")
    if not project.image_url:
        messages.error(request, "You need to upload a screenshot of your project before you can ship!")
        return redirect("projects")
    if not project.description:
        messages.error(request, "Your project must have a description before you can ship!")
        return redirect("projects")
    # DEBUG-only, organizer-only: skip the journal and tracked-time gates so the
    # rest of the pipeline can be exercised without hours of real recording.
    bypass_requirements = can_bypass_ship_requirements(request.user)

    unassigned_journals = project.journals.filter(ship__isnull=True)
    if not bypass_requirements and not unassigned_journals.exists():
        messages.error(request, "Your project must have at least one journal to be shipped")
        return redirect("projects")

    latest_ship = project.ships.order_by('-created_at').first()
    if latest_ship and latest_ship.status not in (Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED):
        messages.error(request, "You cannot reship until your most recent ship has been finalized or rejected.")
        return redirect("project_detail", project_id=project_id)

    if not bypass_requirements:
        unassigned_time = tracked_minutes_for_journals(unassigned_journals)
        if latest_ship:
            if unassigned_time <= 120:
                messages.error(request, "Can't ship again without at least 2 hours of work!")
                return redirect("projects")
        else:
            if unassigned_time <= 180:
                messages.error(request, "You must have atleast 3 hours of logged time before you can ship!")
                return redirect("projects")

    with transaction.atomic():
        ship = Ship.objects.create(
            project = project,
            status = Ship.ShipStatus.T1_QUEUE
        )
        project.journals.filter(ship__isnull=True).update(ship=ship)

    notify_followers(
        request,
        project,
        f'A project you follow, "{project.title}", has just shipped a new update! Check it out!'
    )

    messages.success(request, f'Successfully shipped project "{project.title}"!')
    return redirect("projects")


# Object-key prefixes we upload to (see random_storage_key). serve_media only
# streams keys under these, so the view can never be used to read arbitrary
# objects out of the bucket.
ALLOWED_MEDIA_PREFIXES = ("images/", "models/", "editor_models/")


@login_required
def serve_media(request, key):
    """Stream a private-bucket object back to the browser.

    The R2 bucket has no public URL, so uploaded files (stored as object keys)
    are proxied through here: we open the object with the server's S3
    credentials and stream it to the (authenticated) requester.
    """
    if ".." in key or not key.startswith(ALLOWED_MEDIA_PREFIXES):
        raise Http404

    try:
        file = default_storage.open(key)
        # S3 opens lazily, so force the object to be fetched now: a missing or
        # inaccessible key then surfaces here as a 404 instead of a 500 raised
        # mid-stream, outside this view.
        file.read(1)
        file.seek(0)
    except (FileNotFoundError, OSError, ClientError):
        raise Http404

    content_type, _ = mimetypes.guess_type(key)
    return FileResponse(file, content_type=content_type or "application/octet-stream")