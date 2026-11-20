"""A reviewer's streak: how many decisions they've made in a row without
stopping, and the one-shot celebration the page after each decision plays.

Kept in the session rather than worked out from the review tables. A streak is
about this sitting at this desk, not a record anyone needs to keep, and the
session already knows which browser the reviewer is walking the queue in.

Every decision counts toward it (an approval, a rejection, a request for
changes, a timelapse sign-off), since each is a review done. A skip neither
adds to it nor breaks it. Only stopping does: STREAK_GAP without a decision and
the next one starts again at 1.
"""

from datetime import datetime, timedelta

from django.utils import timezone

# Long enough for a careful T3 review between two decisions, short enough that
# a coffee break ends the run.
STREAK_GAP = timedelta(minutes=20)
# The badge appears at SHOW_AT and catches fire at FIRE_AT.
SHOW_AT = 2
FIRE_AT = 3
# Past this the flames get bigger.
INFERNO_AT = 10
# Streaks that get a callout of their own when reached.
MILESTONES = (5, 10, 25, 50, 100)

SESSION_KEY = "review_streak"
CELEBRATE_KEY = "review_celebrate"


def _last_decision(state):
    try:
        return datetime.fromisoformat(state["last"])
    except (KeyError, TypeError, ValueError):
        return None


def current_streak(request, now=None):
    """The reviewer's running streak, or 0 once it has gone cold."""
    state = request.session.get(SESSION_KEY) or {}
    last = _last_decision(state)
    now = now or timezone.now()
    if last is None or now - last > STREAK_GAP:
        return 0
    return state.get("count", 0)


def record_decision(request, approved):
    """Count one decision toward the streak and queue its celebration.

    Called by each desk once its decision has committed, just before moving
    on to the next item. Returns the streak including this decision.
    """
    now = timezone.now()
    count = current_streak(request, now) + 1
    request.session[SESSION_KEY] = {"count": count, "last": now.isoformat()}
    request.session[CELEBRATE_KEY] = {"approved": bool(approved), "streak": count}
    return count


def pop_celebration(request):
    """The celebration owed for the last decision, once: None after that."""
    return request.session.pop(CELEBRATE_KEY, None)


def streak_tier(count):
    """The badge's look: hidden, plain, on fire, or an inferno."""
    if count >= INFERNO_AT:
        return "inferno"
    if count >= FIRE_AT:
        return "fire"
    if count >= SHOW_AT:
        return "warm"
    return None
