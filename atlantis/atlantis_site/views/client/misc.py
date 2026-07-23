from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404

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

    return render(request, "atlantis_site/user.html", {
        "profile": profile,
        "user_viewed": user_viewed,
        "viewed_profile": viewed_profile,
        "projects": projects,
        "is_self": is_self,
    })
