"""Queue navigation shared by the four review desks.

Every desk (T1, T2, T3 and Lookout) is the same shape: an ordered list of
things waiting, one of which a reviewer is looking at right now. This module is
what makes that shape explicit, so a reviewer can walk the queue — open the
oldest, decide, land on the next one — without going back to a dashboard
between every item.

Two ideas do most of the work:

*Position.* A review page knows where it sits in its queue ("4 of 17") and what
comes next, so "next" is a link that already exists rather than a search the
reviewer performs by eye.

*Claims.* Opening a review takes a short lease on it, held in the cache rather
than the database: a claim is a fact about right now, it expires on its own,
and nothing about it is worth keeping once it has. Two reviewers can still
force their way onto the same item — the lease only steers `next` past it and
warns whoever arrives second — because the real guard against a double decision
is the status check inside each decision's transaction, and that has to stay
the thing that's authoritative.
"""

from dataclasses import dataclass

from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.db.models import Exists, OuterRef, Prefetch, Subquery
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ...models import (
    Journal, LookoutSession, Project, Ship, T1, T2, T3, TimelapseAnnotation,
    TimelapseReview,
)
from ..helpers import (
    approved_minutes_for_journals, display_name, format_minutes,
    timelapse_cleared_ships, tracked_minutes_for_journals,
)

# How long a claim survives without a heartbeat. Long enough to read a project
# and write feedback, short enough that a reviewer who closed the tab isn't
# still holding the queue up ten minutes later.
CLAIM_TTL = 6 * 60
# The review page beats faster than the lease expires, so one dropped request
# doesn't cost the reviewer their claim mid-sentence.
CLAIM_HEARTBEAT_SECONDS = 90

# Days a ship may sit in a queue before its row is called out as overdue. These
# are display thresholds only — nothing enforces them — and they get shorter as
# the ship moves up the ladder, because time spent late in the pipeline is time
# the shipper has already waited through once.
QUEUE_SLA_DAYS = {"t1": 4, "t2": 3, "t3": 2, "lookout": 3}


SHIP_STATUS_FOR = {
    "t1": Ship.ShipStatus.T1_QUEUE,
    "t2": Ship.ShipStatus.T2_QUEUE,
    "t3": Ship.ShipStatus.T3_QUEUE,
}


def project_lapse_prefetch():
    """The unreviewed lapses of a project, oldest first, footage attached.

    The desk lists every queued project's lapses under it, so they load as two
    extra queries for the whole page rather than two per project.
    """
    return Prefetch(
        "journals",
        queryset=(
            Journal.objects
            .filter(timelapse_review__isnull=True)
            .prefetch_related("timelapses")
            .order_by("created_at", "id")
        ),
        to_attr="pending_lapses",
    )


@dataclass(frozen=True)
class Queue:
    """One review desk: what's in it, and the URLs for moving through it."""

    key: str
    label: str
    short: str
    dash: str
    detail: str
    next_url: str
    accent: str
    noun: str

    @property
    def sla_days(self):
        return QUEUE_SLA_DAYS[self.key]

    def dash_url(self):
        return reverse(self.dash)

    def detail_url(self, item_id):
        return reverse(self.detail, args=[item_id])

    def pending(self):
        """The queue itself, in the order it should be worked: oldest first.

        Oldest-first and not newest-first, unlike the tables this replaces. A
        queue worked newest-first starves its tail, and the tail is exactly the
        set of people who have been waiting longest.
        """
        if self.key == "lookout":
            # A project, not a lapse. Every lapse on a project is the same
            # footage of the same person working on the same thing, and judging
            # them one at a time meant re-learning that context on every visit.
            # One project is one sitting: all of its lapses, all of their
            # Lookouts, one decision.
            pending_lapses = Journal.objects.filter(
                project=OuterRef("pk"), timelapse_review__isnull=True
            )
            return (
                Project.objects
                .filter(deleted=False)
                .filter(Exists(pending_lapses))
                .select_related("owner", "owner__hackclub_profile")
                .prefetch_related(project_lapse_prefetch())
                .annotate(waiting_since=Subquery(
                    pending_lapses.order_by("created_at", "id").values("created_at")[:1]
                ))
                .order_by("waiting_since", "id")
            )
        base = Ship.objects.filter(status=SHIP_STATUS_FOR[self.key], project__deleted=False)
        if self.key == "t1":
            # A ship whose lapses haven't been signed off isn't reviewable yet,
            # so it isn't in the queue — same rule review_dash has always used.
            base = timelapse_cleared_ships(base)
        return (
            base
            .select_related("project", "project__owner", "project__owner__hackclub_profile")
            .order_by("created_at", "id")
        )


