"""Switches nearly every page needs, and no view should have to remember.

Only what a template cannot reach any other way belongs here. A flag that one
view cares about stays in that view's context, the way `lookout_allow_new` is
passed by project_detail() — the point of this module is the handful of toggles
that are about the deployment rather than the request, so that adding one does
not mean editing every view that happens to render a page.
"""

from django.conf import settings


def mihi_mode(request):
    """Whether mihi mode is switched on for this deployment at all.

    The button also checks that somebody is signed in, because the click is
    tallied per user; this is the other half — the off switch that takes the
    button off every page at once.
    """
    return {"mihi_mode": settings.MIHI_MODE}
