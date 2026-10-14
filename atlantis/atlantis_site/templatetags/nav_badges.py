"""The pending counts the admin rail wears next to each desk.

A reviewer arriving at /root asks one question first — which queue needs me? —
and answering it used to mean opening all five pages. The badges put the answer
in the sidebar, so the rail is the triage.

Counted per link rather than all at once on purpose: the navbar only renders
the desks a reviewer has permission for, so a T1-only reviewer never pays for
the T3 count.

The rail is on every admin page, so the counts are cached for half a minute
(PENDING_COUNT_TTL). A count doesn't depend on who is asking — permissions only
decide which badges are drawn — so there is one key per desk, shared by every
reviewer. A decision clears them on its way to the next item (go_to_next), so
the reviewer who made it never sees their own work still counted; somebody
else's decision can take up to the TTL to show.
"""

from django import template
from django.core.cache import cache
from django.utils.html import format_html

from ..models import Order
from ..views.admin.queue import PENDING_COUNT_TTL, QUEUES, pending_count_cache_key

register = template.Library()

# Past this the exact number stops being information a reviewer acts on, and a
# four-digit badge would push the rail's links around.
BADGE_MAX = 99


def pending_count(key):
    """How many things are waiting on one desk.

    The review desks answer with their own queue — the same set `next` walks —
    so a badge can never disagree with the page it links to.
    """
    if key == "fulfillment":
        return Order.objects.filter(status=Order.OrderStatus.PENDING).count()
    return QUEUES[key].pending().count()


def cached_pending_count(key):
    """pending_count, as of at most PENDING_COUNT_TTL seconds ago."""
    cache_key = pending_count_cache_key(key)
    count = cache.get(cache_key)
    if count is None:
        count = pending_count(key)
        cache.set(cache_key, count, PENDING_COUNT_TTL)
    return count


@register.simple_tag
def nav_badge(key):
    """The rail's badge for one desk — nothing at all when it's empty.

    An empty queue is the state that needs no attention, and a rail of zeroes
    is five things to read past on the way to the one that isn't.
    """
    count = cached_pending_count(key)
    if not count:
        return ""
    return format_html(
        '<span class="nav-badge" title="{} pending">{}</span>',
        count,
        count if count <= BADGE_MAX else f"{BADGE_MAX}+",
    )
