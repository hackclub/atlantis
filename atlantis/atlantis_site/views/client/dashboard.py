from django.shortcuts import render
from django.contrib.auth.decorators import login_required

from ... import challenge, weeks


def index(request):
    return render(request, "atlantis_site/home.html")


def challenge_context(user):
    """What the deck's streak panel needs, shared with anywhere else showing it.

    The deadline goes out as an ISO instant rather than a number of seconds
    left: a page can sit open for hours, and a countdown ticking down from a
    figure baked in at render time would drift away from the real one. The
    browser subtracts from its own clock instead.
    """
    state = challenge.standing(user)
    live = state.current

    return {
        "standing": state,
        "week": live,
        "week_deadline": weeks.deadline(live.index).isoformat() if live else "",
        "weekly_hours": weeks.WEEKLY_HOURS,
        "program_starts": weeks.starts_at().isoformat(),
        "dropped_reason": challenge.shipping_blocked_reason(user),
    }


@login_required
def dashboard(request):
    profile = request.user.hackclub_profile
    return render(request, "atlantis_site/dashboard.html", {
        "profile": profile,
        **challenge_context(request.user),
    })
