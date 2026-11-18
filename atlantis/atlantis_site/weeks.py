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
"prep", which pays the old flat rate. After the last week closes come
POST_PREP_WEEKS more prep weeks, paid at that same flat rate; once they end,
shipping is off.
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

# Pairs of weeks judged together: ten hours across a pair keeps you in however
# it's split, and the first week of a pair can't be missed until the second has
# closed too. Weeks 1 and 2 ease people into the program; weeks 7 and 8 were
# pooled the same way later on. Every other week stands on its own five. See
# challenge.Week.met.
GRACE_PAIRS = ((1, 2), (7, 8))

# Days a week's close was pushed back past its Sunday, by week index. Week 8
# was extended to Tuesday 11:59pm. Only the program's last week can be
# extended — any other would run into the week after it — so an entry for an
# earlier week is ignored.
EXTENSION_DAYS = {8: 2}

# On top of that, an organizer can give one person more days on the last week
# (models.FinalWeekExtension). Everything below that can depend on it takes
# `extra`, those days, and it only ever moves the last week's end: for any
# other week, or extra=0, the answer is everyone's.

# Prep weeks after the program: once the last week closes, shipping stays open
# at the flat prep rate until the end of this many more Sundays. They're counted
# from the last week's own Sunday, so an extension eats into them rather than
# pushing them back — week 8 ending on a Tuesday still leaves them ending on
# the Sunday two weeks after its original one.
POST_PREP_WEEKS = 2


def zone():
    """The program's timezone — the one weeks are cut in."""
    return ZoneInfo(settings.CHALLENGE_TIMEZONE)


def week_count():
    return settings.CHALLENGE_WEEKS


def printer_hours():
    """Hours the printer bar is out of: five for every week of the program."""
    return WEEKLY_HOURS * week_count()


def grace_pairs():
    """The pooled pairs, each cut to the weeks that exist in a program this long."""
    pairs = (tuple(index for index in pair if index <= week_count()) for pair in GRACE_PAIRS)
    return tuple(pair for pair in pairs if pair)


def grace_pair(index):
    """The pooled weeks week `index` is judged with, itself included — or ()
    for a week that stands on its own five."""
    return next((pair for pair in grace_pairs() if index in pair), ())


def grace_minutes(index):
    """What the pair week `index` is in asks for between them."""
    return WEEKLY_MINUTES * len(grace_pair(index))


def start_date():
    """Local date week 1 opens on."""
    return date.fromisoformat(settings.CHALLENGE_START_DATE)


def extension_days(index):
    """Days week `index` runs past its Sunday — 0 unless it was extended."""
    return EXTENSION_DAYS.get(index, 0) if index == week_count() else 0


def _close_date(index, extra=0):
    """Local date of the midnight week `index` closes at (exclusive)."""
    if index == week_count():
        extra += extension_days(index)
    else:
        extra = 0
    return start_date() + timedelta(weeks=index, days=extra)


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


def ends_at(extra=0):
    """The instant the program closes — midnight after the last week's last day.

    Exclusive, like every other end in here: the last week runs up to but not
    including this, so a moment equal to it is already past the program.
    """
    return _local_midnight(_close_date(week_count(), extra))


def shipping_closes_at():
    """The instant shipping closes — midnight after the last post-program prep
    week's Sunday. Exclusive, and never before the program itself ends."""
    closes = _local_midnight(start_date() + timedelta(weeks=week_count() + POST_PREP_WEEKS))
    return max(closes, ends_at())


def is_post_prep(moment):
    """True between the program ending and shipping closing: the prep weeks
    after the last week, which pay the flat rate like the ones before week 1.

    Kept apart from is_prep, which means *before* week 1 — a deflation made
    then is owed in week 1, and one made now is owed nowhere.
    """
    if moment is None:
        return False
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    return ends_at() <= moment < shipping_closes_at()


def shipping_has_closed(now=None):
    return (now or timezone.now()) >= shipping_closes_at()


def max_extension_days():
    """The most days an organizer can add to one person's last week.

    Enough that it still ends a whole day before shipping closes: time
    recorded in the extension only counts towards a printer once it's
    shipped, so an extension running up to the close would leave nothing
    to ship it in.
    """
    last_day = shipping_closes_at().astimezone(zone()).date() - timedelta(days=1)
    return max((last_day - _close_date(week_count())).days, 0)