QUEUES = {
    "t1": Queue(
        key="t1", label="Project review", short="T1", accent="tide", noun="ship",
        dash="review_dash", detail="review_project", next_url="review_next",
    ),
    "t2": Queue(
        key="t2", label="YSWS review", short="T2", accent="lagoon", noun="ship",
        dash="ysws_review_dash", detail="ysws_review_project", next_url="ysws_review_next",
    ),
    "t3": Queue(
        key="t3", label="Fraud review", short="T3", accent="warn", noun="ship",
        dash="fraud_review_dash", detail="fraud_review_project", next_url="fraud_review_next",
    ),
    "lookout": Queue(
        key="lookout", label="Lookout review", short="Lookout", accent="sand", noun="project",
        dash="timelapse_review_dash", detail="timelapse_review_project",
        next_url="timelapse_review_next",
    ),
}


# ---------------------------------------------------------------- claims

def _claim_key(queue_key, item_id):
    return f"review-claim:{queue_key}:{item_id}"


def _holder_key(user_id):
    return f"review-claim-of:{user_id}"


def claim_holder(queue_key, item_id):
    """Who holds this item right now, or None. `{"user_id", "name"}`."""
    return cache.get(_claim_key(queue_key, item_id))


def claim_review(queue_key, item_id, user):
    """Take (or renew) the lease on an item, dropping whatever this user held.

    Returns True when the caller owns the item afterwards. One claim per
    reviewer, across every desk: a reviewer is only ever looking at one thing,
    and a stale claim left behind on another desk holds up a queue nobody can
    see them in.
    """
    holder = {"user_id": user.id, "name": display_name(user)}
    key = _claim_key(queue_key, item_id)
    release_claim(user, keep=(queue_key, item_id))

    if cache.add(key, holder, CLAIM_TTL):
        cache.set(_holder_key(user.id), [queue_key, item_id], CLAIM_TTL)
        return True

    existing = cache.get(key)
    if existing and existing.get("user_id") == user.id:
        # Ours already — a heartbeat, or a reload. Push the expiry back out.
        cache.set(key, holder, CLAIM_TTL)
        cache.set(_holder_key(user.id), [queue_key, item_id], CLAIM_TTL)
        return True
    return False


def release_claim(user, keep=None):
    """Drop this user's claim, unless it's already on `keep`."""
    held = cache.get(_holder_key(user.id))
    if not held:
        return False
    queue_key, item_id = held[0], held[1]
    if keep and list(keep) == [queue_key, item_id]:
        return False
    existing = cache.get(_claim_key(queue_key, item_id))
    if existing and existing.get("user_id") == user.id:
        cache.delete(_claim_key(queue_key, item_id))
    cache.delete(_holder_key(user.id))
    return True


def claims_for(queue_key, item_ids):
    """`{item_id: holder}` for the rows on one page of a queue."""
    keys = {_claim_key(queue_key, item_id): item_id for item_id in item_ids}
    if not keys:
        return {}
    found = cache.get_many(list(keys))
    return {keys[key]: holder for key, holder in found.items() if holder}


# ------------------------------------------------------------ navigation

def parse_skip(request):
    """The `?skip=` list: items this reviewer has passed over this session."""
    raw = request.GET.get("skip", "")
    ids = []
    for part in raw.split(",")[:200]:
        part = part.strip()
        if part.isdigit():
            value = int(part)
            if value not in ids:
                ids.append(value)
    return ids


def skip_param(skip_ids):
    return ",".join(str(i) for i in skip_ids)


def next_item_id(queue_key, skip_ids=(), user=None):
    """The next item to work: first unskipped, unclaimed thing in the queue."""
    queue = QUEUES[queue_key]
    ids = list(queue.pending().values_list("id", flat=True))
    claims = claims_for(queue_key, ids)
    for item_id in ids:
        if item_id in skip_ids:
            continue
        holder = claims.get(item_id)
        if holder and (user is None or holder.get("user_id") != user.id):
            continue
        return item_id
    return None


