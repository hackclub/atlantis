"""Where organizers watch the streaks and, when they have to, overrule them.

Two pages. The roster is every participant against every week, so "who is out
and why" is one glance rather than a query; it also lists, and exports, who
the two-hour buffer after each week put back in. The per-user page is where a wrong
call gets fixed: grant saver hours, take hours off a week, force a week to
pass or fail outright, or give someone extra days on the last week.

Every one of those is a thumb on the scale, so all of them are audit-logged
with the reason the organizer typed.
"""

import csv
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ... import challenge, weeks
from ...models import FinalWeekExtension, HourRemoval, SaverCredit, WeekOutcome
from ...models import Profile
from ..helpers import (
    check_perms, display_name, record_audit, run_in_background, send_slack_dm,
    staff_member_required,
)

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
        # Rows are labelled with display_name, which prefers the Slack name,
        # so a search has to match that as well as the account username.
        users = users.filter(
            Q(username__icontains=search)
            | Q(hackclub_profile__slack_username__icontains=search)
        )

    users = list(users)
    states = challenge.standings_for([user.id for user in users])
    rescued = challenge.rescued_by_buffer()
    rows = []
    for user in users:
        state = states[user.id]
        if only == "out" and not state.eliminated:
            continue
        if only == "buffer" and user.id not in rescued:
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
            "buffer_weeks": _buffer_weeks(*rescued[user.id]) if user.id in rescued else [],
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
        "rescued_count": len(rescued),
    })


def _buffer_weeks(state, without):
    """The weeks the buffer saved, from rescued_by_buffer's pair."""
    return [week.index for week in without.missed if not state.weeks[week.index - 1].missed]


@staff_member_required
@check_perms(CHALLENGE_PERMS)
def buffer_rescued_csv(request):
    """Everyone the buffer put back in the program, for the mass-DM script.

    The columns are unfinished_hours_csv's, which scripts/dm_unfinished_hours.py
    reads: `week` is the weeks the buffer saved, `progress` what each of them
    has now, and `left` is nothing, since that's what saved them. One row per
    person however many weeks it saved, so nobody is DMed twice, and nobody
    banned, who isn't back in anything.
    """
    rescued = challenge.rescued_by_buffer()

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="back_in_from_buffer.csv"'
    writer = csv.writer(response)
    writer.writerow(["slack_id", "username", "user_id", "week", "progress", "left"])

    profiles = (
        Profile.objects.select_related("user")
        .filter(user_id__in=rescued.keys(), banned=False)
        .order_by("user_id")
    )
    for profile in profiles:
        state, without = rescued[profile.user_id]
        saved = [state.weeks[i - 1] for i in _buffer_weeks(state, without)]
        writer.writerow([
            profile.slack_id,
            profile.slack_username or profile.user.username,
            profile.user_id,
            ", ".join(str(week.index) for week in saved),
            ", ".join(week.credited_display for week in saved),
            challenge.format_minutes(0),
        ])

    record_audit(request, "export_buffer_rescued", metadata={"count": profiles.count()})
    return response


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
    state = challenge.standing(target)

    return render(request, "root/challenge_user.html", {
        "target": target,
        "name": display_name(target),
        "standing": state,
        "outcomes": outcomes,
        "credits": (
            SaverCredit.objects.filter(user=target)
            .select_related("order", "granted_by")[:50]
        ),
        "removals": (
            HourRemoval.objects.filter(user=target)
            .select_related("removed_by")[:50]
        ),
        "overrides": WeekOutcome.Override.choices,
        "final_week": weeks.week_count(),
        "extension": (
            FinalWeekExtension.objects.filter(user=target).select_related("granted_by").first()
        ),
        "max_extension_days": weeks.max_extension_days(),
        # Formatted here, in the program's zone: TIME_ZONE is UTC, so a
        # template's |date would put 11:59 PM Eastern on the next day.
        "usual_final_deadline": _eastern(weeks.deadline(weeks.week_count())),
        "final_deadline": _eastern(state.weeks[-1].deadline),
    })


