import json
from datetime import datetime, time, timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Sum, Avg
from django.db.models.functions import TruncDate, TruncWeek
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_date

from ...models import (
    ActiveDay,
    AuditLog,
    Profile,
    Project,
    Ship,
    Journal,
    T1,
    T2,
    T3,
    Item,
    MetricsSnapshot,
    Order,
    SaverCredit,
    Timelapse,
    PAYOUT_MULTIPLIER_DEFAULT,
    detect_editor,
)
from ... import challenge, weeks
from ..helpers import (
    add_bars,
    approved_minutes_for_journals,
    check_perms,
    display_name,
    format_minutes,
    layers_for_minutes,
    reviewer_leaderboard,
    tracked_minutes_for_journals,
)

# How recently a user has to have been seen to count as here *now*. Presence is
# written at most once a minute per user (see presence.py), so this is
# comfortably wider than the staleness that introduces.
ACTIVE_NOW_WINDOW = timedelta(minutes=5)

# The window every "per day" average on this page is taken over, and the one
# the 30-day totals are cut to.
WINDOW_DAYS = 30

# The shorter window, for the averages that should react to this week rather
# than to the trailing month.
SHORT_WINDOW_DAYS = 7

# The buckets the "this week so far" chart sorts people still in into, as
# (label, upper bound in minutes, exclusive). The last is open-ended, and is
# exactly the people who are done.
STREAK_BUCKETS = [
    ("Nothing yet", 1),
    ("Under 1h", 60),
    ("1-2h", 120),
    ("2-3h", 180),
    ("3-4h", 240),
    ("4-5h", weeks.WEEKLY_MINUTES),
    ("Done (5h+)", None),
]

# How many days the daily bar charts go back. Short enough that each bar is
# still readable in a column of them.
TREND_DAYS = 14

# How many calendar weeks (Mon-Sun, this one included) the weekly builders
# chart goes back — enough to see a drop-off across the challenge.
TREND_WEEKS = 8


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else 0.0


def _hours(minutes):
    """Minutes as the hours figure the stat cards are read in."""
    return round((minutes or 0) / 60, 1)


def _avg(total, count):
    return round(total / count, 1) if count else 0.0


def _daily_rows(counts, days, today, value=lambda n: n):
    """One chart row per day in the window, oldest first, gaps filled with zero.

    `counts` maps date -> raw number. Days nobody did anything on are absent
    from any aggregate query, and a chart that simply skipped them would draw a
    quiet week as a busy one.
    """
    return add_bars([
        {"label": day.strftime("%b %-d"), "value": value(counts.get(day, 0))}
        for day in (today - timedelta(days=offset) for offset in range(days - 1, -1, -1))
    ])


def _week_elapsed(index, now):
    """How far through week `index` `now` is, 0-1."""
    start, end = weeks.week_bounds(index)
    return min(max((now - start) / (end - start), 0.0), 1.0)


