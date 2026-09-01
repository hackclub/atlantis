from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Sum
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.http import FileResponse, Http404

from botocore.exceptions import ClientError

from datetime import datetime, timezone as dt_timezone

import logging
import mimetypes

from ...models import (
    Project, Ship, Journal, Timelapse, ALLOWED_EDITORS, EDITOR_FILE_EXTENSIONS, detect_editor_from_filename, detect_editor_from_link
)
from ... import activity, lapse
from .lapse import account_for, complete_authorization
from ..helpers import (
    is_valid_printables_url, get_model_info, validate_file_size,
    sniff_image_extension, random_storage_key,
    notify_followers, rate_limit, tracked_minutes_for_journals, format_minutes,
    can_bypass_ship_requirements, ysws_block_reason,
)

import os

logger = logging.getLogger(__name__)


def _recorded_at(created_at):
    """Lapse's `createdAt`, which is epoch milliseconds, as a datetime.

    Milliseconds rather than the seconds the format usually means: the API
    returns 1788226165685 for a timelapse recorded in 2026. Read as seconds
    that lands fifty thousand years out, so the unit matters.
    """
    if not created_at:
        return None
    try:
        return datetime.fromtimestamp(int(created_at) / 1000, tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _already_attached(user, lapse_ids):
    """Which of these Lapse recordings this user has already taped in.

    Across every book of theirs, not just this one: the same footage paid for
    twice is the thing the unique constraint on lapse_id exists to stop, and
    catching it here is what turns that into a sentence rather than a 500.
    """
    return set(
        Timelapse.objects.filter(owner=user, lapse_id__in=lapse_ids)
        .values_list("lapse_id", flat=True)
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
    # This is also the redirect URI registered with Lapse, so a shipper coming
    # back from authorizing lands here with a code on the query string. That is
    # dealt with first: it always ends in a redirect, either to the book they
    # started from or to a clean copy of this URL, so a reload can't replay a
    # code that has already been spent.
    finished = complete_authorization(request)
    if finished is not None:
        return finished

    projects = list(request.user.projects.filter(deleted=False).order_by("id"))
    profile = request.user.hackclub_profile

    # Every book cover shows tracked time, so total it for all of them in one
    # query rather than one per book.
    tracked_seconds = dict(
        Timelapse.objects.filter(journal__project__in=projects)
        .order_by()  # Meta.ordering would otherwise land in the GROUP BY
        .values_list("journal__project")
        .annotate(total=Sum("tracked_seconds"))
    )

    for project in projects:
        project.tracked_hours = f"{tracked_seconds.get(project.id, 0) / 3600:.1f}h"

    return render(request, "atlantis_site/projects.html", {
        "projects": projects,
        "profile": profile,
        "create_blocked_reason": ysws_block_reason(request.user),
    })

@login_required
@require_POST
@rate_limit("create_project", 2)
def create_project(request):
    # A YSWS project is a claim on YSWS prizes, so who may start one is HCA's
    # call, not ours.
    blocked = ysws_block_reason(request.user)
    if blocked:
        messages.error(request, blocked)
        return redirect("projects")

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
    """The project's book — one page for everyone who opens it.

    The owner gets the writing tools taped in among the pages; anyone else
    reads the same book with nothing to write with.
    """
    project = get_object_or_404(Project, id=project_id, deleted=False)
    user = request.user
    is_owner = project.owner_id == user.id
    # A locked project is the owner's to look back on and an organizer's to
    # audit — it is off the shelf for everybody else.
    if project.locked and not is_owner and not user.has_perm("atlantis_site.organizer"):
        raise PermissionDenied

    profile = user.hackclub_profile
    ships = list(project.ships.order_by('-created_at'))
    # Oldest first: the book reads front to back, and the empty space for the
    # next lapse is at the end of the run.
    journals = project.journals.order_by('id')

    time_spent = format_minutes(tracked_minutes_for_journals(journals))

    latest_ship = ships[0] if ships else None
    ship_pending = latest_ship is not None and latest_ship.status not in (Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED)

    # Only the owner is ever offered the button, so only the owner's copy has
    # to work out whether it is live.
    can_ship = False
    ship_disabled_reason = ""
    if is_owner:
        ysws_blocked = ysws_block_reason(user)
        if ysws_blocked:
            ship_disabled_reason = ysws_blocked
        elif project.locked:
            ship_disabled_reason = "This project is locked and cannot be shipped."
        elif not is_valid_printables_url(project.printablesUrl):
            ship_disabled_reason = "You need a valid Printables URL before you can ship."
        elif not project.editor_model_url:
            ship_disabled_reason = "You need to upload or link your editor model before you can ship."
        elif not project.image_url:
            ship_disabled_reason = "You need to upload a screenshot of your project before you can ship."
        elif ship_pending:
            ship_disabled_reason = "Your most recent ship must be finalized or rejected before you can reship."
        # The gate ship_project actually enforces: shipping claims the journals
        # it carries, so what's left to ship is the lapses no ship has taken
        # yet. Counting every lapse ever written would light the button up
        # after a rejection and then bounce the post.
        elif not project.journals.filter(ship__isnull=True).exists() and not can_bypass_ship_requirements(user):
            ship_disabled_reason = (
                "You need a new lapse before you can reship."
                if latest_ship
                else "You need at least one lapse before you can ship."
            )
        else:
            can_ship = True

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

    # What a reviewer wrote is for the person who shipped it — a visitor's
    # copy of the book carries the status and nothing else.
    for ship in ships:
        ship.latest_feedback = get_latest_feedback(ship) if is_owner else ""

    # The picker is the owner's alone: nobody else has anything to tape a
    # recording into. It loads its list from Lapse over XHR rather than from
    # here, so a timelapse published while the book is open can be picked up by
    # the refresh button instead of a reload — all this needs to say is whether
    # there is a connection for it to read.
    lapse_account = account_for(user) if is_owner else None
    lapse_connected = bool(lapse_account and lapse_account.is_usable)

    pages = _book_pages(journals, allow_new=is_owner and not project.locked)

    return render(request, "atlantis_site/project_detail.html", {
        "project": project,
        "user": user,
        "profile": profile,
        "is_owner": is_owner,
        "ships": ships,
        "journals": journals,
        "pages": pages,
        "time_spent": time_spent,
        "can_ship": can_ship,
        "ship_disabled_reason": ship_disabled_reason,
        "printablesData": printablesData,
        "allowed_editors": ALLOWED_EDITORS,
        "allowed_editor_extensions": ",".join(EDITOR_FILE_EXTENSIONS),
        "lapse_account": lapse_account,
        "lapse_connected": lapse_connected,
        "lapse_timelapses_url": reverse("lapse_timelapses", args=[project.id]),
        "is_following": project.followers.filter(pk=user.pk).exists(),
        "follower_count": project.followers.count(),
    })

@login_required
def explore(request):
    profile = request.user.hackclub_profile

    projects_unlocked = Project.objects.filter(deleted=False).exclude(locked=True)
    # An empty book is nothing to browse: a project only reaches the shelf once
    # its owner has written at least one lapse into it.
    projects = projects_unlocked.exclude(owner=request.user).filter(
        Exists(Journal.objects.filter(project=OuterRef("pk")))
    )

    return render(request, "atlantis_site/explore.html", {'profile': profile, 'projects': projects})

@login_required
@require_POST
@rate_limit("follow_project", 1)
def follow_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, deleted=False)
    if project.locked and not request.user.has_perm("atlantis_site.organizer"):
        raise PermissionDenied
    if project.owner == request.user:
        messages.error(request, "You can't follow your own project.")
        return redirect("project_detail", project_id=project_id)

    project.followers.add(request.user)
    messages.success(request, f'You are now following "{project.title}". You\'ll be notified of new journal entries and ships.')
    return redirect("project_detail", project_id=project_id)


@login_required
@require_POST
@rate_limit("unfollow_project", 1)
def unfollow_project(request, project_id):
    project = get_object_or_404(Project, id=project_id, deleted=False)
    project.followers.remove(request.user)
    messages.success(request, f'You have unfollowed "{project.title}".')
    return redirect("project_detail", project_id=project_id)

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

    # Time is never self-reported — an entry's time is the sum of the Lapse
    # timelapses attached to it, so at least one is required.
    lapse_ids = [raw.strip() for raw in request.POST.getlist("timelapses") if raw.strip()]
    if not lapse_ids:
        messages.error(request, "Attach at least one Lapse timelapse to your lapse!")
        return redirect("project_detail", project_id=project_id)

    if len(set(lapse_ids)) != len(lapse_ids):
        messages.error(request, "That selection has the same timelapse in it twice.")
        return redirect("project_detail", project_id=project_id)

    account = account_for(request.user)
    if account is None or not account.is_usable:
        messages.error(request, "Connect your Lapse account before taping in a lapse.")
        return redirect("project_detail", project_id=project_id)

    # Read the footage back from Lapse rather than believing the form. What the
    # browser sent is a list of ids and nothing else: the tracked time on each
    # one is what turns into hours and then into money, so it comes from the
    # API on the way in, every time.
    try:
        published = lapse.fetch_published_timelapses(account.access_token)
    except lapse.LapseAuthError:
        messages.error(request, "Your Lapse connection has expired. Reconnect and try again.")
        return redirect("project_detail", project_id=project_id)
    except lapse.LapseError as exc:
        logger.warning("Lapse fetch failed while taping in for user %s: %s", request.user.pk, exc)
        messages.error(request, "Couldn't reach Lapse to check those timelapses. Try again in a moment.")
        return redirect("project_detail", project_id=project_id)

    by_id = {item.get("id"): item for item in published if item.get("id")}
    selected = []
    for lapse_id in lapse_ids:
        found = by_id.get(lapse_id)
        # Not on the account, still processing, or processing failed. All three
        # mean the same thing here: there is no footage to stand behind hours.
        if not found or not found.get("playbackUrl") or found.get("visibility") == "FAILED_PROCESSING":
            messages.error(request, "One or more of those timelapses can't be attached. Refresh and try again.")
            return redirect("project_detail", project_id=project_id)
        selected.append(found)

    if _already_attached(request.user, lapse_ids):
        messages.error(request, "One of those timelapses is already taped into a lapse.")
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
    #
    # The IntegrityError is caught outside the atomic block on purpose: a failed
    # statement poisons the transaction, so nothing may touch the database again
    # inside it — messages included. Letting it out is what rolls the journal
    # back with it.
    try:
        with transaction.atomic():
            journal = Journal.objects.create(
                project=project,
                title=title,
                image_url=image_key,
                model_url=model_key
            )

            rows = [
                Timelapse.objects.create(
                    project=project,
                    owner=request.user,
                    journal=journal,
                    lapse_id=item["id"],
                    name=(item.get("name") or "")[:120],
                    playback_url=item.get("playbackUrl") or "",
                    thumbnail_url=item.get("thumbnailUrl") or "",
                    recorded_at=_recorded_at(item.get("createdAt")),
                    # Lapse's `duration` is recorded seconds, already in the
                    # unit this column is kept in. See the Timelapse docstring.
                    tracked_seconds=int(item.get("duration") or 0),
                )
                for item in selected
            ]

            # The timelapse reviewer who eventually opens this entry needs the
            # inactivity track drawn under each recording, and drawing it is an
            # ffmpeg pass per video — minutes of work, and no reviewer is here
            # yet. Hand it to a worker thread once the attachment is committed,
            # so the thread reads rows that are actually there. Nothing about
            # the entry depends on it: a check that doesn't happen leaves the
            # recording unanalysed, which the review page says plainly.
            attached = sorted(row.id for row in rows)
            transaction.on_commit(
                lambda: activity.check_sessions_in_background(attached)
            )
    except IntegrityError:
        # The unique constraint on lapse_id caught a race the check above
        # couldn't: two lapses taped in at once, both naming the same footage.
        # The rolled-back transaction took the journal with it.
        messages.error(request, "One of those timelapses is already taped into a lapse.")
        return redirect("project_detail", project_id=project_id)

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
    # Shipping is a claim on YSWS prizes, so whether it may happen at all is
    # HCA's call, not ours.
    blocked = ysws_block_reason(request.user)
    if blocked:
        messages.error(request, blocked)
        return redirect("projects")
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

    # A rejected ship is finished work a reviewer sent back, and the fix it
    # asks for is usually minutes rather than hours. Holding the retry to the
    # same fresh-time gate as a voluntary reship would make small corrections
    # unshippable, so the retry answers to the journal requirement alone.
    retrying_rejection = (
        latest_ship is not None and latest_ship.status == Ship.ShipStatus.REJECTED
    )

    if not bypass_requirements and not retrying_rejection:
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