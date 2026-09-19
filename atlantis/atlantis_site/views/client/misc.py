from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.db.models import F, Sum
from django.http import Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ...models import Journal, MihiActivation, Timelapse, Ship
from ...printers import tracks, track as find_track
from ..helpers import rate_limit

# The one list of guides: the scroll rail in _guides_base.html renders it, and
# guide_detail() will only serve a slug that appears here. Adding a guide means
# adding a row plus templates/atlantis_site/guides/<slug>.html — no new route.
GUIDES = (
    {
        "slug": "intro",
        "title": "Intro",
        "blurb": "What Atlantis is, how the eight weeks run, and what CAD means here.",
    },
    {
        "slug": "faq",
        "title": "FAQ",
        "blurb": "Who can join, what counts, which printer you get, and who owns your design.",
    },
    {
        "slug": "cad-software",
        "title": "CAD Software",
        "blurb": "The approved packages, and how Fusion, Onshape, and Solidworks compare.",
    },
    {
        "slug": "designing-for-3dp",
        "title": "Designing for 3D Printing",
        "blurb": "Walls, overhangs, holes, tolerances, fillets, and orientation: designing parts that actually print.",
    },
    {
        "slug": "project-guidelines",
        "title": "Project Guidelines",
        "blurb": "What makes a project good enough to pass review, and what is disallowed.",
    },
    {
        "slug": "shipping",
        "title": "What Is Shipping?",
        "blurb": "What it means to ship a project, and how to ship the same one twice.",
    },
)

GUIDE_SLUGS = frozenset(guide["slug"] for guide in GUIDES)


def _guide_profile(request):
    # The guides read the same whether or not anyone is signed in, so these two
    # views are the public exception on this module. A logged-out reader has no
    # profile and no deck to go back to; _guides_base.html handles both.
    if request.user.is_authenticated:
        return request.user.hackclub_profile
    return None


def guides(request):
    return render(request, "atlantis_site/guides.html", {
        "profile": _guide_profile(request),
        "guides_nav": GUIDES,
        "active_guide": None,
    })


def guide_detail(request, slug):
    # Templates are picked from the registry rather than straight off the URL,
    # so a made-up slug is a 404 and never a template path to go hunting for.
    if slug not in GUIDE_SLUGS:
        raise Http404("No such guide")

    return render(request, f"atlantis_site/guides/{slug}.html", {
        "profile": _guide_profile(request),
        "guides_nav": GUIDES,
        "active_guide": slug,
    })

@login_required
def printer_select(request):
    # The chart room: every track's constellation at once, each one a way in
    # to the tree that printer_track() draws.
    return render(request, "atlantis_site/printer_select.html", {
        "profile": request.user.hackclub_profile,
        "tracks": tracks(),
    })


@login_required
def printer_track(request, slug):
    # Same guard as guide_detail: the slug has to name a track we know, so a
    # made-up one is a 404 rather than a blank map.
    chosen = find_track(slug)
    if chosen is None:
        raise Http404("No such printer track")

    return render(request, "atlantis_site/printer_track.html", {
        "profile": request.user.hackclub_profile,
        "track": chosen,
    })

@login_required
def user_profile(request, user_id):
    profile = request.user.hackclub_profile
    user_viewed = get_object_or_404(get_user_model(), id=user_id)
    viewed_profile = user_viewed.hackclub_profile
    is_self = user_viewed == request.user

    projects = user_viewed.projects.filter(deleted=False)
    if not is_self and not request.user.has_perm("atlantis_site.organizer"):
        projects = projects.exclude(locked=True)
    projects = projects.order_by("id")

    journals = Journal.objects.filter(project__in=projects).select_related("project").order_by("-created_at")
    journal_count = journals.count()
    ship_count = Ship.objects.filter(project__in=projects).count()
    # Tracked, not approved: approved/removed seconds come from timelapse
    # review, which is internal and never shown back to the person it's about.
    tracked_seconds = Timelapse.objects.filter(project__in=projects).aggregate(
        total=Sum("tracked_seconds")
    )["total"] or 0

    return render(request, "atlantis_site/user.html", {
        "profile": profile,
        "user_viewed": user_viewed,
        "viewed_profile": viewed_profile,
        "projects": projects,
        "journals": journals[:12],
        "journal_count": journal_count,
        "ship_count": ship_count,
        "tracked_hours": tracked_seconds // 3600,
        "tracked_minutes": (tracked_seconds % 3600) // 60,
        "is_self": is_self,
    })


@login_required
@require_POST
@rate_limit("mihi_activate", 1, json=True)
def mihi_activate(request):
    """Note that this user turned mihi mode on. The button is a joke and this is
    all it records: that today's row for them exists, and that they clicked
    again. The response is empty because the browser has already done the only
    thing the click was for — the effect is local, and nothing here is read back
    into the page.
    """
    activation, created = MihiActivation.objects.get_or_create(
        user=request.user,
        activated_on=timezone.localdate(),
    )
    if not created:
        # F() rather than activation.save(): two clicks racing each other should
        # count twice, not have the later one overwrite the earlier.
        MihiActivation.objects.filter(pk=activation.pk).update(
            activations_count=F("activations_count") + 1
        )
    return JsonResponse({"ok": True})