def build_streak_stats(now):
    """Where the field stands in the weekly challenge.

    Counted over challenge.standings(), so a participant is anyone who has
    logged time or held a saver — not every account on the site. "Behind
    pace" is the dropping-out forecast: someone whose hours so far, carried on
    at the same rate to Sunday midnight, come up short of the five.
    """
    field = list(challenge.standings(now).values())
    still_in = [s for s in field if not s.eliminated]
    live_index = weeks.current_week(now)

    stats = {
        "started": weeks.has_started(now),
        "ended": weeks.has_ended(now),
        "participants": len(field),
        "still_in": len(still_in),
        "out": len(field) - len(still_in),
        "survival_rate": _pct(len(still_in), len(field)),
        "live_week": live_index,
        "live_label": weeks.week_label(live_index) if live_index else "",
        "weekly_hours": weeks.WEEKLY_HOURS,
        "printer_unlocked": sum(1 for s in still_in if s.printer_unlocked),
    }

    # Weeks that closed short on logged time and were met anyway: savers,
    # bought or granted, or an organizer's pass.
    stats["weeks_rescued"] = sum(
        1 for s in field for w in s.weeks
        if w.closed and w.met and w.tracked_minutes < weeks.WEEKLY_MINUTES
    )

    closed = weeks.closed_weeks(now)
    stats["survived_by_week"] = add_bars([
        {
            "label": f"Week {index}",
            "value": sum(1 for s in field if s.weeks[index - 1].met),
            "sub": f"{sum(1 for s in field if s.weeks[index - 1].missed)} missed",
        }
        for index in closed
    ])

    # Who actually logged the week's hours themselves, week by week, the live
    # one included. Logged time only: a saver keeps someone in the program,
    # but it isn't five hours of work, and an average padded with bought
    # hours would say people built more than they did. Counted over the
    # whole field, so someone who has since dropped out still counts for the
    # weeks they did complete.
    started = closed + ([live_index] if live_index else [])
    completed_rows = []
    for index in started:
        completers = [
            s.weeks[index - 1].tracked_minutes for s in field
            if s.weeks[index - 1].tracked_minutes >= weeks.WEEKLY_MINUTES
        ]
        average = sum(completers) / len(completers) if completers else 0
        completed_rows.append({
            "label": f"Week {index}",
            "value": len(completers),
            "sub": (
                f"avg {format_minutes(average)}" if completers else "nobody yet"
            ) + (" so far" if index == live_index else ""),
            "average_minutes": average,
        })
    stats["completed_by_week"] = add_bars(completed_rows)

    if live_index is None:
        return stats

    elapsed = _week_elapsed(live_index, now)
    live = [s.current for s in still_in]
    done = [w for w in live if w.met]
    short = [w for w in live if not w.met]
    # Credited so far against the share of the five the elapsed part of the
    # week asks for: level with the clock is on pace, below it is not.
    behind = [w for w in short if w.credited_minutes < elapsed * weeks.WEEKLY_MINUTES]
    shortfall = sum(w.shortfall_minutes for w in short)

    counts = [0] * len(STREAK_BUCKETS)
    for w in live:
        for i, (_label, bound) in enumerate(STREAK_BUCKETS):
            if bound is None or w.credited_minutes < bound:
                counts[i] += 1
                break

    stats.update({
        "done": len(done),
        "done_rate": _pct(len(done), len(live)),
        "short": len(short),
        "on_pace": len(short) - len(behind),
        "behind_pace": len(behind),
        "behind_rate": _pct(len(behind), len(live)),
        "nothing_yet": sum(1 for w in live if not w.credited_minutes),
        "elapsed_pct": round(elapsed * 100),
        "pace_display": format_minutes(elapsed * weeks.WEEKLY_MINUTES),
        # Formatted here rather than in the template: a snapshot stores this
        # as JSON, and a datetime would come back a string |date can't read.
        "deadline_display": weeks.deadline(live_index).astimezone(weeks.zone()).strftime("%a %b %-d, %-I:%M%p ET"),
        "shortfall_hours": _hours(shortfall),
        "avg_shortfall_display": format_minutes(shortfall / len(short) if short else 0),
        "avg_credited_display": format_minutes(
            sum(w.credited_minutes for w in live) / len(live) if live else 0
        ),
        "saver_hours_this_week": SaverCredit.objects.filter(week_index=live_index).count(),
        # The same figure the chart's live row carries, as a card.
        "completed_this_week": completed_rows[-1]["value"],
        "avg_completed_display": format_minutes(completed_rows[-1]["average_minutes"]),
        "this_week": add_bars([
            {"label": label, "value": count}
            for (label, _bound), count in zip(STREAK_BUCKETS, counts)
        ]),
    })
    return stats