def week_bounds(index, extra=0):
    """(start, end) for week `index`, 1-based. End is exclusive.

    The end is the *next* week's local midnight rather than 23:59:59, so no
    instant falls between two weeks. What a page shows as "Sunday 11:59pm" is
    this minus a second; what the arithmetic uses is this. An extended week
    (EXTENSION_DAYS, plus `extra` for one person) ends that many days later.
    """
    if not 1 <= index <= week_count():
        raise ValueError(f"week {index} is outside the {week_count()}-week program")
    opens = start_date() + timedelta(weeks=index - 1)
    return _local_midnight(opens), _local_midnight(_close_date(index, extra))


def week_for(moment, extra=0):
    """Which week `moment` falls in, or None for prep and for after the end.

    Compares local *dates*, since weeks are whole local days: that settles the
    Sunday-11:59:59pm edge exactly, with no reliance on how the boundary
    instant rounds.
    """
    if moment is None:
        return None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    on = moment.astimezone(zone()).date()
    days = (on - start_date()).days
    if days < 0:
        return None
    index = days // 7 + 1
    if index <= week_count():
        return index
    # The days an extended last week runs past its Sunday.
    return week_count() if on < _close_date(week_count(), extra) else None


def is_prep(moment):
    """True for work logged before week 1 opened, which pays the flat rate."""
    if moment is None:
        return False
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    return moment < starts_at()


def current_week(now=None, extra=0):
    """The week in progress, or None during prep and after the program ends."""
    return week_for(now or timezone.now(), extra)


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


def has_ended(now=None, extra=0):
    return (now or timezone.now()) >= ends_at(extra)


def closed_weeks(now=None, extra=0):
    """Every week index that has finished, oldest first.

    A week is closed once its end has passed; the week in progress is never in
    here, which is what stops a live week being judged before it is over.
    """
    now = now or timezone.now()
    return [i for i in range(1, week_count() + 1) if week_bounds(i, extra)[1] <= now]


def deadline(index, extra=0):
    """The last instant of week `index`, for display — its Sunday 11:59:59pm."""
    return week_bounds(index, extra)[1] - timedelta(seconds=1)


# The two hours after a week closes, up to 2am local on the Monday. Work
# recorded in them goes to the week that just closed if it closed short, and to
# the new one if it didn't — challenge.py decides which, since it needs the
# hours. The deadline everyone is shown is still Sunday midnight: this is slack
# for a session that ran over, not a later deadline.
BUFFER_ENDS = time(2, 0)


def buffer_bounds(index, extra=0):
    """(start, end) of the buffer after week `index`. End is exclusive.

    Built from the local date like every other boundary here, not by adding
    two hours to midnight; neither DST change lands in a Monday's first hours,
    but that keeps it a non-question.
    """
    start, end = week_bounds(index, extra)
    monday = end.astimezone(zone()).date()
    return end, datetime.combine(monday, BUFFER_ENDS, tzinfo=zone()).replace(fold=0)


def buffer_week(moment, extra=0):
    """The week whose buffer `moment` falls in, or None.

    Includes the buffer after the last week, when week_for already says None.
    """
    if moment is None:
        return None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    local = moment.astimezone(zone())
    if local.time() >= BUFFER_ENDS:
        return None
    on = local.date()
    return next((i for i in range(1, week_count() + 1) if _close_date(i, extra) == on), None)


def settled_at(extra=0):
    """The instant every week is settled: the end of the buffer after the last
    one, 2am local the day after it closes.

    The program has *ended* two hours before this, but until the last buffer
    closes the last week (and its grace pair) can't be missed yet, so who is
    still in isn't final. Anything decided on who passed waits for this.
    """
    return buffer_bounds(week_count(), extra)[1]


def has_settled(now=None, extra=0):
    return (now or timezone.now()) >= settled_at(extra)


# When the "hours left" DM goes out: the evening before the week's last day,
# local, a day and a bit before it closes — Saturday, for a week that isn't
# extended.
REMINDER_TIME = time(18, 0)


def reminder_at(index):
    """The instant week `index`'s reminder DM is due — 6pm the day before its
    last day."""
    on = _close_date(index) - timedelta(days=2)
    return datetime.combine(on, REMINDER_TIME, tzinfo=zone()).replace(fold=0)


def week_label(index, extra=0):
    """"Week 3 · Oct 5–11", the way a heading names it."""
    opens, closes = week_bounds(index, extra)
    closes = (closes - timedelta(days=1)).astimezone(zone())
    opens = opens.astimezone(zone())
    if opens.month == closes.month:
        span = f"{opens:%b} {opens.day}–{closes.day}"
    else:
        span = f"{opens:%b} {opens.day}–{closes:%b} {closes.day}"
    return f"Week {index} · {span}"