def go_to_next(request, queue_key, skip_ids=(), empty_message=None):
    """Send the reviewer to the next item, or back to the desk when it's clear."""
    from django.contrib import messages

    queue = QUEUES[queue_key]
    # Whatever the reviewer was holding is done with — released before the
    # search so a re-entrant "next" can't hand them back the item they just
    # decided on.
    release_claim(request.user)
    item_id = next_item_id(queue_key, skip_ids, request.user)
    if item_id is None:
        release_claim(request.user)
        messages.info(request, empty_message or f"Nothing else waiting in {queue.label.lower()}.")
        return redirect(queue.dash)
    url = queue.detail_url(item_id)
    if skip_ids:
        url = f"{url}?skip={skip_param(skip_ids)}"
    return redirect(url)


# --------------------------------------------------------------- context

def age_bucket(created_at, sla_days):
    """How overdue something is, as a class name the CSS can colour."""
    days = (timezone.now() - created_at).total_seconds() / 86400
    if days >= sla_days:
        return "overdue"
    if days >= sla_days / 2:
        return "aging"
    return "fresh"


def age_display(created_at):
    """Compact wait, in the largest unit that isn't a lie: `4d`, `7h`, `12m`.

    Reads as a duration wherever it lands, including mid-sentence — so
    something that has only just arrived is `<1m` rather than a word.
    """
    seconds = max((timezone.now() - created_at).total_seconds(), 0)
    if seconds >= 86400:
        return f"{int(seconds // 86400)}d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60:
        return f"{int(seconds // 60)}m"
    return "<1m"


