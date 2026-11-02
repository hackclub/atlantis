from django.contrib import messages
from django.shortcuts import redirect, render
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

from ... import challenge, weeks
from ...models import MakeupDeferral


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
        "dropped_reason": challenge.elimination_reason(user),
    }


@login_required
def dashboard(request):
    profile = request.user.hackclub_profile
    return render(request, "atlantis_site/dashboard.html", {
        "profile": profile,
        **challenge_context(request.user),
    })


@login_required
@require_POST
def defer_makeup(request):
    """Choose whether this week's deflations are made up this week or next.

    Only the week in progress can be changed, and only to the week straight
    after it: a closed week's bar is settled, and makeup pushed further than
    one week would just pile up at the end.
    """
    week = challenge.standing(request.user).current
    if week is None or not week.can_defer_makeup:
        messages.error(request, "There's no makeup this week you can move.")
        return redirect("dashboard")

    if request.POST.get("when") == "next":
        MakeupDeferral.objects.get_or_create(user=request.user, week_index=week.index)
        messages.success(
            request,
            f"{week.own_makeup_display} of makeup moved to week {week.index + 1}.",
        )
    else:
        MakeupDeferral.objects.filter(user=request.user, week_index=week.index).delete()
        messages.success(request, f"{week.own_makeup_display} of makeup is due this week.")
    return redirect("dashboard")
