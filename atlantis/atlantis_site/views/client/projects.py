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
from django.utils import timezone
from django.http import FileResponse, Http404

from botocore.exceptions import ClientError

from datetime import datetime, timedelta, timezone as dt_timezone

import logging
import mimetypes

from ...models import (
    Project, Ship, Journal, Timelapse, ALLOWED_EDITORS, EDITOR_FILE_EXTENSIONS, EDITOR_ARCHIVE_EXTENSIONS, UNLISTED_EDITOR_EXTENSIONS, LINKABLE_EDITORS, is_editor_model_file
)
from ... import activity, challenge, lapse, lookout
from ...checklists import SHIP_CHECKLIST, unticked, unticked_message
from .lapse import account_for
from .timelapse import _apply_session_payload
from ..helpers import (
    is_valid_printables_url, is_valid_editor_model_url, get_model_info, validate_file_size,
    sniff_image_extension, random_storage_key,
    notify_followers, rate_limit, tracked_minutes_for_journals, format_minutes,
    can_bypass_ship_requirements, ysws_block_reason, field_max_length, fit, too_long,
    INT_FIELD_MAX,
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


def _duration_seconds(duration):
    """Lapse's `duration` as tracked seconds the column will hold.

    Anything unreadable counts as no time rather than raising: this runs inside
    the attach, where a ValueError out of int() would be a 500 on a lapse that
    is otherwise fine, and a recording with no duration is already the ordinary
    case the reviewer sees.
    """
    try:
        seconds = int(duration or 0)
    except (TypeError, ValueError):
        return 0
    return min(max(seconds, 0), INT_FIELD_MAX)


def _already_attached(user, lapse_ids):
    """Which of these Lapse recordings this user has already taped in.

    Across every book of theirs, not just this one: the same footage paid for
    twice is the thing the unique constraint on lapse_id exists to stop, and
    catching it here is what turns that into a sentence rather than a 500.

    A deleted book doesn't count. Deleting a project gives its footage back —
    the recording still exists on Lapse and the hours behind it were never
    paid, because a project that has shipped can't be deleted at all.
    """
    return set(
        Timelapse.objects.filter(
            owner=user,
            source=Timelapse.Source.LAPSE,
            project__deleted=False,
            lapse_id__in=lapse_ids,
        ).values_list("lapse_id", flat=True)
    )


def _released_lapse_rows(user, lapse_ids):
    """The rows holding footage a deleted book let go of.

    _already_attached stops treating these as taped in, but the row is still
    there and the unique constraint on lapse_id is what the new attach would
    fail on — so the old row goes when the footage is claimed again, rather
    than at the delete. Leaving it until then is what lets an undeleted
    project keep its hours: nothing is lost until somebody else wants it.
    """
    return Timelapse.objects.filter(
        owner=user,
        source=Timelapse.Source.LAPSE,
        project__deleted=True,
        lapse_id__in=lapse_ids,
    )


def _attachable_lookouts(project, user, ids=None):
    """Finished Lookouts the user can still attach to a new lapse.

    Legacy. New time comes off Lapse; this is what keeps footage recorded
    before the switch — and anything still mid-recording on it — attachable.
    """
    qs = Timelapse.objects.filter(
        project=project,
        owner=user,
        source=Timelapse.Source.LOOKOUT,
        status=Timelapse.Status.COMPLETE,
        journal__isnull=True,
    )
    if ids is not None:
        qs = qs.filter(id__in=ids)
    return qs


# The recorder page is not the only way a session ends — closing the tab
# mid-flight used to leave its time unattachable — so the project page asks
# Lookout itself, throttled so a reload storm can't hammer the API.
LOOKOUT_REFRESH_AFTER = timedelta(seconds=20)
LOOKOUT_REFRESH_LIMIT = 3


def _refresh_lookouts(sessions):
    """Bring our copy of any unfinished Lookout up to date, in place."""
    stale_before = timezone.now() - LOOKOUT_REFRESH_AFTER
    asked = 0
    for session in sessions:
        # A Lapse row has no session id to ask about, and arrives finished.
        if not session.is_lookout:
            continue
        if session.is_complete or session.status == Timelapse.Status.FAILED:
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

    # The column is narrower than the 2048 characters is_valid_printables_url
    # will vouch for, and Postgres answers the overflow with a DataError — a
    # 500 on a link that is otherwise perfectly good.
    if too_long(printables_url, Project, "printablesUrl"):
        messages.error(request, f"Printables URL too long (max {field_max_length(Project, 'printablesUrl')} chars)")
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

    # The column is narrower than the 2048 characters is_valid_printables_url
    # will vouch for, and Postgres answers the overflow with a DataError — a
    # 500 on a link that is otherwise perfectly good.
    if too_long(printables_url, Project, "printablesUrl"):
        messages.error(request, f"Printables URL too long (max {field_max_length(Project, 'printablesUrl')} chars)")
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

    # "Remove" is a second submit button on the same form, so it arrives with
    # the file and link fields alongside it — it has to be read before them or
    # a half-filled form would be taken as an edit instead.
    if request.POST.get("clear"):
        if not project.editor_model_url:
            messages.error(request, "There is no editor model to remove.")
            return redirect("project_detail", project_id=project_id)

        # The stored object is left in the bucket: a rejected ship is reviewed
        # against what was uploaded at the time, so the key stays readable.
        project.editor_model_url = ""
        project.save()
        messages.success(request, "Editor model removed.")
        return redirect("project_detail", project_id=project_id)

    if editor_model_file:
        if settings.ALLOW_JOURNALING:
            if not is_editor_model_file(editor_model_file.name):
                messages.error(request, f"Unsupported editor model file. Supported editors: {', '.join(ALLOWED_EDITORS)}. You can also upload a .zip of your project files.")
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
        
        # The rule the admin form already uses: a share link from an editor we
        # recognize, or a direct link to a source file. Matching share domains
        # alone left Solidworks and FreeCAD users with no working link at all,
        # under a message that named their editor as supported.
        if not is_valid_editor_model_url(editor_model_link):
            messages.error(request, f"Unsupported editor model link. Link a document on {' or '.join(LINKABLE_EDITORS)}, or link straight to a file ending in {', '.join([*EDITOR_FILE_EXTENSIONS, *sorted(EDITOR_ARCHIVE_EXTENSIONS)])}.")
            return redirect("project_detail", project_id=project_id)

        if too_long(editor_model_link, Project, "editor_model_url"):
            messages.error(request, f"Editor model link too long (max {field_max_length(Project, 'editor_model_url')} chars)")
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

    # A finalized ship has already been paid for, and deleting the project is
    # what frees its footage to be taped in again (see _released_lapse_rows) —
    # so a project that has shipped is never deletable, or the same hours could
    # be claimed a second time.
    if project.ships.filter(status=Ship.ShipStatus.FINALIZED).exists():
        messages.error(request, "You cannot delete a project that has already been finalized..")
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
    # A T1 reviewer asked for changes: the ship is waiting on its owner, and
    # the button resubmits it rather than shipping a new one.
    resubmitting = latest_ship is not None and latest_ship.status == Ship.ShipStatus.CHANGES_REQUESTED
    ship_pending = latest_ship is not None and latest_ship.status not in (
        Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED, Ship.ShipStatus.CHANGES_REQUESTED,
    )

    # Only the owner is ever offered the button, so only the owner's copy has
    # to work out whether it is live.
    #
    # The button stays live either way: when it can't ship it opens the list of
    # what's in the way instead, and a list is only worth opening if it is the
    # whole of it, so the requirements are all asked rather than stopping at the
    # first no. Each one carries the slip that clears it where there is one, so
    # the fix is a click away rather than a hunt around the book.
    can_ship = False
    ship_blockers = []
    if is_owner:
        # Some answers are about where the account or the project stands rather
        # than about a piece that is missing. While one of them holds there is
        # nothing on the rest of the list worth doing anything about, so it is
        # the whole answer.
        halted = (
            ysws_block_reason(user)
            or challenge.shipping_blocked_reason(user)
            or (project.locked and "This project is locked and cannot be shipped.")
            or (ship_pending and "Your most recent ship must be finalized or rejected before you can reship.")
        )
        if halted:
            ship_blockers.append({"text": halted})
        else:
            if not is_valid_printables_url(project.printablesUrl):
                ship_blockers.append({
                    "text": "You need a valid Printables URL before you can ship.",
                    "slip": "slip-edit",
                    "action": "add the link",
                })
            if not project.editor_model_url:
                ship_blockers.append({
                    "text": "You need to upload or link your editor model before you can ship.",
                    "slip": "slip-model",
                    "action": "upload the file",
                })
            if not project.image_url:
                ship_blockers.append({
                    "text": "You need to upload a screenshot of your project before you can ship.",
                    "slip": "slip-screenshot",
                    "action": "upload a screenshot",
                })
            # The gate ship_project actually enforces: shipping claims the
            # journals it carries, so what's left to ship is the lapses no ship
            # has taken yet. Counting every lapse ever written would light the
            # button up after a rejection and then bounce the post. A
            # resubmission already carries its lapses, so it needs no new one.
            if not resubmitting and not project.journals.filter(ship__isnull=True).exists() and not can_bypass_ship_requirements(user):
                ship_blockers.append({
                    "text": (
                        "You need a new lapse before you can reship."
                        if latest_ship
                        else "You need at least one lapse before you can ship."
                    )
                })
        can_ship = not ship_blockers

    # The same answer as one sentence, for anywhere a list doesn't fit.
    ship_disabled_reason = " ".join(blocker["text"] for blocker in ship_blockers)

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

    # Likewise the recordings: they are the owner's, and nobody else has
    # anything to attach them to.
    #
    # The Lapse half of this is deliberately thin. Its picker loads over XHR
    # rather than from here, so a timelapse published while the book is open is
    # one refresh button away instead of a page reload — all the page needs to
    # know is whether there is a connection for it to read.
    lapse_account = account_for(user) if is_owner else None
    lapse_connected = bool(lapse_account and lapse_account.is_usable)
    lapse_expired = bool(lapse_account and not lapse_account.is_usable)

    # The Lookout half is the legacy drawer, and it *is* rendered here: it is a
    # finite list of rows we already hold, and there is no live service call to
    # make for it beyond refreshing anything still recording.
    attachable_timelapses = []
    unfinished_timelapses = []
    lookout_status = None
    record_session_url = ""
    has_lookout_history = False

    if is_owner:
        timelapses = list(
            project.timelapses.filter(
                owner=user, source=Timelapse.Source.LOOKOUT
            ).select_related("journal")
        )
        has_lookout_history = bool(timelapses)
        _refresh_lookouts(timelapses)
        # Re-read after the refresh: one of them may have just finished.
        attachable_timelapses = list(_attachable_lookouts(project, user))
        # Recordings that aren't ready to attach yet still need somewhere to be
        # picked back up from, so the book lists them alongside the picker.
        unfinished_timelapses = [
            timelapse for timelapse in timelapses if not timelapse.is_complete
        ]
        # However many are mid-flight, they are worth one line between them: which
        # one to pick back up. Listing each is the same sentence over and over.
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
        requested = request.GET.get("record", "")
        if requested.isdigit() and any(str(t.pk) == requested for t in timelapses):
            record_session_url = reverse("record_timelapse", args=[int(requested)])

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
        "resubmitting": resubmitting,
        "ship_blockers": ship_blockers,
        "ship_disabled_reason": ship_disabled_reason,
        "ship_checklist": SHIP_CHECKLIST,
        "printablesData": printablesData,
        "allowed_editors": ALLOWED_EDITORS,
        # Feeds the file picker's accept list only, never any visible copy, so
        # the unadvertised extensions can be selectable without being named.
        "allowed_editor_extensions": ",".join([
            *EDITOR_FILE_EXTENSIONS, *sorted(EDITOR_ARCHIVE_EXTENSIONS), *sorted(UNLISTED_EDITOR_EXTENSIONS)
        ]),
        "linkable_editors": LINKABLE_EDITORS,
        # Lapse — the way time is logged now.
        "lapse_account": lapse_account,
        "lapse_connected": lapse_connected,
        "lapse_expired": lapse_expired,
        "lapse_configured": lapse.is_configured(),
        "lapse_web_url": settings.LAPSE_WEB_BASE_URL,
        "lapse_timelapses_url": reverse("lapse_timelapses", args=[project.id]),
        # Lookout — the legacy drawer.
        "pickable_timelapses": attachable_timelapses,
        "unfinished_timelapses": unfinished_timelapses,
        "lookout_status": lookout_status,
        "record_session_url": record_session_url,
        "lookout_allow_new": settings.LOOKOUT_ALLOW_NEW and bool(settings.LOOKOUT_TOKEN),
        # Whether to show the legacy drawer at all: somebody who has never
        # touched Lookout is not owed a menu about it.
        "show_lookout": has_lookout_history or (
            settings.LOOKOUT_ALLOW_NEW and bool(settings.LOOKOUT_TOKEN)
        ),
        "is_following": project.followers.filter(pk=user.pk).exists(),
        "follower_count": project.followers.count(),
        # Why the book won't take a new lapse, if it won't. Only ever set for
        # the owner: a visitor's copy says nothing about their streak.
        "journal_blocked_reason": (
            challenge.journaling_blocked_reason(user) if is_owner else ""
        ),
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

    # Someone who is out of the program stops banking hours until they buy
    # their way back in — logging time that could never count towards anything
    # would be worse than being told plainly.
    dropped = challenge.journaling_blocked_reason(request.user)
    if dropped:
        messages.error(request, dropped)
        return redirect("project_detail", project_id=project_id)

    project = get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)

    if project.locked:
        messages.error(request, "You cannot create a journal on a locked project.")
        return redirect("projects")

    # Time is never self-reported — an entry's time is the sum of the recordings
    # attached to it, so at least one is required. Two fields rather than one,
    # because the two sources are named differently: Lapse sends its own opaque
    # ids and Lookout sends row primary keys, and an id that happens to be all
    # digits must not be able to pass for the other kind.
    lapse_ids = [raw.strip() for raw in request.POST.getlist("lapse_timelapses") if raw.strip()]
    try:
        lookout_ids = {int(raw) for raw in request.POST.getlist("lookout_timelapses")}
    except ValueError:
        messages.error(request, "Invalid Lookout selection.")
        return redirect("project_detail", project_id=project_id)

    if not lapse_ids and not lookout_ids:
        messages.error(request, "Attach at least one timelapse to your lapse!")
        return redirect("project_detail", project_id=project_id)

    if len(set(lapse_ids)) != len(lapse_ids):
        messages.error(request, "That selection has the same timelapse in it twice.")
        return redirect("project_detail", project_id=project_id)

    if lookout_ids and _attachable_lookouts(project, request.user, lookout_ids).count() != len(lookout_ids):
        messages.error(request, "One or more of those Lookouts can't be attached. Refresh and try again.")
        return redirect("project_detail", project_id=project_id)

    # Read the Lapse footage back from Lapse rather than believing the form.
    # What the browser sent is a list of ids and nothing else: the tracked time
    # on each one is what turns into hours and then into money, so it comes
    # from the API on the way in, every time.
    selected_lapses = []
    if lapse_ids:
        account = account_for(request.user)
        if account is None:
            messages.error(request, "Connect your Lapse account before taping in a lapse.")
            return redirect("project_detail", project_id=project_id)
        if not account.is_usable:
            # Worth its own sentence: reconnecting is a different action from
            # connecting, and there is no refresh grant that could spare them it.
            messages.error(request, "Your Lapse connection has expired. Reconnect and try again.")
            return redirect("project_detail", project_id=project_id)

        try:
            published = lapse.fetch_published_timelapses(account.access_token)
        except lapse.LapseAuthError:
            # Refused by Lapse, whatever this side believed about the expiry.
            # Dropping it is what turns the book back into a reconnect prompt.
            account.forget_token()
            messages.error(request, "Your Lapse connection has expired. Reconnect and try again.")
            return redirect("project_detail", project_id=project_id)
        except lapse.LapseError as exc:
            logger.warning("Lapse fetch failed while taping in for user %s: %s", request.user.pk, exc)
            messages.error(request, "Couldn't reach Lapse to check those timelapses. Try again in a moment.")
            return redirect("project_detail", project_id=project_id)

        by_id = {item.get("id"): item for item in published if item.get("id")}
        for lapse_id in lapse_ids:
            # Unlike the other Lapse strings below, this one is the uniqueness
            # key and cannot be trimmed to fit — a prefix would collide with
            # footage it isn't — so an id too wide for the column is refused.
            if too_long(lapse_id, Timelapse, "lapse_id"):
                messages.error(request, "One or more of those timelapses can't be attached. Refresh and try again.")
                return redirect("project_detail", project_id=project_id)
            found = by_id.get(lapse_id)
            # Not on the account, still processing, or processing failed. All
            # three mean the same thing here: there is no footage to stand
            # behind hours.
            if not lapse.is_attachable(found):
                messages.error(request, "One or more of those timelapses can't be attached. Refresh and try again.")
                return redirect("project_detail", project_id=project_id)
            selected_lapses.append(found)

        if _already_attached(request.user, lapse_ids):
            messages.error(request, "One of those timelapses is already taped into a lapse.")
            return redirect("project_detail", project_id=project_id)

    title = request.POST.get("title", "").strip()

    if not title:
        messages.error(request, "Your lapse needs a title.")
        return redirect("project_detail", project_id=project_id)

    if too_long(title, Journal, "title"):
        messages.error(request, f"Lapse title too long (max {field_max_length(Journal, 'title')} chars)")
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
    if not validate_file_size(model_file, 100):
        messages.error(request, "Max file size for STL files is 100MB.")
        return redirect("project_detail", project_id=project_id)

    image_ext = sniff_image_extension(image_file)
    if not image_ext:
        messages.error(request, "Uploaded image must be a valid PNG, JPEG, GIF, or WEBP file.")
        return redirect("project_detail", project_id=project_id)

    image_key = default_storage.save(random_storage_key("images", image_ext), image_file)
    model_key = default_storage.save(random_storage_key("models", ".stl"), model_file)

    # Store the object keys (not URLs) — the bucket is private and served
    # through serve_media.
    # The IntegrityError is caught outside the atomic block on purpose: a failed
    # statement poisons the transaction, so nothing may touch the database again
    # inside it — messages included. Letting it out is what rolls the journal
    # back with it.
    try:
        with transaction.atomic():
            # Locked before the journal is written, so two lapses taped in at
            # once can't both claim the same Lookout.
            available = _attachable_lookouts(
                project, request.user, lookout_ids
            ).select_for_update() if lookout_ids else Timelapse.objects.none()
            if lookout_ids and available.count() != len(lookout_ids):
                messages.error(request, "One or more of those Lookouts can't be attached. Refresh and try again.")
                return redirect("project_detail", project_id=project_id)

            journal = Journal.objects.create(
                project=project,
                title=title,
                image_url=image_key,
                model_url=model_key
            )

            # Lookout rows already exist — this recording happened here — so
            # attaching one is pointing it at the journal.
            attached = sorted(lookout_ids)
            if lookout_ids:
                available.update(journal=journal)

            # Footage a deleted book let go of is still held by the row that
            # book wrote, and that row is what the unique constraint would
            # refuse the new one over. Drop it here, where it's inside the
            # same transaction as the write that takes its place.
            if selected_lapses:
                _released_lapse_rows(
                    request.user, [item["id"] for item in selected_lapses]
                ).delete()

            # A Lapse row is written at the attach: until now the timelapse was
            # nothing of ours, just a recording sitting in somebody's account.
            for item in selected_lapses:
                row = Timelapse.objects.create(
                    project=project,
                    owner=request.user,
                    journal=journal,
                    source=Timelapse.Source.LAPSE,
                    lapse_id=item["id"],
                    # Trimmed to their columns, as the name already was:
                    # these are Lapse's strings, and one longer than we
                    # allowed for would fail the write rather than the attach.
                    name=fit(item.get("name"), Timelapse, "name"),
                    playback_url=fit(item.get("playbackUrl"), Timelapse, "playback_url"),
                    lapse_thumbnail_url=fit(item.get("thumbnailUrl"), Timelapse, "lapse_thumbnail_url"),
                    recorded_at=_recorded_at(item.get("createdAt")),
                    # Lapse's `duration` is recorded seconds, already in the
                    # unit this column is kept in. See the Timelapse docstring.
                    tracked_seconds=_duration_seconds(item.get("duration")),
                    # It arrived finished; there was no lifecycle to watch.
                    status=Timelapse.Status.COMPLETE,
                )
                attached.append(row.id)

            # The timelapse reviewer who eventually opens this entry needs the
            # inactivity track drawn under each recording, and drawing it is an
            # ffmpeg pass per video — minutes of work, and no reviewer is here
            # yet. Hand it to a worker thread once the attachment is committed,
            # so the thread reads rows that are actually there. Nothing about the
            # entry depends on it: a check that doesn't happen leaves the
            # recording unanalysed, which the review page says plainly.
            checkable = sorted(attached)
            transaction.on_commit(lambda: activity.check_sessions_in_background(checkable))
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
    # Missing a week ends your run, and the run is what shipping is for. Sits
    # after the HCA check because eligibility is the more fundamental refusal:
    # somebody HCA has turned down should hear that, not a streak message.
    dropped = challenge.shipping_blocked_reason(request.user)
    if dropped:
        messages.error(request, dropped)
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

    latest_ship = project.ships.order_by('-created_at').first()

    # A T1 reviewer asked for changes rather than rejecting the ship. The
    # same ship goes back into the T1 queue with its journals, plus anything
    # logged since, so none of the new-ship gates below apply to it.
    resubmitting = (
        latest_ship is not None and latest_ship.status == Ship.ShipStatus.CHANGES_REQUESTED
    )

    unassigned_journals = project.journals.filter(ship__isnull=True)
    if not bypass_requirements and not resubmitting and not unassigned_journals.exists():
        messages.error(request, "Your project must have at least one journal to be shipped")
        return redirect("projects")

    if latest_ship and latest_ship.status not in (
        Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED, Ship.ShipStatus.CHANGES_REQUESTED,
    ):
        messages.error(request, "You cannot reship until your most recent ship has been finalized or rejected.")
        return redirect("project_detail", project_id=project_id)

    # A rejected ship is finished work a reviewer sent back, and the fix it
    # asks for is usually minutes rather than hours. Holding the retry to the
    # same fresh-time gate as a voluntary reship would make small corrections
    # unshippable, so the retry answers to the journal requirement alone.
    retrying_rejection = (
        latest_ship is not None and latest_ship.status == Ship.ShipStatus.REJECTED
    )

    if not bypass_requirements and not retrying_rejection and not resubmitting:
        unassigned_time = tracked_minutes_for_journals(unassigned_journals)
        if unassigned_time <= 120:
            messages.error(
                request,
                "Can't ship again without at least 2 hours of work!"
                if latest_ship
                else "You must have at least 2 hours of logged time before you can ship!",
            )
            return redirect("projects")

    # Last, so the boxes are only worth ticking once everything the server can
    # check for itself has passed. The button on the book can't be pressed
    # until all of them are ticked; this is the same gate for a post that
    # didn't come from it.
    missing = unticked(SHIP_CHECKLIST, request)
    if missing:
        messages.error(request, unticked_message(
            missing, "Go through the shipping checklist first; still unchecked:"
        ))
        return redirect("project_detail", project_id=project_id)

    if resubmitting:
        with transaction.atomic():
            ship = Ship.objects.select_for_update().get(id=latest_ship.id)
            # Re-checked under the lock, so a double-clicked resubmit or a
            # rollback landing at the same moment can't be overwritten.
            if ship.status != Ship.ShipStatus.CHANGES_REQUESTED:
                messages.error(request, "That ship isn't waiting on changes any more.")
                return redirect("project_detail", project_id=project_id)
            ship.status = Ship.ShipStatus.T1_QUEUE
            ship.save(update_fields=["status"])
            project.journals.filter(ship__isnull=True).update(ship=ship)

        messages.success(request, f'Resubmitted "{project.title}" for T1 review!')
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