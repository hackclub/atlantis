from datetime import timedelta

from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Sum, Avg
from django.contrib.auth import get_user_model
from django.utils import timezone

from ...models import (
    AuditLog,
    Profile,
    Project,
    Ship,
    Journal,
    T1,
    T2,
    T3,
    Print,
    Item,
    Order,
)

from ...models import detect_editor
from ..helpers import (
    check_perms,
    layers_for_minutes,
    add_bars as _add_bars,
    display_name as _display_name,
    reviewer_leaderboard as _reviewer_leaderboard,
)

def _fmt_minutes(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60}h {minutes % 60}m"


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else 0.0


@staff_member_required
@check_perms(["layered_site.organizer"])
def metrics(request):
    User = get_user_model()
    now = timezone.now()
    last_7 = now - timedelta(days=7)
    last_30 = now - timedelta(days=30)
    last_24h = now - timedelta(hours=24)

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
    editor_breakdown = _add_bars([
        {"label": name, "value": count}
        for name, count in sorted(editor_counts.items(), key=lambda kv: kv[1], reverse=True)
    ])

    total_journals = Journal.objects.count()
    total_time_minutes = Journal.objects.aggregate(t=Sum("time_spent"))["t"] or 0
    avg_journal_minutes = Journal.objects.aggregate(a=Avg("time_spent"))["a"] or 0
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
        "total_time_display": _fmt_minutes(total_time_minutes),
        "total_time_hours": round(total_time_minutes / 60, 1),
        "avg_journal_display": _fmt_minutes(avg_journal_minutes),
        "avg_project_display": _fmt_minutes(avg_project_minutes),
    }

    total_ships = Ship.objects.count()
    ships_last_7 = Ship.objects.filter(created_at__gte=last_7).count()
    status_counts = dict(
        Ship.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    )
    status_labels = dict(Ship.ShipStatus.choices)
    ship_by_status = _add_bars([
        {"label": label, "value": status_counts.get(code, 0)}
        for code, label in status_labels.items()
    ])

    backlog = {
        "t1": status_counts.get(Ship.ShipStatus.T1_QUEUE, 0),
        "print_queue": status_counts.get(Ship.ShipStatus.PRINT_QUEUE, 0),
        "being_printed": status_counts.get(Ship.ShipStatus.BEING_PRINTED, 0),
        "t2": status_counts.get(Ship.ShipStatus.T2_QUEUE, 0),
        "t3": status_counts.get(Ship.ShipStatus.T3_QUEUE, 0),
    }
    backlog_total = sum(backlog.values())

    pipeline = _add_bars([
        {"label": "T1 review", "value": backlog["t1"]},
        {"label": "Print queue", "value": backlog["print_queue"]},
        {"label": "Being printed", "value": backlog["being_printed"]},
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
    t1_print_requested = T1.objects.filter(print=True).count()

    t2_total = T2.objects.count()
    t2_decisions = dict(
        T2.objects.values_list("decision").annotate(n=Count("id")).values_list("decision", "n")
    )
    t2_decision_labels = dict(T2.Decision.choices)
    t2_breakdown = _add_bars([
        {"label": label, "value": t2_decisions.get(code, 0)}
        for code, label in t2_decision_labels.items()
    ])
    t2_total_deductions = T2.objects.aggregate(t=Sum("deductions"))["t"] or 0

    t3_total = T3.objects.count()
    t3_decisions = dict(
        T3.objects.values_list("decision").annotate(n=Count("id")).values_list("decision", "n")
    )
    t3_decision_labels = dict(T3.Decision.choices)
    t3_breakdown = _add_bars([
        {"label": label, "value": t3_decisions.get(code, 0)}
        for code, label in t3_decision_labels.items()
    ])
    t3_total_airtable_minutes = T3.objects.aggregate(t=Sum("airtable_time"))["t"] or 0

    total_payout_minutes = 0
    total_layers_paid = 0
    for payout_time in T3.objects.filter(decision=T3.Decision.APPROVE).values_list("payout_time", flat=True):
        total_payout_minutes += payout_time or 0
        total_layers_paid += layers_for_minutes(payout_time or 0)

    print_total = Print.objects.count()
    print_decisions = dict(
        Print.objects.values_list("decision").annotate(n=Count("id")).values_list("decision", "n")
    )
    print_decision_labels = dict(Print.Decision.choices)
    print_breakdown = _add_bars([
        {"label": label, "value": print_decisions.get(code, 0)}
        for code, label in print_decision_labels.items()
    ])
    print_weight_agg = Print.objects.filter(weight__isnull=False).aggregate(
        total=Sum("weight"), avg=Avg("weight")
    )
    prints_completed = Print.objects.filter(finished_time__isnull=False).count()

    reviews_stats = {
        "t1_total": t1_total,
        "t1_approved": t1_approved,
        "t1_denied": t1_denied,
        "t1_approval_rate": _pct(t1_approved, t1_total),
        "t1_denied_rate": _pct(t1_denied, t1_total),
        "t1_print_requested": t1_print_requested,
        "t2_total": t2_total,
        "t2_breakdown": t2_breakdown,
        "t2_total_deductions_display": _fmt_minutes(t2_total_deductions),
        "t3_total": t3_total,
        "t3_breakdown": t3_breakdown,
        "t3_payout_display": _fmt_minutes(total_payout_minutes),
        "t3_airtable_display": _fmt_minutes(t3_total_airtable_minutes),
        "total_layers_paid": total_layers_paid,
        "print_total": print_total,
        "print_breakdown": print_breakdown,
        "prints_completed": prints_completed,
        "print_total_weight": print_weight_agg["total"] or 0,
        "print_avg_weight": round(print_weight_agg["avg"] or 0, 1),
        "top_t1": _reviewer_leaderboard("t1_reviews"),
        "top_t2": _reviewer_leaderboard("t2_reviews"),
        "top_t3": _reviewer_leaderboard("t3_reviews"),
        "top_printers": _reviewer_leaderboard("prints"),
    }

    total_items = Item.objects.count()
    active_items = Item.objects.filter(deleted=False).count()
    deleted_items = Item.objects.filter(deleted=True).count()

    total_orders = Order.objects.count()
    order_counts = dict(
        Order.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    )
    order_status_labels = dict(Order.OrderStatus.choices)
    order_breakdown = _add_bars([
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

    top_items = _add_bars([
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
    top_fulfillers = _add_bars([{"label": _display_name(u), "value": u.n} for u in fulfillers])

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

    total_users = User.objects.count()
    staff_users = User.objects.filter(is_staff=True).count()
    slack_linked = Profile.objects.exclude(slack_id="").count()
    layers_in_circulation = Profile.objects.aggregate(t=Sum("layers"))["t"] or 0
    avg_layers = Profile.objects.aggregate(a=Avg("layers"))["a"] or 0
    users_last_7 = User.objects.filter(date_joined__gte=last_7).count()

    top_holders = _add_bars([
        {"label": _display_name(p.user), "value": p.layers}
        for p in Profile.objects.select_related("user", "user__hackclub_profile")
        .order_by("-layers")[:10]
    ])

    users_stats = {
        "total": total_users,
        "staff": staff_users,
        "slack_linked": slack_linked,
        "last_7": users_last_7,
        "layers_in_circulation": layers_in_circulation,
        "avg_layers": round(avg_layers, 1),
        "top_holders": top_holders,
    }

    total_audit = AuditLog.objects.count()
    audit_last_24h = AuditLog.objects.filter(created_at__gte=last_24h).count()
    audit_actions = _add_bars([
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
        "projects": projects_stats,
        "ships": ships_stats,
        "reviews": reviews_stats,
        "shop": shop_stats,
        "users": users_stats,
        "audit": audit_stats,
    })
