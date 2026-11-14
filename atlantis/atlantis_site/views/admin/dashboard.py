from django.shortcuts import render
from django.urls import reverse

from ..helpers import check_perms, staff_member_required
from .queue import QUEUE_PERMS, QUEUES, can_open_queue

# The desks in the order a ship passes through them, which is also the order
# the public /queue/ page lists them in.
PIPELINE = ("lookout", "t1", "t2", "t3")

FULFILLMENT_PERMS = ["atlantis_site.organizer", "atlantis_site.fulfillment"]


def _has_any(user, perms):
    return any(user.has_perm(perm) for perm in perms)


def desks_for(user):
    """What is waiting on each desk `user` can open, for the admin home.

    The counts are the rail's own cached ones, so the home page and the badges
    beside it can't disagree and a warm page load counts nothing.
    """
    # Imported here: nav_badges imports this package (for QUEUES), so a
    # module-level import is circular whenever nav_badges loads first.
    from ...templatetags.nav_badges import cached_pending_count

    desks = []
    for key in PIPELINE:
        if not can_open_queue(user, key):
            continue
        queue = QUEUES[key]
        count = cached_pending_count(key)
        desks.append({
            "short": queue.short,
            "label": queue.label,
            "accent": queue.accent,
            "count": count,
            "noun": queue.noun,
            "url": queue.dash_url(),
            # Starting takes a claim, which is narrower than looking: a
            # reviewer lead can read the T1 desk but not work it.
            "start_url": (
                reverse(queue.next_url)
                if count and _has_any(user, QUEUE_PERMS[key]) else None
            ),
        })
    if _has_any(user, FULFILLMENT_PERMS):
        desks.append({
            "short": "Shop",
            "label": "Fulfillment",
            "accent": "good",
            "count": cached_pending_count("fulfillment"),
            "noun": "order",
            "url": reverse("fulfillment_dash"),
            "start_url": None,
        })
    return desks


@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment", "atlantis_site.t1_review", "atlantis_site.t2_review", "atlantis_site.baller", "atlantis_site.t3_review", "atlantis_site.timelapse_review", "atlantis_site.reviewer_lead", "atlantis_site.metrics"])
def admin_dash(request):
    return render(request, "root/home.html", {"desks": desks_for(request.user)})
