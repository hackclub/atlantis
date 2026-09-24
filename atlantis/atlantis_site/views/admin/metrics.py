from datetime import datetime, time, timedelta

from django.shortcuts import render
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Sum, Avg
from django.db.models.functions import TruncDate
from django.contrib.auth import get_user_model
from django.utils import timezone

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
    Order,
    Timelapse,
    PAYOUT_MULTIPLIER_DEFAULT,
    detect_editor,
)
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

# How many days the daily bar charts go back. Short enough that each bar is
# still readable in a column of them.
TREND_DAYS = 14


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


@staff_member_required
@check_perms(["atlantis_site.organizer"])
@timezone.override(settings.CHALLENGE_TIMEZONE)
def metrics(request):
    """All "today"/"per day" windows here are cut in CHALLENGE_TIMEZONE (US
    Eastern), not the server's UTC — the decorator above activates it for the
    whole view, so timezone.localdate()/localtime(), the TruncDate groupings,
    and the template's |date rendering of generated_at all follow it."""
    User = get_user_model()
    now = timezone.now()
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
    pending_journals = Journal.objects.filter(timelapse_review__isnull=True)
    reviewed_window = Journal.objects.filter(timelapse_review__reviewed_at__gte=last_30)

    minutes_today = tracked_minutes_for_journals(journals_today)
    minutes_window = tracked_minutes_for_journals(journals_window)
    minutes_last_7 = tracked_minutes_for_journals(journals_last_7)
    minutes_this_week = tracked_minutes_for_journals(journals_this_week)
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
        "week_start": week_start_day,
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

    ships_stats = {
        "total": total_ships,
        "last_7": ships_last_7,
        "by_status": ship_by_status,
        "finalized": finalized_ships,
        "rejected": rejected_ships,
        "backlog_total": backlog_total,
        "pipeline": pipeline,
    }

    t1_total = T1.objects.count()
    t1_approved = T1.objects.filter(approved=True).count()
    t1_denied = T1.objects.filter(approved=False).count()

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
        "t1_approval_rate": _pct(t1_approved, t1_total),
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

    return render(request, "root/metrics.html", {
        "generated_at": now,
        "activity": activity_stats,
        "hours": hours_stats,
        "projects": projects_stats,
        "ships": ships_stats,
        "reviews": reviews_stats,
        "shop": shop_stats,
        "users": users_stats,
        "audit": audit_stats,
    })
