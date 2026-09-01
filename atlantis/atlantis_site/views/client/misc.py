from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.http import Http404

from ...models import Journal, Ship, Timelapse

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


@login_required
def guides(request):
    return render(request, "atlantis_site/guides.html", {
        "profile": request.user.hackclub_profile,
        "guides_nav": GUIDES,
        "active_guide": None,
    })


@login_required
def guide_detail(request, slug):
    # Templates are picked from the registry rather than straight off the URL,
    # so a made-up slug is a 404 and never a template path to go hunting for.
    if slug not in GUIDE_SLUGS:
        raise Http404("No such guide")

    return render(request, f"atlantis_site/guides/{slug}.html", {
        "profile": request.user.hackclub_profile,
        "guides_nav": GUIDES,
        "active_guide": slug,
    })

@login_required
def printer_select(request):
    return redirect("dashboard")

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
