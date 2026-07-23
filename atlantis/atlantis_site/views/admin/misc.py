from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.core.paginator import Paginator

from ...models import AuditLog
from ..helpers import check_perms

@staff_member_required
@check_perms(["atlantis_site.organizer"])
def audit_log(request):
    logs = AuditLog.objects.select_related("actor").all()

    action_filter = request.GET.get("action", "").strip()
    actor_filter = request.GET.get("actor", "").strip()
    target_type_filter = request.GET.get("target_type", "").strip()

    if action_filter:
        logs = logs.filter(action=action_filter)
    if actor_filter:
        logs = logs.filter(
            Q(actor__username__icontains=actor_filter)
            | Q(actor__first_name__icontains=actor_filter)
            | Q(actor__last_name__icontains=actor_filter)
        )
    if target_type_filter:
        logs = logs.filter(target__startswith=f"{target_type_filter} #")

    actions = AuditLog.objects.order_by("action").values_list("action", flat=True).distinct()

    target_types = sorted({
        target.split(" ", 1)[0]
        for target in AuditLog.objects.exclude(target="").values_list("target", flat=True).distinct()
        if target
    })

    paginator = Paginator(logs, 50)
    page = paginator.get_page(request.GET.get("page"))

    return render(request, "root/audit_log.html", {
        "page": page,
        "logs": page.object_list,
        "actions": actions,
        "action_filter": action_filter,
        "actor_filter": actor_filter,
        "target_types": target_types,
        "target_type_filter": target_type_filter,
    })
