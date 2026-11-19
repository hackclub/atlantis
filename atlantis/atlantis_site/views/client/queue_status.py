"""The public /queue/ page: how long each review queue is, and roughly how long
a ship that joins it now can expect to wait.

Only the shape of each queue leaves this module. Counts are rounded to the
nearest ten and waits to the nearest hour before they are cached or rendered,
so nothing on the page (or in its context) can be used to work out which
projects are in a queue, or that one particular project has just joined it.
The one exception is the fish costume counter (SHIP_GOAL), an exact total of
projects shipped since a fixed moment, which names no project.

The wait answers "if I join this desk now, how long until I'm through it":
everything ahead of you plus your own review, at the pace the desk works
*while it has something to work on*.

That last part is the whole estimate. Reviewers don't clear a desk at an even
rate: ships pile up before the weekly deadline, a review session clears them
in an evening, and the desk sits empty until the next pile. Dividing the
backlog by a week's worth of decisions spreads that work over the empty days
too, so a desk that cleared a hundred ships in one night reads as one that
manages fourteen a day, and a fresh pile of a hundred is told it will take a
week. Instead the rate is decisions per hour that something was waiting at the
desk (its busy hours, the union of every item's time in the queue), which is
how fast the queue actually drains once you are in it. Those busy hours include
nights and the gaps between sessions, so the estimate stays in wall-clock time.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from ...models import Journal, Ship, T1, T2, T3
from ..admin.queue import QUEUES, REVIEW_MODEL

# The desks in the order a ship passes through them, under the names a shipper
# would recognise. T3 is "final review" to the public, as Ship.ShipStatus
# already calls it.
PIPELINE = (
    ("lookout", "Timelapse review"),
    ("t1", "Project review"),
    ("t2", "YSWS review"),
    ("t3", "Final review"),
)

# A week: one full cycle of the weekly ship deadline, so the rate covers both
# the pile-up and the sessions that clear it.
THROUGHPUT_WINDOW = timedelta(days=7)

# The fish costume goal: unique projects shipped since 11 PM Eastern on
# Wednesday, November 11, 2026, counted up to SHIP_GOAL. Unlike the queue
# lengths this is an exact running total, so the bar moves with every ship.
SHIP_GOAL_SINCE = datetime(2026, 11, 11, 23, 0, tzinfo=ZoneInfo("America/New_York"))
SHIP_GOAL = 300

# Versioned: the cache is the database, so it outlives a deploy. Bump the
# suffix whenever the snapshot's shape changes, or the new template renders
# the old code's snapshot (with its new fields missing) until it expires.
CACHE_KEY = "public-queue-stats-v3"
CACHE_TTL = 5 * 60


def _clip_hours(intervals, since, now):
    """Hours inside [since, now] covered by at least one (start, end) interval."""
    spans = sorted(
        (max(start, since), min(end, now))
        for start, end in intervals
        if end > since and start < now
    )
    total = timedelta()
    run_start = run_end = None
    for start, end in spans:
        if end <= start:
            continue
        if run_end is None or start > run_end:
            if run_end is not None:
                total += run_end - run_start
            run_start, run_end = start, end
        else:
            run_end = max(run_end, end)
    if run_end is not None:
        total += run_end - run_start
    return total.total_seconds() / 3600


def _lookout_flow(since, now):
    """(pending, cleared, busy hours) for the timelapse desk, in projects.

    Only footage on a ship counts. The desk also reviews lapses from projects
    that haven't shipped, which nobody is queued behind, and counting those
    decisions while leaving their projects out of the backlog made the desk
    look faster than it is to a shipper. A lapse is waiting from the later of
    when it was written and when its ship was created; one signed off before
    its project shipped kept nobody waiting.
    """
    rows = (
        Journal.objects
        .filter(ship__isnull=False, project__deleted=False)
        .filter(Q(timelapse_review__isnull=True) | Q(timelapse_review__reviewed_at__gte=since))
        .values_list("project_id", "created_at", "ship__created_at", "timelapse_review__reviewed_at")
    )
    pending, cleared, intervals = set(), set(), []
    for project_id, written, shipped, reviewed in rows:
        waiting_from = max(written, shipped)
        if reviewed is None:
            pending.add(project_id)
            intervals.append((waiting_from, now))
        elif reviewed > waiting_from:
            cleared.add(project_id)
            intervals.append((waiting_from, reviewed))
    return len(pending), len(cleared), _clip_hours(intervals, since, now)


def _ship_entries(queue_key, ship_ids):
    """Every moment each ship could have (re)joined this desk, by ship id.

    A ship's visit to a desk starts at the latest of these before it left
    (or before now, if it's still there). The ship's own creation is always
    one, so every visit has a start.

    T1: the ship was created; its footage was signed off (a ship isn't in the
    T1 queue until it is); a later desk sent it back; or its owner resubmitted
    after changes were asked for. Resubmitting leaves no timestamp of its own,
    so the newest lapse on the ship stands in for it: a resubmission needs one.
    T2: approved at T1, or sent back from T3. T3: approved at T2.
    """
    entries = defaultdict(list)
    for ship_id, created in Ship.objects.filter(id__in=ship_ids).values_list("id", "created_at"):
        entries[ship_id].append(created)

    if queue_key == "t1":
        sources = [
            T2.objects.filter(decision=T2.Decision.RETURN_T1),
            T3.objects.filter(decision=T3.Decision.RETURN_T1),
        ]
        lapses = Journal.objects.filter(ship_id__in=ship_ids).values_list(
            "ship_id", "created_at", "timelapse_review__reviewed_at",
        )
        for ship_id, written, reviewed in lapses:
            entries[ship_id].append(written)
            if reviewed is not None:
                entries[ship_id].append(reviewed)
    elif queue_key == "t2":
        sources = [T1.objects.filter(approved=True), T3.objects.filter(decision=T3.Decision.RETURN_T2)]
    else:
        sources = [T2.objects.filter(decision=T2.Decision.APPROVE)]

    for source in sources:
        for ship_id, at in source.filter(ship_id__in=ship_ids).values_list("ship_id", "reviewed_at"):
            entries[ship_id].append(at)
    return entries


def _ship_desk_flow(queue_key, since, now):
    """(pending, cleared, busy hours) for a T1, T2 or T3 desk, in ships."""
    exits = list(
        REVIEW_MODEL[queue_key].objects
        .filter(reviewed_at__gte=since, ship__project__deleted=False)
        .values_list("ship_id", "reviewed_at")
    )
    pending_ids = list(QUEUES[queue_key].pending().values_list("id", flat=True))
    entries = _ship_entries(queue_key, {ship_id for ship_id, _ in exits} | set(pending_ids))

    def joined(ship_id, before):
        return max((at for at in entries[ship_id] if at <= before), default=before)

    intervals = [(joined(ship_id, left), left) for ship_id, left in exits]
    intervals += [(joined(ship_id, now), now) for ship_id in pending_ids]
    return len(pending_ids), len(exits), _clip_hours(intervals, since, now)


def desk_flow(queue_key, since, now):
    """How many are waiting at a desk, how many it decided since `since`,
    and for how many hours of that it had something waiting."""
    if queue_key == "lookout":
        return _lookout_flow(since, now)
    return _ship_desk_flow(queue_key, since, now)


def estimate_hours(pending, cleared, busy_hours):
    """Hours until a ship joining now is through: the `pending` ahead of it
    and its own review, at `cleared` decisions per `busy_hours`.

    0 for an empty desk with no recent work to take a pace from (nobody has
    been waiting there), None for a non-empty one (no rate to divide by).
    """
    if not cleared or busy_hours <= 0:
        return None if pending else 0.0
    return (pending + 1) * busy_hours / cleared


def total_hours(waits):
    """A ship shipped now goes through every desk in turn, so its wait is the
    sum of theirs; unknown if any of them is."""
    if any(wait is None for wait in waits):
        return None
    return sum(waits)


def rounded_count(count):
    """A count to the nearest ten, without ever rounding a non-empty queue to zero."""
    if count == 0:
        return "0"
    if count < 10:
        return "<10"
    return f"{(count + 5) // 10 * 10:,}"


def wait_display(hours):
    """An estimate to the nearest hour, or to the nearest day past two of them.

    "~312 hrs" makes the reader do the division, and implies a precision that
    a forecast a week out doesn't have.
    """
    if hours is None:
        return "—"
    if hours == 0:
        return "No wait"
    whole = int(hours + 0.5)
    if whole < 1:
        return "< 1 hr"
    if whole < 48:
        return f"~{whole} hr{'' if whole == 1 else 's'}"
    days = int(hours / 24 + 0.5)
    return f"~{days:,} days"


def ship_goal_progress(shipped, goal=SHIP_GOAL):
    """The counter's numbers: the total, the goal, and how full the bar is (0-100)."""
    return {
        "shipped": shipped,
        "goal": goal,
        "percent": min(100, round(shipped * 100 / goal)),
        "reached": shipped >= goal,
    }


def _projects_shipped_since(since):
    """Distinct live projects with at least one ship created at or after `since`."""
    return (
        Ship.objects
        .filter(created_at__gte=since, project__deleted=False)
        .values("project")
        .distinct()
        .count()
    )


def queue_snapshot():
    """Every desk's rounded count and wait, cached briefly.

    Only the rounded strings are cached, never the raw counts.
    """
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    now = timezone.now()
    since = now - THROUGHPUT_WINDOW
    queues, waits = [], []
    for step, (key, label) in enumerate(PIPELINE, start=1):
        pending, cleared, busy_hours = desk_flow(key, since, now)
        wait = estimate_hours(pending, cleared, busy_hours)
        waits.append(wait)
        queues.append({
            "step": step,
            "label": label,
            "count": rounded_count(pending),
            "empty": pending == 0,
            "wait": wait_display(wait),
        })

    snapshot = {
        "queues": queues,
        # Only worth a sentence when there's an actual wait to name.
        "total_wait": wait_display(total) if (total := total_hours(waits)) else None,
        "ship_goal": ship_goal_progress(_projects_shipped_since(SHIP_GOAL_SINCE)),
        "updated_at": timezone.now(),
    }
    cache.set(CACHE_KEY, snapshot, CACHE_TTL)
    return snapshot


def queue_status(request):
    return render(request, "atlantis_site/queue_status.html", {
        **queue_snapshot(),
        "refresh_minutes": CACHE_TTL // 60,
    })
