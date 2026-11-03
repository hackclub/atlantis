"""The public /queue/ page: how long each review queue is, and roughly how long
a ship that joins it now can expect to wait.

Only the shape of each queue leaves this module. Counts are rounded to the
nearest ten and waits to the nearest hour before they are cached or rendered,
so nothing on the page (or in its context) can be used to work out which
projects are in a queue, or that one particular project has just joined it.

The wait is the backlog divided by the rate the desk has been clearing it:
"at the pace of the last week, how long until the reviewers get through
everything ahead of you". That answers the question a shipper is asking about
*now*, where a percentile of past waits would describe a queue that has since
grown or shrunk.
"""

from datetime import timedelta

from django.core.cache import cache
from django.shortcuts import render
from django.utils import timezone

from ...models import Project
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

# A week, so a quiet weekend or one big review session doesn't swing the
# estimate by a factor of three.
THROUGHPUT_WINDOW = timedelta(days=7)

CACHE_KEY = "public-queue-stats"
CACHE_TTL = 5 * 60


def _pending(queue_key):
    """How many projects are waiting at this desk.

    The timelapse desk also holds lapses from projects that haven't shipped
    yet, which nobody is waiting on; its count here is the projects with an
    unreviewed lapse on a ship, the ones a shipper is actually queued behind
    (the desk's own default "Shipped" tab).
    """
    if queue_key == "lookout":
        return (
            Project.objects
            .filter(
                deleted=False,
                journals__ship__isnull=False,
                journals__timelapse_review__isnull=True,
            )
            .distinct()
            .count()
        )
    return QUEUES[queue_key].pending().count()


def _cleared(queue_key, since):
    """How many projects this desk has decided on since `since`.

    The timelapse desk writes one review per lapse but works a project in one
    sitting, so it is counted in projects to match what _pending counts.
    """
    reviews = REVIEW_MODEL[queue_key].objects.filter(reviewed_at__gte=since)
    if queue_key == "lookout":
        return reviews.values("journal__project").distinct().count()
    return reviews.count()


def estimate_hours(pending, cleared, window=THROUGHPUT_WINDOW):
    """Hours to clear `pending` at the rate of `cleared` per `window`.

    0 for an empty queue, None when the desk hasn't decided anything in the
    window and so has no rate to divide by.
    """
    if not pending:
        return 0.0
    if not cleared:
        return None
    per_hour = cleared / (window.total_seconds() / 3600)
    return pending / per_hour


def rounded_count(count):
    """A count to the nearest ten, without ever rounding a non-empty queue to zero."""
    if count == 0:
        return "0"
    if count < 10:
        return "<10"
    return f"{(count + 5) // 10 * 10:,}"


def wait_display(hours):
    """An estimate in hours, rounded to the nearest hour."""
    if hours is None:
        return "—"
    if hours == 0:
        return "No wait"
    whole = int(hours + 0.5)
    if whole < 1:
        return "< 1 hr"
    return f"~{whole:,} hr{'' if whole == 1 else 's'}"


def queue_snapshot():
    """Every desk's rounded count and wait, cached briefly.

    Only the rounded strings are cached, never the raw counts.
    """
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached

    since = timezone.now() - THROUGHPUT_WINDOW
    queues = []
    for step, (key, label) in enumerate(PIPELINE, start=1):
        pending = _pending(key)
        wait = wait_display(estimate_hours(pending, _cleared(key, since)))
        queues.append({
            "step": step,
            "label": label,
            "count": rounded_count(pending),
            "empty": pending == 0,
            "wait": wait,
        })

    snapshot = {"queues": queues, "updated_at": timezone.now()}
    cache.set(CACHE_KEY, snapshot, CACHE_TTL)
    return snapshot


def queue_status(request):
    return render(request, "atlantis_site/queue_status.html", {
        **queue_snapshot(),
        "refresh_minutes": CACHE_TTL // 60,
    })
