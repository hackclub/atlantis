"""When the challenge weeks run, and which one a moment falls in.

Pure date arithmetic: nothing here touches the database, so it can be imported
from models.py, from views, and from the management command without a cycle.
The ledger built on top of it lives in challenge.py.

Two clocks are in play and conflating them is the bug this module exists to
prevent. Every timestamp in the database is UTC (settings.TIME_ZONE), but a
week is a *local* Monday-to-Sunday in settings.CHALLENGE_TIMEZONE, and the two
are neither the same instant nor a fixed distance apart: US Eastern shifts by
an hour on the first Sunday in November, which on the default schedule falls
inside week 6. So boundaries are built by combining a local date with local
midnight and attaching the zone, never by adding a timedelta to an aware
datetime — the latter would put weeks 7 and 8 an hour out.

The program is `CHALLENGE_WEEKS` weeks long. Anything before week 1 opens is
"prep", which pays the old flat rate; anything after the last week closes is
past the end of the program, when shipping is off.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


# What a week asks for, and what surviving one is worth against the printer.
# The 40 hours a printer costs is exactly WEEKLY_HOURS * CHALLENGE_WEEKS: a
# week credits five hours towards it and no more, however many were really
# logged, so the bar fills if and only if every week is survived.
WEEKLY_HOURS = 5
WEEKLY_MINUTES = WEEKLY_HOURS * 60

# A one-off for the start of the program: weeks 1 and 2 are judged together,
# so ten hours across the pair keeps you in however it's split, and week 1
# can't be missed until week 2 has closed too. Every later week stands on its
# own five. See challenge.Week.met.
GRACE_WEEKS = (1, 2)


def zone():
    """The program's timezone — the one weeks are cut in."""
    return ZoneInfo(settings.CHALLENGE_TIMEZONE)


def week_count():
    return settings.CHALLENGE_WEEKS


def printer_hours():
    """Hours the printer bar is out of: five for every week of the program."""
    return WEEKLY_HOURS * week_count()


def grace_weeks():
    """The pooled weeks that actually exist in a program this long."""
    return tuple(index for index in GRACE_WEEKS if index <= week_count())


def grace_minutes():
    """What the pooled weeks ask for between them."""
    return WEEKLY_MINUTES * len(grace_weeks())


def start_date():
    """Local date week 1 opens on."""
    return date.fromisoformat(settings.CHALLENGE_START_DATE)


def _local_midnight(on):
    """Midnight local to the program on the given local date.

    fold=0 is the default and is spelled out because this is exactly the call
    a DST transition would otherwise make ambiguous. Neither US transition
    happens at midnight, so no date this is handed has two midnights; saying so
    keeps that a stated assumption rather than a lucky one.
    """
    return datetime.combine(on, time.min, tzinfo=zone()).replace(fold=0)


def starts_at():
    """The instant week 1 opens."""
    return _local_midnight(start_date())


def ends_at():
    """The instant the program closes — midnight after the last week's Sunday.

    Exclusive, like every other end in here: the last week runs up to but not
    including this, so a moment equal to it is already past the program.
    """
    return _local_midnight(start_date() + timedelta(weeks=week_count()))


def week_bounds(index):
    """(start, end) for week `index`, 1-based. End is exclusive.

    The end is the *next* week's local midnight rather than 23:59:59, so no
    instant falls between two weeks. What a page shows as "Sunday 11:59pm" is
    this minus a second; what the arithmetic uses is this.
    """
    if not 1 <= index <= week_count():
        raise ValueError(f"week {index} is outside the {week_count()}-week program")
    opens = start_date() + timedelta(weeks=index - 1)
    return _local_midnight(opens), _local_midnight(opens + timedelta(days=7))


