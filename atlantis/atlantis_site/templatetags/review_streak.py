"""The reviewer streak badge and the celebration played after a decision.

See views/admin/streak.py for what a streak is. The badge sits in a review
page's topbar; the celebration is loaded on every /root page, so finishing
the last item in a queue (and landing back on the desk) still gets one.
"""

from django import template
from django.templatetags.static import static
from django.utils.html import format_html, json_script

from ..views.admin.streak import FIRE_AT, MILESTONES, current_streak, pop_celebration, streak_tier

register = template.Library()


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
    return format_html(
        '{}<script src="{}" defer></script>',
        json_script(payload, "review-celebration"),
        static("atlantis_site/celebrate.js"),
    )