def decorate_rows(queue_key, items):
    """Annotate a queue's rows with everything its table shows.

    Returns a list, deliberately: the callers all iterate it more than once
    (count, then render) and the annotations are per-object.
    """
    queue = QUEUES[queue_key]
    rows = list(items)
    claims = claims_for(queue_key, [row.id for row in rows])
    for index, row in enumerate(rows, start=1):
        row.queue_index = index
        row.claim = claims.get(row.id)
        # A lookout row is a project, and a project has been waiting since its
        # oldest unreviewed lapse — not since the project was created.
        waiting = getattr(row, "waiting_since", None) or row.created_at
        row.age_display = age_display(waiting)
        row.age_bucket = age_bucket(waiting, queue.sla_days)
        if queue_key == "lookout":
            decorate_lapses(row.pending_lapses, queue.sla_days)
            row.lapses = row.pending_lapses
            row.lapse_count = len(row.lapses)
            row.lookout_count = sum(len(lapse.timelapses.all()) for lapse in row.lapses)
            row.tracked_seconds_total = sum(
                lapse.tracked_seconds_total for lapse in row.lapses
            )
            row.tracked_label = format_minutes(row.tracked_seconds_total // 60)
            # The ships this project can't get into T1 until the queue clears it.
            row.held_ships = sorted({
                lapse.ship_id for lapse in row.lapses if lapse.ship_id
            })
        else:
            row.time_spent_display = format_minutes(
                approved_minutes_for_journals(row.project.journals.all())
            )
    return rows


def decorate_lapses(lapses, sla_days):
    """Annotate a project's lapses with what the desk and the review page show."""
    for lapse in lapses:
        # Journal.tracked_seconds re-aggregates per row and ignores the
        # prefetch; the sessions are already in memory, so count them there.
        lapse.tracked_seconds_total = sum(
            session.tracked_seconds for session in lapse.timelapses.all()
        )
        lapse.tracked_label = format_minutes(lapse.tracked_seconds_total // 60)
        lapse.lookout_count = len(lapse.timelapses.all())
        lapse.age_display = age_display(lapse.created_at)
        lapse.age_bucket = age_bucket(lapse.created_at, sla_days)
    return lapses


def queue_stats(queue_key, rows, user=None, extra=()):
    """The four numbers a desk's header carries.

    Everything here is read off `rows`, which the dash already has in memory,
    so the header costs one extra query at most (the reviewer's own count) no
    matter how long the queue is.
    """
    queue = QUEUES[queue_key]
    oldest = rows[0] if rows else None  # rows are oldest-first
    overdue = sum(1 for row in rows if row.age_bucket == "overdue")

    stats = [
        {"label": "Waiting", "value": str(len(rows)), "sub": queue.noun + ("" if len(rows) == 1 else "s")},
        *extra,
        {
            "label": "Oldest",
            "value": oldest.age_display if oldest else "—",
            "sub": f"SLA {queue.sla_days}d",
            "tone": "bad" if oldest and oldest.age_bucket == "overdue" else "",
        },
        {
            "label": "Overdue",
            "value": str(overdue),
            "sub": f"past {queue.sla_days}d",
            "tone": "bad" if overdue else "good",
        },
    ]
    if user is not None:
        stats.append({
            "label": "Your reviews today",
            "value": str(reviews_today(queue_key, user)),
            "sub": "since midnight",
        })
    return stats


def reviews_today(queue_key, user):
    model = {"t1": T1, "t2": T2, "t3": T3, "lookout": TimelapseReview}[queue_key]
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return model.objects.filter(reviewer=user, reviewed_at__gte=start).count()


def review_context(request, queue_key, item, claimable=True, waiting_since=None):
    """Everything the reviewer shell needs to place one item in its queue.

    Claiming happens here rather than in each view, because every path onto a
    review page goes through it. `claimable` is False for a page that can no
    longer be decided — an already-reviewed lapse, a ship that has moved on —
    where a lease would hold the queue up for nothing.
    """
    queue = QUEUES[queue_key]
    skip_ids = parse_skip(request)
    ids = list(queue.pending().values_list("id", flat=True))

    holder = claim_holder(queue_key, item.id)
    if claimable:
        mine = claim_review(queue_key, item.id, request.user)
    else:
        release_claim(request.user)
        mine = True
    # Someone else's lease: they keep it, and this reviewer is told rather than
    # bounced, so a deliberate visit (a link from Slack, say) still works.
    conflict = None if mine else holder

    try:
        position = ids.index(item.id) + 1
    except ValueError:
        position = None

    next_url = reverse(queue.next_url)
    forward_skip = skip_param(skip_ids)
    skip_url = reverse(queue.next_url) + f"?skip={skip_param(skip_ids + [item.id])}"
    if forward_skip:
        next_url = f"{next_url}?skip={forward_skip}"

    return {
        "queue": queue,
        "queue_key": queue_key,
        "queue_label": queue.label,
        "queue_short": queue.short,
        "queue_accent": queue.accent,
        "queue_dash_url": queue.dash_url(),
        "queue_total": len(ids),
        "queue_position": position,
        "queue_remaining": max(len(ids) - (position or len(ids)), 0),
        "skip_url": skip_url,
        "next_url": next_url,
        "skip_value": forward_skip,
        "claim_conflict": conflict,
        "claim_held": claimable,
        "heartbeat_url": reverse("review_heartbeat", args=[queue_key, item.id]),
        "heartbeat_seconds": CLAIM_HEARTBEAT_SECONDS,
        "age_display": age_display(waiting_since or item.created_at),
        "age_bucket": age_bucket(waiting_since or item.created_at, queue.sla_days),
    }


def dash_context(request, queue_key, rows, extra_stats=()):
    """The header a desk shows above its table."""
    queue = QUEUES[queue_key]
    release_claim(request.user)  # Back at the desk: not reviewing anything.
    start_url = reverse(queue.next_url)
    return {
        "queue": queue,
        "queue_key": queue_key,
        "queue_label": queue.label,
        "queue_short": queue.short,
        "queue_accent": queue.accent,
        "queue_stats": queue_stats(queue_key, rows, request.user, extra_stats),
        "start_url": start_url,
        "has_pending": bool(rows),
    }


def owner_snapshot(user):
    """What a reviewer wants to know about the person behind the ship.

    Shipped/finalized/rejected counts and pearls, so a decision can be made in
    the light of what this person has shipped before without opening their
    profile in another tab.
    """
    profile = getattr(user, "hackclub_profile", None)
    ships = Ship.objects.filter(project__owner=user)
    finalized = ships.filter(status=Ship.ShipStatus.FINALIZED).count()
    rejected = ships.filter(status=Ship.ShipStatus.REJECTED).count()
    return {
        "user": user,
        "name": display_name(user),
        "slack_id": profile.slack_id if profile else "",
        "avatar": profile.slack_pfp_url if profile else "",
        "pearls": profile.layers if profile else 0,
        "projects": user.projects.filter(deleted=False).count(),
        "ships": ships.count(),
        "finalized": finalized,
        "rejected": rejected,
        "verified": bool(profile and profile.is_ysws_eligible),
    }


# ----------------------------------------------------------------- views

# Who may hold a claim on each desk. Same lists the desks' own views enforce —
# the heartbeat is only a lease renewal, but it names an item and a queue, so
# it answers to the same permissions the page behind it does.
QUEUE_PERMS = {
    "t1": [
        "atlantis_site.t1_review", "atlantis_site.t2_review",
        "atlantis_site.t3_review", "atlantis_site.organizer",
    ],
    "t2": [
        "atlantis_site.t2_review", "atlantis_site.t3_review",
        "atlantis_site.organizer",
    ],
    "t3": ["atlantis_site.t3_review", "atlantis_site.organizer"],
    "lookout": ["atlantis_site.timelapse_review", "atlantis_site.organizer"],
}


@require_POST
@staff_member_required
def review_heartbeat(request, queue_key, item_id):
    """Renew the open review's lease. Called by the page every 90 seconds.

    409 is the interesting answer: someone else holds the item now, and the
    reviewer is told before they spend any longer writing feedback that will
    land on top of a decision already made.
    """
    if queue_key not in QUEUES:
        return JsonResponse({"ok": False, "error": "unknown_queue"}, status=404)
    if not any(request.user.has_perm(perm) for perm in QUEUE_PERMS[queue_key]):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    if claim_review(queue_key, item_id, request.user):
        return JsonResponse({"ok": True, "seconds": CLAIM_TTL})

    holder = claim_holder(queue_key, item_id) or {}
    return JsonResponse(
        {"ok": False, "error": "claim_lost", "holder": holder.get("name", "")},
        status=409,
    )


def ship_snapshot(ship):
    """The shape of the work behind a ship, counted once for the header strip.

    Everything a reviewer would otherwise scroll the journal list to count:
    how many entries there are, how many belong to *this* ship rather than an
    earlier one, how much Lookout footage backs them, and how much of the
    tracked time survived timelapse review.
    """
    journals = list(ship.project.journals.all())
    ship_journals = [journal for journal in journals if journal.ship_id == ship.id]
    ship_ids = list(ship.project.ships.order_by("created_at", "id").values_list("id", flat=True))
    tracked = tracked_minutes_for_journals(journals)
    approved = approved_minutes_for_journals(journals)
    return {
        "journals": len(journals),
        "ship_journals": len(ship_journals),
        "lookouts": LookoutSession.objects.filter(journal__in=journals).count(),
        "tracked": tracked,
        "tracked_display": format_minutes(tracked),
        "approved": approved,
        "approved_display": format_minutes(approved),
        "removed_display": format_minutes(max(tracked - approved, 0)),
        "removed": max(tracked - approved, 0),
        "attempt": ship_ids.index(ship.id) + 1 if ship.id in ship_ids else None,
        "attempts": len(ship_ids),
        "unreviewed_lapses": sum(
            1 for journal in ship_journals if not journal.timelapse_reviewed
        ),
    }


def sibling_reviews(ship):
    """Where this ship stands at each tier, as one row of badges.

    The question a reviewer asks first and used to answer by scrolling the
    history: has anyone else looked at this, and what did they say. Only the
    latest pass per tier — a ship that went round the loop has older ones, and
    those are the history's business, not this row's.
    """
    tiers = []
    latest_t1 = ship.t1_reviews.order_by("-reviewed_at", "-id").first()
    tiers.append({
        "label": "T1",
        "state": "" if latest_t1 is None else ("approved" if latest_t1.approved else "rejected"),
        "reviewer": display_name(latest_t1.reviewer) if latest_t1 else "",
    })

    latest_t2 = ship.t2_reviews.order_by("-reviewed_at", "-id").first()
    tiers.append({
        "label": "T2",
        "state": "" if latest_t2 is None else (
            "approved" if latest_t2.decision == T2.Decision.APPROVE else "returned"
        ),
        "reviewer": display_name(latest_t2.reviewer) if latest_t2 else "",
    })

    latest_t3 = ship.t3_reviews.order_by("-reviewed_at", "-id").first()
    tiers.append({
        "label": "T3",
        "state": "" if latest_t3 is None else (
            "approved" if latest_t3.decision == T3.Decision.APPROVE else "returned"
        ),
        "reviewer": display_name(latest_t3.reviewer) if latest_t3 else "",
    })
    return tiers


def journal_stats(journals):
    """The shape of the log, for the journal card's collapsed summary.

    How many entries, how long in total, and how evenly spread — a project
    logged in five even sittings and one logged in one nineteen-hour entry
    are different-looking claims, and this is where the difference shows
    without opening the card.

    Approved minutes rather than tracked, everywhere: tracked is what the
    shipper's clock said, approved is what the timelapse reviewer let stand,
    and it is the second one every tier below them is deciding about. Expects
    journals from annotate_recordings(), whose prefetch it reads.
    """
    journals = list(journals)
    each = sorted(journal.review_approved_minutes for journal in journals)
    total = sum(each)
    return {
        "count": len(journals),
        "total": total,
        "total_display": format_minutes(total),
        "average_display": format_minutes(total // len(each)) if each else format_minutes(0),
        "low_display": format_minutes(each[0] if each else 0),
        "high_display": format_minutes(each[-1] if each else 0),
    }


def annotate_recordings(journals):
    """Hang the timelapse reviewer's own words on each piece of footage.

    A T1/T2/T3 reviewer reading a journal wants to know what somebody who
    actually watched the Lookout thought of it. Without this they would have to
    open the internal review to find out, which is a page most of them can't
    reach — so the description travels with the recording instead.

    The prefetch is load-bearing, not an optimisation: the description is hung
    on the session *objects*, and without a populated cache every later
    `journal.timelapses.all()` — the template's included — is a fresh query
    returning fresh objects that never saw it.

    The rest of the annotation is the same idea applied to time. Journal and
    LookoutSession both expose tracked/removed/approved as properties that
    aggregate on read, which is one or two queries every time a template
    touches one; over a page that lists every entry and every recording under
    it that is hundreds. So the same numbers are computed once here, off the
    prefetched rows, under `review_` names the templates use instead.
    """
    if hasattr(journals, "prefetch_related"):
        journals = (
            journals
            .select_related("timelapse_review")
            .prefetch_related("timelapses", "timelapses__removals")
        )
    journals = list(journals)

    sessions = {}
    for journal in journals:
        for session in journal.timelapses.all():
            sessions[session.id] = session

    descriptions = dict(
        TimelapseAnnotation.objects
        .filter(session_id__in=sessions)
        .values_list("session_id", "description")
    ) if sessions else {}

    for journal in journals:
        tracked = removed = 0
        for session in journal.timelapses.all():
            session.review_description = descriptions.get(session.id, "")
            session.review_removed_seconds = sum(
                removal.duration_seconds for removal in session.removals.all()
            )
            session.review_approved_display = format_minutes(
                max((session.tracked_seconds or 0) - session.review_removed_seconds, 0) // 60
            )
            tracked += session.tracked_seconds or 0
            removed += session.review_removed_seconds

        journal.review_tracked_display = format_minutes(tracked // 60)
        journal.review_removed_seconds = removed
        journal.review_approved_minutes = max(tracked - removed, 0) // 60
        journal.review_approved_display = format_minutes(journal.review_approved_minutes)
    return journals


def preflight_checks(ship, subject, owner, has_make=None):
    """The handful of yes/no facts worth reading before a decision.

    Nothing here decides anything or blocks anything — it is the set of
    questions a careful reviewer asks every single time, answered once so they
    don't have to be asked by hand. `fail` means something is missing that a
    shippable project should have; `warn` means look closer, not stop.
    """
    checks = []

    def add(label, state, note=""):
        checks.append({"label": label, "state": state, "note": note})

    add(
        "Printables listing",
        "pass" if ship.project.printablesUrl else "fail",
        "" if ship.project.printablesUrl else "nothing published",
    )
    if has_make is not None:
        add(
            "Printables makes",
            "warn" if has_make else "pass",
            "someone has already made this model" if has_make else "no makes on the listing",
        )
    add(
        "Editor model",
        "pass" if ship.project.editor_model_url else "warn",
        "" if ship.project.editor_model_url else "no source file uploaded",
    )
    add(
        "Lookout footage",
        "pass" if subject["lookouts"] else "fail",
        f"{subject['lookouts']} session{'' if subject['lookouts'] == 1 else 's'}"
        if subject["lookouts"] else "no verifiable time at all",
    )
    if subject["removed"]:
        add("Time cut in lapse review", "warn", f"{format_minutes(subject['removed'])} removed")
    if subject["unreviewed_lapses"]:
        add(
            "Lapses awaiting review", "fail",
            f"{subject['unreviewed_lapses']} still unsigned",
        )
    if subject["attempts"] and subject["attempts"] > 1:
        add("Re-ship", "warn", f"attempt {subject['attempt']} of {subject['attempts']}")
    add(
        "Owner verified",
        "pass" if owner["verified"] else "fail",
        "" if owner["verified"] else "not YSWS-eligible with Hack Club",
    )
    if owner["rejected"]:
        add("Owner history", "warn", f"{owner['rejected']} ship{'' if owner['rejected'] == 1 else 's'} rejected before")
    if ship.project.locked:
        add("Project locked", "fail", "no decision can be recorded while locked")

    return {
        "checks": checks,
        "failed": sum(1 for check in checks if check["state"] == "fail"),
        "warned": sum(1 for check in checks if check["state"] == "warn"),
        "passed": sum(1 for check in checks if check["state"] == "pass"),
    }
