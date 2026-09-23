"""Where organizers watch the streaks and, when they have to, overrule them.

Two pages. The roster is every participant against every week, so "who is out
and why" is one glance rather than a query. The per-user page is where a wrong
call gets fixed: grant saver hours, or force a week to pass or fail outright.

Every one of those is a thumb on the scale, so all three are audit-logged with
the reason the organizer typed.
"""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ... import challenge, weeks
from ...models import SaverCredit, WeekOutcome
from ..helpers import check_perms, display_name, record_audit

CHALLENGE_PERMS = ["atlantis_site.organizer"]


@staff_member_required
@check_perms(CHALLENGE_PERMS)
def challenge_dash(request):
    """Everyone's week-by-week standing, worst off first."""
    User = get_user_model()
    search = request.GET.get("q", "").strip()
    only = request.GET.get("show", "").strip()

    users = User.objects.select_related("hackclub_profile").order_by("id")
    if search:
        users = users.filter(username__icontains=search)

    rows = []
    for user in users:
        state = challenge.standing(user)
        if only == "out" and not state.eliminated:
            continue
        if only == "at-risk" and (state.eliminated or not _at_risk(state)):
            continue
        if only == "met" and (state.eliminated or not _met_this_week(state)):
            continue
        rows.append({
            "user": user,
            "name": display_name(user),
            "standing": state,
            "at_risk": _at_risk(state),
        })

    # Out first, then those about to be, then everyone else: the list is read
    # top-down when someone is deciding who to chase.
    rows.sort(key=lambda row: (not row["standing"].eliminated, not row["at_risk"], row["name"].lower()))

    return render(request, "root/challenge.html", {
        "rows": rows,
        "week_indexes": range(1, weeks.week_count() + 1),
        "current_week": weeks.current_week(),
        "search": search,
        "show": only,
    })


def _at_risk(state):
    """Still in, but the live week isn't met yet — who to chase before Sunday."""
    live = state.current
    return bool(live and not live.met)


def _met_this_week(state):
    """The live week already has its hours, logged or saved."""
    live = state.current
    return bool(live and live.met)


@staff_member_required
@check_perms(CHALLENGE_PERMS)
def challenge_user(request, user_id):
    """One person's weeks, with the levers to change them."""
    User = get_user_model()
    target = get_object_or_404(User.objects.select_related("hackclub_profile"), id=user_id)
    outcomes = {row.week_index: row for row in WeekOutcome.objects.filter(user=target)}

    return render(request, "root/challenge_user.html", {
        "target": target,
        "name": display_name(target),
        "standing": challenge.standing(target),
        "outcomes": outcomes,
        "credits": (
            SaverCredit.objects.filter(user=target)
            .select_related("order", "granted_by")[:50]
        ),
        "overrides": WeekOutcome.Override.choices,
    })


@staff_member_required
@require_POST
@check_perms(CHALLENGE_PERMS)
def grant_saver(request, user_id):
    """Put saver hours on a week by hand, without charging anyone for them."""
    User = get_user_model()
    target = get_object_or_404(User, id=user_id)

    week_index, error = _week_arg(request)
    if error:
        messages.error(request, error)
        return redirect("challenge_user", user_id=user_id)

    try:
        hours = int(request.POST.get("hours", "").strip() or "0")
    except ValueError:
        messages.error(request, "Hours must be a whole number.")
        return redirect("challenge_user", user_id=user_id)

    # Bounded because each hour is a row, and a fat-fingered paste should not
    # be able to write a million of them.
    if not 1 <= hours <= weeks.WEEKLY_HOURS:
        messages.error(request, f"Grant between 1 and {weeks.WEEKLY_HOURS} hours at a time.")
        return redirect("challenge_user", user_id=user_id)

    note = request.POST.get("note", "").strip()[:200]
    was_out = challenge.standing(target).eliminated
    challenge.grant_saver_hours(target, week_index, hours, request.user, note)
    now_out = challenge.standing(target).eliminated

    record_audit(request, "grant_saver_hours", target=f"{display_name(target)} week {week_index}", metadata={
        "user_id": target.id,
        "week": week_index,
        "hours": hours,
        "note": note,
        "revived": was_out and not now_out,
    })
    messages.success(request, f"Granted {hours}h to week {week_index}.")
    return redirect("challenge_user", user_id=user_id)


@staff_member_required
@require_POST
@check_perms(CHALLENGE_PERMS)
def override_week(request, user_id):
    """Rule on a week directly — the last word over whatever the hours say."""
    User = get_user_model()
    target = get_object_or_404(User, id=user_id)

    week_index, error = _week_arg(request)
    if error:
        messages.error(request, error)
        return redirect("challenge_user", user_id=user_id)

    value = request.POST.get("override", "").strip()
    if value not in dict(WeekOutcome.Override.choices):
        messages.error(request, "Pick a valid override.")
        return redirect("challenge_user", user_id=user_id)

    note = request.POST.get("note", "").strip()[:200]
    outcome, _ = WeekOutcome.objects.get_or_create(
        user=target,
        week_index=week_index,
        defaults={"real_minutes": 0, "passed": False},
    )
    outcome.override = value
    outcome.override_by = request.user
    outcome.override_note = note
    outcome.save(update_fields=["override", "override_by", "override_note"])

    record_audit(request, "override_week", target=f"{display_name(target)} week {week_index}", metadata={
        "user_id": target.id,
        "week": week_index,
        "override": value or "cleared",
        "note": note,
    })
    messages.success(request, f"Week {week_index} override set.")
    return redirect("challenge_user", user_id=user_id)


def _week_arg(request):
    """The week index off a POST, or (None, reason)."""
    try:
        index = int(request.POST.get("week", "").strip() or "0")
    except ValueError:
        return None, "Week must be a whole number."
    if not 1 <= index <= weeks.week_count():
        return None, f"Week must be between 1 and {weeks.week_count()}."
    return index, None
