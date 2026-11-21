"""A reviewer's daily streak: how many days running they've done DAILY_GOAL
project reviews.

Not the streak in views/admin/streak.py, which is decisions in a row at one
sitting and lives in the session. This one is a habit, measured in days, and
it is meant to survive closing the laptop.

Any T1, T2 or T3 decision counts toward a day's DAILY_GOAL, whatever it
decided: a review done is a review done. Timelapse review doesn't, being a
different job with its own desk.

A reviewer's day is their own. The challenge weeks (weeks.py) run on Eastern
time for everyone, but a streak that rolled over at 9pm for somebody in
California, or at 5am for somebody in London, would ask them to review across
two of their evenings to keep one day. So a day here runs midnight to midnight
in the zone Slack has for the reviewer (Profile.timezone), and the reminder
goes out at 11pm by the same clock. Somebody whose zone we don't know yet is
read on the program's clock (settings.CHALLENGE_TIMEZONE) until we do.

Nothing is stored. The T1/T2/T3 rows are already the record of who reviewed
what and when, so the streak is worked out from them every time it is asked
for. That way an undone review (which deletes its row) stops counting by
itself, and there is no counter to drift out of step with the tables.
History is read in the reviewer's *current* zone, so someone whose Slack
zone changes sees their past days re-cut in the new one. That's rare enough
not to be worth storing a zone per review for.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import T1, T2, T3, Profile

# Reviews a day takes to count toward the streak.
DAILY_GOAL = 7
# The local hour the "your streak ends at midnight" DM goes out in.
REMIND_HOUR = 23

# Whoever may decide at T1, T2 or T3. Every T2 and T3 reviewer can also work
# T1 (queue.QUEUE_PERMS), so this is the T1 desk's list. Copied rather than
# imported: views.admin.queue pulls in most of the review views, and this
# module is imported by a template tag that renders on every /root page.
REVIEW_PERMS = (
    "atlantis_site.t1_review", "atlantis_site.t2_review", "atlantis_site.baller",
    "atlantis_site.t3_review", "atlantis_site.organizer",
)


def can_keep_streak(user):
    """Can this user make the reviews a streak is counted in?"""
    return any(user.has_perm(perm) for perm in REVIEW_PERMS)


def _zone_named(name):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def default_zone():
    return ZoneInfo(settings.CHALLENGE_TIMEZONE)


def zone_for(user):
    """The zone this reviewer's days are cut in."""
    profile = getattr(user, "hackclub_profile", None)
    return _zone_named(profile.timezone if profile else "") or default_zone()


def fetch_slack_timezone(slack_id):
    """The IANA zone Slack has for this user, or "" if it can't say."""
    if not slack_id:
        return ""
    # Imported here: views.helpers builds the Slack client at import time and
    # pulls in half the site, which a template tag has no business loading.
    from .views.helpers import slack_client
    try:
        tz = slack_client.users_info(user=slack_id)["user"].get("tz") or ""
    except Exception:
        return ""
    if not _zone_named(tz):
        return ""
    return tz[: Profile._meta.get_field("timezone").max_length]


def ensure_zone(profile, save=True):
    """The profile's zone, asking Slack for it first if we don't have one.

    For reviewers who last logged in before zones were recorded. Saved to the
    one column, like the other Profile writers, so it can't clobber anything
    else on the row.
    """
    zone = _zone_named(profile.timezone)
    if zone:
        return zone
    tz = fetch_slack_timezone(profile.slack_id)
    if tz:
        profile.timezone = tz
        if save and profile.pk:
            Profile.objects.filter(pk=profile.pk).update(timezone=tz)
        return ZoneInfo(tz)
    return default_zone()


def reviews_by_day(user, zone):
    """How many T1, T2 and T3 decisions `user` made on each local date.

    One grouped query per table, so this is a row per day reviewed rather
    than per review.
    """
    counts = Counter()
    for model in (T1, T2, T3):
        rows = (
            model.objects.filter(reviewer=user)
            .annotate(day=TruncDate("reviewed_at", tzinfo=zone))
            .values("day")
            .annotate(n=Count("id"))
            .order_by()
        )
        for row in rows:
            counts[row["day"]] += row["n"]
    return counts


@dataclass(frozen=True)
class DailyStreak:
    # Days in a row the goal was met, counting today once it has been.
    days: int
    # Reviews done so far today.
    today: int
    # The reviewer's local date, and the zone it was read in.
    date: date
    zone_name: str
    goal: int = DAILY_GOAL

    @property
    def done_today(self):
        return self.today >= self.goal

    @property
    def left(self):
        return max(self.goal - self.today, 0)

    @property
    def at_risk(self):
        """A run is going, and today hasn't been added to it yet."""
        return self.days > 0 and not self.done_today

    @property
    def pips(self):
        """One per review the goal asks for: True for each one done."""
        return [i < self.today for i in range(self.goal)]


def streak_from_counts(counts, today):
    """Days in a row up to `today` with DAILY_GOAL reviews or more.

    Today not having reached the goal yet doesn't break anything: the day
    isn't over. The run is then counted back from yesterday instead, and only
    a whole day short of the goal ends it.
    """
    day = today if counts.get(today, 0) >= DAILY_GOAL else today - timedelta(days=1)
    days = 0
    while counts.get(day, 0) >= DAILY_GOAL:
        days += 1
        day -= timedelta(days=1)
    return days


def daily_streak(user, now=None, zone=None):
    """Where `user`'s daily streak stands right now."""
    zone = zone or zone_for(user)
    today = (now or timezone.now()).astimezone(zone).date()
    counts = reviews_by_day(user, zone)
    return DailyStreak(
        days=streak_from_counts(counts, today),
        today=counts.get(today, 0),
        date=today,
        zone_name=str(zone),
    )