def week_for(moment):
    """Which week `moment` falls in, or None for prep and for after the end.

    Compares local *dates*, since weeks are whole local days: that settles the
    Sunday-11:59:59pm edge exactly, with no reliance on how the boundary
    instant rounds.
    """
    if moment is None:
        return None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    days = (moment.astimezone(zone()).date() - start_date()).days
    if days < 0:
        return None
    index = days // 7 + 1
    return index if index <= week_count() else None


def is_prep(moment):
    """True for work logged before week 1 opened, which pays the flat rate."""
    if moment is None:
        return False
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    return moment < starts_at()


def current_week(now=None):
    """The week in progress, or None during prep and after the program ends."""
    return week_for(now or timezone.now())


def current_week_bounds(now=None):
    """(start, end) of the week `now` is in. End is exclusive.

    The challenge week while the program runs; outside it, the local Monday to
    Sunday around `now`, so a "this week" view still means something in prep
    and after the end.
    """
    now = now or timezone.now()
    index = week_for(now)
    if index is not None:
        return week_bounds(index)
    today = now.astimezone(zone()).date()
    monday = today - timedelta(days=today.weekday())
    return _local_midnight(monday), _local_midnight(monday + timedelta(days=7))


def has_started(now=None):
    return (now or timezone.now()) >= starts_at()


def has_ended(now=None):
    return (now or timezone.now()) >= ends_at()


def closed_weeks(now=None):
    """Every week index that has finished, oldest first.

    A week is closed once its end has passed; the week in progress is never in
    here, which is what stops a live week being judged before it is over.
    """
    now = now or timezone.now()
    return [i for i in range(1, week_count() + 1) if week_bounds(i)[1] <= now]


def deadline(index):
    """The last instant of week `index`, for display — its Sunday 11:59:59pm."""
    return week_bounds(index)[1] - timedelta(seconds=1)


# The two hours after a week closes, up to 2am local on the Monday. Work
# recorded in them goes to the week that just closed if it closed short, and to
# the new one if it didn't — challenge.py decides which, since it needs the
# hours. The deadline everyone is shown is still Sunday midnight: this is slack
# for a session that ran over, not a later deadline.
BUFFER_ENDS = time(2, 0)


def buffer_bounds(index):
    """(start, end) of the buffer after week `index`. End is exclusive.

    Built from the local date like every other boundary here, not by adding
    two hours to midnight; neither DST change lands in a Monday's first hours,
    but that keeps it a non-question.
    """
    start, end = week_bounds(index)
    monday = end.astimezone(zone()).date()
    return end, datetime.combine(monday, BUFFER_ENDS, tzinfo=zone()).replace(fold=0)


def buffer_week(moment):
    """The week whose buffer `moment` falls in, or None.

    Includes the buffer after the last week, when week_for already says None.
    """
    if moment is None:
        return None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    local = moment.astimezone(zone())
    days = (local.date() - start_date()).days
    if days <= 0 or days % 7 or local.time() >= BUFFER_ENDS:
        return None
    index = days // 7
    return index if index <= week_count() else None


# When the "hours left" DM goes out: Saturday evening, local, a day and a bit
# before the week closes.
REMINDER_DAY = 5  # Monday is 0
REMINDER_TIME = time(18, 0)


def reminder_at(index):
    """The instant week `index`'s reminder DM is due — its Saturday 6pm."""
    opens = start_date() + timedelta(weeks=index - 1)
    on = opens + timedelta(days=REMINDER_DAY)
    return datetime.combine(on, REMINDER_TIME, tzinfo=zone()).replace(fold=0)


def week_label(index):
    """"Week 3 · Oct 5–11", the way a heading names it."""
    opens, closes = week_bounds(index)
    closes = (closes - timedelta(days=1)).astimezone(zone())
    opens = opens.astimezone(zone())
    if opens.month == closes.month:
        span = f"{opens:%b} {opens.day}–{closes.day}"
    else:
        span = f"{opens:%b} {opens.day}–{closes:%b} {closes.day}"
    return f"Week {index} · {span}"