def _eastern(moment):
    local = moment.astimezone(weeks.zone())
    return f"{local:%a %b} {local.day} at {local:%-I:%M %p}"


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
def remove_hours(request, user_id):
    """Take hours off a week, the way grant_saver puts them on."""
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

    before = challenge.standing(target)
    week = before.weeks[week_index - 1]
    # Up to what the week has, a part-hour rounded up, so a slip of the
    # keyboard can't bury a week under hours it would take a month to log.
    available = -(-week.credited_minutes // 60)
    if not available:
        messages.error(request, f"Week {week_index} has no hours to remove.")
        return redirect("challenge_user", user_id=user_id)
    if not 1 <= hours <= available:
        messages.error(request, f"Remove between 1 and {available} hours from week {week_index}.")
        return redirect("challenge_user", user_id=user_id)

    note = request.POST.get("note", "").strip()[:200]
    removal = challenge.remove_hours(target, week_index, hours, request.user, note)
    now_out = challenge.standing(target).eliminated

    record_audit(request, "remove_week_hours", target=f"{display_name(target)} week {week_index}", metadata={
        "user_id": target.id,
        "removal_id": removal.id,
        "week": week_index,
        "hours": hours,
        "note": note,
        "eliminated": now_out and not before.eliminated,
    })
    messages.success(request, f"Removed {hours}h from week {week_index}.")
    return redirect("challenge_user", user_id=user_id)


@staff_member_required
@require_POST
@check_perms(CHALLENGE_PERMS)
def undo_hour_removal(request, user_id, removal_id):
    """Give back hours a removal took, by deleting it."""
    removal = get_object_or_404(HourRemoval.objects.select_related("user"), id=removal_id, user_id=user_id)
    target = removal.user
    week_index, hours = removal.week_index, removal.hours
    removal.delete()

    record_audit(request, "undo_hour_removal", target=f"{display_name(target)} week {week_index}", metadata={
        "user_id": target.id,
        "removal_id": removal_id,
        "week": week_index,
        "hours": hours,
    })
    messages.success(request, f"Gave {hours}h back to week {week_index}.")
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


@staff_member_required
@require_POST
@check_perms(CHALLENGE_PERMS)
def extend_final_week(request, user_id):
    """Give one person extra days on the last week, change how many, or take
    them away with 0.

    Their last week then ends that many days after everyone else's, with the
    usual two-hour buffer after it: time they record in those days counts
    towards it, it can't be missed until then, and printers wait for them
    until it has settled. It can be given after their week has closed, which
    reopens it — the way to rescue someone who had an emergency over the
    deadline. Capped by weeks.max_extension_days so there's still a day left
    to ship the time in.
    """
    User = get_user_model()
    target = get_object_or_404(User, id=user_id)
    final = weeks.week_count()

    try:
        days = int(request.POST.get("days", "").strip())
    except ValueError:
        messages.error(request, "Days must be a whole number.")
        return redirect("challenge_user", user_id=user_id)

    most = weeks.max_extension_days()
    if not 0 <= days <= most:
        messages.error(request, f"Extend week {final} by 1 to {most} days, or 0 to take it away.")
        return redirect("challenge_user", user_id=user_id)

    note = request.POST.get("note", "").strip()[:200]
    before = challenge.extension_days(target)
    was_out = challenge.standing(target).eliminated
    if days:
        FinalWeekExtension.objects.update_or_create(
            user=target, defaults={"days": days, "granted_by": request.user, "note": note},
        )
    else:
        FinalWeekExtension.objects.filter(user=target).delete()
    now_out = challenge.standing(target).eliminated

    ends = weeks.deadline(final, days).astimezone(weeks.zone())
    record_audit(request, "extend_final_week", target=f"{display_name(target)} week {final}", metadata={
        "user_id": target.id,
        "week": final,
        "days": days,
        "previous_days": before,
        "deadline": ends.isoformat(),
        "note": note,
        "revived": was_out and not now_out,
    })
    # Told only when the deadline actually moved: re-saving the same number
    # with a better note is bookkeeping, not news. The note stays internal.
    profile = getattr(target, "hackclub_profile", None)
    if days != before and profile is not None and profile.slack_id:
        run_in_background(send_slack_dm, _extension_dm(final, days, ends), profile.slack_id)

    if days:
        messages.success(
            request,
            f"Week {final} now ends {_eastern(ends)} Eastern "
            f"for {display_name(target)} (+{days} day{'s' if days != 1 else ''}).",
        )
    else:
        messages.success(request, f"Took away {display_name(target)}'s week {final} extension.")
    return redirect("challenge_user", user_id=user_id)


def _extension_dm(final, days, ends):
    """What someone is told when their last week's deadline moves."""
    if not days:
        return (
            f"Your extension on week {final} of Atlantis has been removed, so week {final} "
            f"ended {_eastern(ends)} Eastern, the same as everyone else's."
        )
    ship_by = _eastern(weeks.shipping_closes_at() - timedelta(seconds=1))
    return (
        f"An organizer gave you {days} extra day{'s' if days != 1 else ''} on week {final} "
        f"of Atlantis: it now ends {_eastern(ends)} Eastern for you. Time you record "
        f"until then counts towards week {final}, and towards your 40 printer hours once "
        f"it's shipped. Shipping closes {ship_by} Eastern, so ship it before then. "
        "https://atlantis.hackclub.com/dashboard/"
    )


def _week_arg(request):
    """The week index off a POST, or (None, reason)."""
    try:
        index = int(request.POST.get("week", "").strip() or "0")
    except ValueError:
        return None, "Week must be a whole number."
    if not 1 <= index <= weeks.week_count():
        return None, f"Week must be between 1 and {weeks.week_count()}."
    return index, None
