"""The reviewer streak badges and the celebration played after a decision.

See views/admin/streak.py for what the in-a-row streak is, and daily_streak.py
for the daily one. Both badges sit in a review page's topbar, and the daily one
is on the /root home page too. The celebration is loaded on every /root page,
so finishing the last item in a queue (and landing back on the desk) still
gets one.
"""

from django import template
from django.templatetags.static import static
from django.utils.html import format_html, format_html_join, json_script

from ..daily_streak import can_keep_streak, daily_streak
from ..views.admin.streak import FIRE_AT, MILESTONES, current_streak, pop_celebration, streak_tier

register = template.Library()


def _daily_title(streak):
    where = f"Your day ends at midnight {streak.zone_name} time."
    if streak.done_today:
        return f"{streak.days}-day review streak. Today's {streak.goal} reviews are done! {where}"
    if streak.days:
        return (
            f"{streak.days}-day review streak. {streak.today} of today's {streak.goal} "
            f"reviews done: {streak.left} more keeps it going. {where}"
        )
    return (
        f"Do {streak.goal} reviews today to start a daily streak "
        f"({streak.today} so far). {where}"
    )


@register.simple_tag(takes_context=True)
def daily_streak_badge(context, streak=None):
    """The daily streak: days kept, and a pip for each of today's reviews.

    Takes the streak when the view already worked it out, so the home page
    doesn't count it twice.
    """
    request = context.get("request")
    if request is None or not request.user.is_authenticated:
        return ""
    if streak is None:
        if not can_keep_streak(request.user):
            return ""
        streak = daily_streak(request.user)

    if streak.done_today:
        state = "done"
    elif streak.days:
        state = "risk"
    else:
        state = "idle"
    pips = format_html_join(
        "", '<i class="{}"></i>', (("is-on" if on else "",) for on in streak.pips)
    )
    days = (
        format_html('<span class="rv-daily-days"><strong>{}</strong>d</span>', streak.days)
        if streak.days else ""
    )
    return format_html(
        '<span class="rv-daily rv-daily--{}" data-daily-streak="{}" title="{}">'
        '{}<span class="rv-daily-pips" aria-hidden="true">{}</span>'
        '<span class="rv-daily-count">{}/{}</span></span>',
        state, streak.days, _daily_title(streak),
        days, pips, min(streak.today, streak.goal), streak.goal,
    )


@register.simple_tag(takes_context=True)
def streak_badge(context):
    """The topbar's streak, or nothing until there's a run worth showing."""
    request = context.get("request")
    if request is None or not request.user.is_authenticated:
        return ""
    count = current_streak(request)
    tier = streak_tier(count)
    if tier is None:
        return ""
    return format_html(
        '<span class="rv-streak rv-streak--{}" data-review-streak="{}" '
        'title="{} reviews in a row. Keep going!">'
        '<span class="rv-streak-flame" aria-hidden="true">&#128293;</span>'
        '<strong>{}</strong> streak</span>',
        tier, count, count, count,
    )


@register.simple_tag(takes_context=True)
def review_celebration(context):
    """The data and script for the effect owed by the last decision, if any.

    Popped here, so it plays on exactly one page: whichever the decision
    redirected to.
    """
    request = context.get("request")
    if request is None or not request.user.is_authenticated:
        return ""
    celebration = pop_celebration(request)
    if not celebration:
        return ""
    streak = celebration.get("streak", 0)
    payload = {
        "approved": bool(celebration.get("approved")),
        "streak": streak,
        "onFire": streak >= FIRE_AT,
        "milestone": streak in MILESTONES,
    }
    # Only on the decision that met the day's goal: the daily streak it made.
    if celebration.get("daily"):
        payload["dailyStreak"] = celebration["daily"]
    return format_html(
        '{}<script src="{}" defer></script>',
        json_script(payload, "review-celebration"),
        static("atlantis_site/celebrate.js"),
    )
