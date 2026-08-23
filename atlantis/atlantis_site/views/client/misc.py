from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.db.models import Sum

from ...models import Journal, LookoutSession, Ship

@login_required
def guides(request):
    return render(request, "atlantis_site/guides.html", {
        "profile": request.user.hackclub_profile,
    })

@login_required
def printer_select(request):
    return render(request, "atlantis_site/printer_select.html", {
        "profile": request.user.hackclub_profile,
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
    tracked_seconds = LookoutSession.objects.filter(project__in=projects).aggregate(
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
