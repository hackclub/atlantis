"""Who is on the site, and when they were last here.

Nothing else in here records that a user simply *showed up*. Every other
timestamp we keep is attached to something they produced — a project, a lapse,
an order — so a builder who spent an afternoon reading the guides and watching
the queue left no trace at all, and the metrics page could not tell a quiet day
from an empty one.

So this middleware writes two things, and only for signed-in users:
Profile.last_seen, which answers "who is here now", and one ActiveDay row per
user per day, which is the history "how many people were here last Tuesday" is
counted out of. Both are written on the way out of a request, and neither is
read by anything a user can load.

The cost is kept off the request path by a cache key per user: the first
request in a WRITE_EVERY window does the writes, and every request behind it
inside that window does one cache read and nothing else. That makes last_seen
accurate to within a minute, which is all the "active now" tile needs and far
cheaper than a row update per page view. Presence is bookkeeping, so a database
that refuses these writes is logged and swallowed — it must never be the reason
a page 500s.
"""

import logging

from django.core.cache import cache
from django.db import DatabaseError
from django.utils import timezone

from . import weeks

logger = logging.getLogger(__name__)

# How long a user's presence write is suppressed for after one goes through.
# The floor on how stale last_seen can be, and so well under the five minutes
# the "active now" tile counts in.
WRITE_EVERY = 60


def _cache_key(user_id):
    return f"presence:{user_id}"


def record_seen(user):
    """Note that this user was here. Cheap to call on every request.

    Returns True when the writes actually ran, which is at most once per
    WRITE_EVERY seconds per user — the caller has no use for that, but the
    tests do.
    """
    from .models import ActiveDay, Profile

    key = _cache_key(user.pk)
    if cache.get(key):
        return False
    # Set before writing, not after: a burst of concurrent requests from one
    # user should produce one write, and the window is worth losing if the
    # write below fails — the next one is a minute away.
    cache.set(key, True, WRITE_EVERY)

    now = timezone.now()
    try:
        # .update() rather than save(): this runs alongside whatever the
        # request itself did to the profile, and touching one column can't
        # clobber it.
        Profile.objects.filter(user=user).update(last_seen=now)
        # ON CONFLICT DO NOTHING, so the day's row is written without reading
        # for it first and two parallel requests can't collide over it. The
        # day is the program's, not UTC's: the metrics page counts these by
        # Eastern date, and a UTC date would file every evening under tomorrow.
        ActiveDay.objects.bulk_create(
            [ActiveDay(user=user, day=timezone.localdate(now, weeks.zone()))],
            ignore_conflicts=True,
        )
    except DatabaseError:
        logger.warning("Could not record presence for user %s", user.pk, exc_info=True)
        return False
    return True


class PresenceMiddleware:
    """Records the signed-in user's presence once the response is built.

    After the view, so a request that never made it to one — a redirect out of
    an auth check, a 404 — costs nothing, and so presence is never written for
    a request that ended up rejected.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            record_seen(user)
        return response