@timezone.override(settings.CHALLENGE_TIMEZONE)
def build_metrics(now):
    """Every figure on the metrics page, as it reads at `now`.

    All "today"/"per day" windows here are cut in CHALLENGE_TIMEZONE (US
    Eastern), not the server's UTC — the decorator above activates it for the
    whole build, so timezone.localdate()/localtime() and the TruncDate
    groupings all follow it."""
    User = get_user_model()
    last_7 = now - timedelta(days=7)
    last_30 = now - timedelta(days=30)
    last_24h = now - timedelta(hours=24)
    today = timezone.localdate(now)
    today_start = timezone.localtime(now).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    # The calendar week so far, Monday midnight local. Built from the local
    # date rather than by stepping back from today_start, which would land an
    # hour out on the Monday after a DST change.
    week_start_day = today - timedelta(days=today.weekday())
    week_start = datetime.combine(week_start_day, time.min, tzinfo=timezone.get_current_timezone())
    window_start_day = today - timedelta(days=WINDOW_DAYS - 1)
    trend_start_day = today - timedelta(days=TREND_DAYS - 1)

    # ---- who is here -----------------------------------------------------
    # Presence only exists from the day the middleware shipped, so the DAU
    # average is divided by the days actually on record rather than by a flat
    # 30 — otherwise the first month reads as a collapse in traffic.
    first_tracked_day = ActiveDay.objects.order_by("day").values_list("day", flat=True).first()
    dau_start_day = max(window_start_day, first_tracked_day) if first_tracked_day else None
    dau_days = (today - dau_start_day).days + 1 if dau_start_day else 0

    window_days = ActiveDay.objects.filter(day__gte=window_start_day)
    active_now = Profile.objects.filter(
        last_seen__gte=now - ACTIVE_NOW_WINDOW
    ).count()
    active_today = ActiveDay.objects.filter(day=today).count()
    user_days_in_window = window_days.count()
    active_in_window = window_days.values("user").distinct().count()

    total_users = User.objects.count()
    signups_today = User.objects.filter(date_joined__gte=today_start).count()
    signups_last_7 = User.objects.filter(date_joined__gte=last_7).count()
    signups_last_30 = User.objects.filter(date_joined__gte=last_30).count()
    first_signup = User.objects.order_by("date_joined").values_list("date_joined", flat=True).first()
    signup_days = (today - timezone.localdate(first_signup)).days + 1 if first_signup else 0

    dau_counts = {
        row["day"]: row["n"]
        for row in window_days.filter(day__gte=trend_start_day)
        .values("day").annotate(n=Count("id"))
    }
    signup_counts = {
        row["day"]: row["n"]
        for row in User.objects.filter(date_joined__gte=today_start - timedelta(days=TREND_DAYS - 1))
        .annotate(day=TruncDate("date_joined"))
        .values("day").annotate(n=Count("id"))
    }

    activity_stats = {
        "active_now": active_now,
        "active_now_minutes": int(ACTIVE_NOW_WINDOW.total_seconds() // 60),
        "active_today": active_today,
        "avg_dau": _avg(user_days_in_window, dau_days),
        "dau_days": dau_days,
        "active_in_window": active_in_window,
        "window_days": WINDOW_DAYS,
        "total_users": total_users,
        "signups_today": signups_today,
        "signups_last_7": signups_last_7,
        "signups_last_30": signups_last_30,
        "avg_signups_window": _avg(signups_last_30, WINDOW_DAYS),
        "avg_signups_all_time": _avg(total_users, signup_days),
        "signup_days": signup_days,
        "daily_active": _daily_rows(dau_counts, TREND_DAYS, today),
        "daily_signups": _daily_rows(signup_counts, TREND_DAYS, today),
    }

    # ---- hours logged ----------------------------------------------------
    # "Logged" is counted against the lapse the footage was attached to, not
    # against when it was recorded: that is the moment the time entered the
    # book and the moment it started costing a reviewer something.
    journals_today = Journal.objects.filter(created_at__gte=today_start)
    journals_window = Journal.objects.filter(created_at__gte=last_30)
    journals_last_7 = Journal.objects.filter(created_at__gte=last_7)
    journals_this_week = Journal.objects.filter(created_at__gte=week_start)
    # The whole program, week 1's Monday up to (not including) the midnight
    # after the last Sunday — the span the challenge rates are paid over.
    journals_challenge = Journal.objects.filter(
        created_at__gte=weeks.starts_at(), created_at__lt=weeks.ends_at()
    )
    pending_journals = Journal.objects.filter(timelapse_review__isnull=True)
    reviewed_window = Journal.objects.filter(timelapse_review__reviewed_at__gte=last_30)

    minutes_today = tracked_minutes_for_journals(journals_today)
    minutes_window = tracked_minutes_for_journals(journals_window)
    minutes_last_7 = tracked_minutes_for_journals(journals_last_7)
    minutes_this_week = tracked_minutes_for_journals(journals_this_week)
    minutes_challenge = tracked_minutes_for_journals(journals_challenge)
    total_time_minutes = tracked_minutes_for_journals(Journal.objects.all())
    pending_minutes = tracked_minutes_for_journals(pending_journals)
    approved_minutes_window = approved_minutes_for_journals(reviewed_window)

    devlogs_today = journals_today.count()
    devlogs_window = journals_window.count()
    devlogs_last_7 = journals_last_7.count()
    pending_devlogs = pending_journals.count()
    total_journals = Journal.objects.count()
    # Builders, not users: whoever owns a project that got a lapse in the
    # window. It is the denominator the per-person average only makes sense
    # against — dividing by everyone who ever signed up would bury it.
    builders_window = journals_window.values("project__owner").distinct().count()
    builders_today = journals_today.values("project__owner").distinct().count()
    builders_last_7 = journals_last_7.values("project__owner").distinct().count()
    builders_this_week = journals_this_week.values("project__owner").distinct().count()

    # Builders per calendar week, the same count as builders_this_week for
    # each of the weeks before it. Grouped on the Monday each lapse's week
    # opened, in the zone activated above.
    first_week_day = week_start_day - timedelta(weeks=TREND_WEEKS - 1)
    weekly_builders = {
        timezone.localtime(row["week"]).date(): row["builders"]
        for row in Journal.objects
        .filter(created_at__gte=datetime.combine(first_week_day, time.min, tzinfo=timezone.get_current_timezone()))
        .annotate(week=TruncWeek("created_at"))
        .values("week").annotate(builders=Count("project__owner", distinct=True))
    }
    weekly_builder_rows = add_bars([
        {
            "label": f"Week of {monday:%b} {monday.day}",
            "value": weekly_builders.get(monday, 0),
            **({"sub": "so far"} if monday == week_start_day else {}),
        }
        for monday in (first_week_day + timedelta(weeks=n) for n in range(TREND_WEEKS))
    ])

    hours_counts = {
        row["day"]: row["seconds"]
        for row in Timelapse.objects
        .filter(journal__isnull=False, journal__created_at__gte=today_start - timedelta(days=TREND_DAYS - 1))
        .annotate(day=TruncDate("journal__created_at"))
        .values("day").annotate(seconds=Sum("tracked_seconds"))
    }

    hours_stats = {
        "today": _hours(minutes_today),
        "today_display": format_minutes(minutes_today),
        "devlogs_today": devlogs_today,
        "pending": _hours(pending_minutes),
        "pending_devlogs": pending_devlogs,
        "window": _hours(minutes_window),
        "window_days": WINDOW_DAYS,
        "devlogs_window": devlogs_window,
        "approved_window": _hours(approved_minutes_window),
        "avg_per_day": _avg(_hours(minutes_window), WINDOW_DAYS),
        "avg_per_day_7": _avg(_hours(minutes_last_7), SHORT_WINDOW_DAYS),
        "short_window_days": SHORT_WINDOW_DAYS,
        "last_7": _hours(minutes_last_7),
        "devlogs_last_7": devlogs_last_7,
        "this_week": _hours(minutes_this_week),
        "devlogs_this_week": journals_this_week.count(),
        "builders_this_week": builders_this_week,
        "weekly_builders": weekly_builder_rows,
        "week_start": week_start_day,
        "challenge": _hours(minutes_challenge),
        "devlogs_challenge": journals_challenge.count(),
        "builders_challenge": journals_challenge.values("project__owner").distinct().count(),
        "challenge_start": weeks.start_date(),
        "challenge_end": weeks.start_date() + timedelta(weeks=weeks.week_count(), days=-1),
        "all_time": _hours(total_time_minutes),
        "all_time_display": format_minutes(total_time_minutes),
        "devlogs_all_time": total_journals,
        "avg_per_builder": _avg(_hours(minutes_window), builders_window),
        "builders_window": builders_window,
        "builders_today": builders_today,
        "builders_last_7": builders_last_7,
        "avg_per_devlog_display": format_minutes(
            minutes_window / devlogs_window if devlogs_window else 0
        ),
        "avg_devlogs_per_day": _avg(devlogs_window, WINDOW_DAYS),
        "daily_hours": _daily_rows(
            hours_counts, TREND_DAYS, today, value=lambda seconds: round(seconds / 3600, 1)
        ),
    }

    total_projects = Project.objects.count()
    active_projects = Project.objects.filter(deleted=False).count()
    deleted_projects = Project.objects.filter(deleted=True).count()
    locked_projects = Project.objects.filter(locked=True, deleted=False).count()
    projects_last_7 = Project.objects.filter(created_at__gte=last_7).count()
    projects_last_30 = Project.objects.filter(created_at__gte=last_30).count()
    projects_with_ships = (
        Project.objects.filter(ships__isnull=False).distinct().count()
    )
    projects_no_ship = active_projects - (
        Project.objects.filter(deleted=False, ships__isnull=False).distinct().count()
    )

    editor_counts = {}
    for url in Project.objects.filter(deleted=False).values_list("editor_model_url", flat=True):
        editor = detect_editor(url) or "Unknown / other"
        editor_counts[editor] = editor_counts.get(editor, 0) + 1
    editor_breakdown = add_bars([
        {"label": name, "value": count}
        for name, count in sorted(editor_counts.items(), key=lambda kv: kv[1], reverse=True)
    ])

    avg_journal_minutes = (total_time_minutes / total_journals) if total_journals else 0
    avg_project_minutes = (total_time_minutes / active_projects) if active_projects else 0

    projects_stats = {
        "total": total_projects,
        "active": active_projects,
        "deleted": deleted_projects,
        "locked": locked_projects,
        "last_7": projects_last_7,
        "last_30": projects_last_30,
        "with_ships": projects_with_ships,
        "no_ship": projects_no_ship,
        "editor_breakdown": editor_breakdown,
        "total_journals": total_journals,
        "avg_journal_display": format_minutes(avg_journal_minutes),
        "avg_project_display": format_minutes(avg_project_minutes),
    }

    total_ships = Ship.objects.count()
    ships_last_7 = Ship.objects.filter(created_at__gte=last_7).count()
    status_counts = dict(
        Ship.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    )
    status_labels = dict(Ship.ShipStatus.choices)
    ship_by_status = add_bars([
        {"label": label, "value": status_counts.get(code, 0)}
        for code, label in status_labels.items()
    ])

    backlog = {
        "t1": status_counts.get(Ship.ShipStatus.T1_QUEUE, 0),
        "t2": status_counts.get(Ship.ShipStatus.T2_QUEUE, 0),
        "t3": status_counts.get(Ship.ShipStatus.T3_QUEUE, 0),
    }
    backlog_total = sum(backlog.values())

    pipeline = add_bars([
        {"label": "T1 review", "value": backlog["t1"]},
        {"label": "T2 review", "value": backlog["t2"]},
        {"label": "Fraud (T3)", "value": backlog["t3"]},
    ])

    finalized_ships = status_counts.get(Ship.ShipStatus.FINALIZED, 0)
    rejected_ships = status_counts.get(Ship.ShipStatus.REJECTED, 0)

    # Every lapse that has gone out in a ship, whatever became of the ship. A
    # rejected ship's lapses stay attached to it and are paid by the next one,
    # so they count as shipped here; the finalized figure is the share a T3
    # has signed off.
    shipped_journals = Journal.objects.filter(ship__isnull=False)
    shipped_minutes = tracked_minutes_for_journals(shipped_journals)
    finalized_minutes = tracked_minutes_for_journals(
        shipped_journals.filter(ship__status=Ship.ShipStatus.FINALIZED)
    )

    ships_stats = {
        "total": total_ships,
        "shipped_hours": _hours(shipped_minutes),
        "shipped_devlogs": shipped_journals.count(),
        "finalized_hours": _hours(finalized_minutes),
        "last_7": ships_last_7,
        "by_status": ship_by_status,
        "finalized": finalized_ships,
        "rejected": rejected_ships,
        "backlog_total": backlog_total,
        "pipeline": pipeline,
    }

    t1_total = T1.objects.count()
    t1_approved = T1.objects.filter(approved=True).count()
    t1_changes = T1.objects.filter(approved=False, changes_requested=True).count()
    t1_denied = T1.objects.filter(approved=False, changes_requested=False).count()

    t2_total = T2.objects.count()
    t2_decisions = dict(
        T2.objects.values_list("decision").annotate(n=Count("id")).values_list("decision", "n")
    )
    t2_decision_labels = dict(T2.Decision.choices)
    t2_breakdown = add_bars([
        {"label": label, "value": t2_decisions.get(code, 0)}
        for code, label in t2_decision_labels.items()
    ])
    t2_total_deductions = T2.objects.aggregate(t=Sum("deductions"))["t"] or 0

    t3_total = T3.objects.count()
    t3_decisions = dict(
        T3.objects.values_list("decision").annotate(n=Count("id")).values_list("decision", "n")
    )
    t3_decision_labels = dict(T3.Decision.choices)
    t3_breakdown = add_bars([
        {"label": label, "value": t3_decisions.get(code, 0)}
        for code, label in t3_decision_labels.items()
    ])
    t3_total_airtable_minutes = T3.objects.aggregate(t=Sum("airtable_time"))["t"] or 0

    # What was paid is read off the decision rather than recomputed: an hour is
    # worth one of three rates depending on the week it was recorded in, so
    # minutes and a multiplier no longer determine the pearls. Rows written
    # before that was true carry no figure, and for those the flat rate is
    # still exactly what they paid.
    total_payout_minutes = 0
    total_layers_paid = 0
    for payout_time, multiplier, paid in T3.objects.filter(
        decision=T3.Decision.APPROVE
    ).values_list("payout_time", "payout_multiplier", "payout_layers"):
        minutes = payout_time or 0
        total_payout_minutes += minutes
        total_layers_paid += (
            paid if paid is not None
            else layers_for_minutes(minutes, multiplier or PAYOUT_MULTIPLIER_DEFAULT)
        )

    reviews_stats = {
        "t1_total": t1_total,
        "t1_approved": t1_approved,
        "t1_denied": t1_denied,
        "t1_changes": t1_changes,
        "t1_approval_rate": _pct(t1_approved, t1_total),
        "t1_changes_rate": _pct(t1_changes, t1_total),
        "t1_denied_rate": _pct(t1_denied, t1_total),
        "t2_total": t2_total,
        "t2_breakdown": t2_breakdown,
        "t2_total_deductions_display": format_minutes(t2_total_deductions),
        "t3_total": t3_total,
        "t3_breakdown": t3_breakdown,
        "t3_payout_display": format_minutes(total_payout_minutes),
        "t3_airtable_display": format_minutes(t3_total_airtable_minutes),
        "total_layers_paid": total_layers_paid,
        "top_t1": reviewer_leaderboard("t1_reviews"),
        "top_t2": reviewer_leaderboard("t2_reviews"),
        "top_t3": reviewer_leaderboard("t3_reviews"),
    }

    total_items = Item.objects.count()
    active_items = Item.objects.filter(deleted=False).count()
    deleted_items = Item.objects.filter(deleted=True).count()

    total_orders = Order.objects.count()
    order_counts = dict(
        Order.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    )
    order_status_labels = dict(Order.OrderStatus.choices)
    order_breakdown = add_bars([
        {"label": label, "value": order_counts.get(code, 0)}
        for code, label in order_status_labels.items()
    ])

    pending_orders = order_counts.get(Order.OrderStatus.PENDING, 0)
    pending_value = (
        Order.objects.filter(status=Order.OrderStatus.PENDING).aggregate(t=Sum("cost"))["t"] or 0
    )
    layers_spent = (
        Order.objects.filter(status=Order.OrderStatus.FULFILLED).aggregate(t=Sum("cost"))["t"] or 0
    )
    refunded_layers = (
        Order.objects.filter(status=Order.OrderStatus.REFUNDED).aggregate(t=Sum("cost"))["t"] or 0
    )

    top_items = add_bars([
        {"label": row["item__name"], "value": row["n"], "sub": f'{row["q"] or 0} qty'}
        for row in (
            Order.objects.exclude(status=Order.OrderStatus.DENIED)
            .values("item__name")
            .annotate(n=Count("id"), q=Sum("quantity"))
            .order_by("-n")[:10]
        )
    ])

    fulfillers = (
        User.objects.annotate(n=Count("orders_fulfilled"))
        .filter(n__gt=0)
        .select_related("hackclub_profile")
        .order_by("-n")[:10]
    )
    top_fulfillers = add_bars([{"label": display_name(u), "value": u.n} for u in fulfillers])

    shop_stats = {
        "total_items": total_items,
        "active_items": active_items,
        "deleted_items": deleted_items,
        "total_orders": total_orders,
        "order_breakdown": order_breakdown,
        "pending_orders": pending_orders,
        "pending_value": pending_value,
        "layers_spent": layers_spent,
        "refunded_layers": refunded_layers,
        "top_items": top_items,
        "top_fulfillers": top_fulfillers,
    }

    staff_users = User.objects.filter(is_staff=True).count()
    slack_linked = Profile.objects.exclude(slack_id="").count()
    layers_in_circulation = Profile.objects.aggregate(t=Sum("layers"))["t"] or 0
    avg_layers = Profile.objects.aggregate(a=Avg("layers"))["a"] or 0

    top_holders = add_bars([
        {"label": display_name(p.user), "value": p.layers}
        for p in Profile.objects.select_related("user", "user__hackclub_profile")
        .order_by("-layers")[:10]
    ])

    users_stats = {
        "total": total_users,
        "staff": staff_users,
        "slack_linked": slack_linked,
        "last_7": signups_last_7,
        "layers_in_circulation": layers_in_circulation,
        "avg_layers": round(avg_layers, 1),
        "top_holders": top_holders,
    }

    total_audit = AuditLog.objects.count()
    audit_last_24h = AuditLog.objects.filter(created_at__gte=last_24h).count()
    audit_actions = add_bars([
        {"label": row["action"], "value": row["n"]}
        for row in AuditLog.objects.values("action").annotate(n=Count("id")).order_by("-n")[:15]
    ])

    audit_stats = {
        "total": total_audit,
        "last_24h": audit_last_24h,
        "actions": audit_actions,
    }

    return {
        "activity": activity_stats,
        "streaks": build_streak_stats(now),
        "hours": hours_stats,
        "projects": projects_stats,
        "ships": ships_stats,
        "reviews": reviews_stats,
        "shop": shop_stats,
        "users": users_stats,
        "audit": audit_stats,
    }


# The dates in the context, which JSON flattens to ISO strings on the way into
# a snapshot and which have to come back as dates for the template's |date.
SNAPSHOT_DATE_KEYS = [
    ("hours", "week_start"),
    ("hours", "challenge_start"),
    ("hours", "challenge_end"),
]


def take_snapshot(now=None):
    """Write (or rewrite) the snapshot for the local day `now` falls on."""
    now = now or timezone.now()
    data = json.loads(json.dumps(build_metrics(now), cls=DjangoJSONEncoder))
    snapshot, _ = MetricsSnapshot.objects.update_or_create(
        day=timezone.localdate(now, weeks.zone()),
        defaults={"taken_at": now, "data": data},
    )
    return snapshot


def _snapshot_context(snapshot):
    context = snapshot.data
    for section, key in SNAPSHOT_DATE_KEYS:
        value = context.get(section, {}).get(key)
        if value:
            context[section][key] = parse_date(value)
    return context


@staff_member_required
@check_perms(["atlantis_site.organizer"])
@timezone.override(settings.CHALLENGE_TIMEZONE)
def metrics(request):
    """The live page, or with ?day=YYYY-MM-DD the snapshot taken at the end of
    that day. The timezone override is for the template: |date renders the
    generated time in Eastern, the zone the snapshot days are named in."""
    snapshot = None
    requested = request.GET.get("day")
    if requested:
        try:
            day = parse_date(requested)
        except ValueError:
            day = None
        if day is None:
            raise Http404("Not a date.")
        snapshot = get_object_or_404(MetricsSnapshot, day=day)
        context = _snapshot_context(snapshot)
        context["generated_at"] = snapshot.taken_at
    else:
        now = timezone.now()
        context = build_metrics(now)
        context["generated_at"] = now

    context["snapshot"] = snapshot
    context["snapshot_days"] = list(MetricsSnapshot.objects.values_list("day", flat=True))
    return render(request, "root/metrics.html", context)
