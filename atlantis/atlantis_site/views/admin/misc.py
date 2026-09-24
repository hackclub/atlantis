from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.core.paginator import Paginator

from ...models import AuditLog
from ..helpers import check_perms

# What a reviewer lead audits: the decisions reviewers make and what is said
# about them. The rest of the log (addresses viewed, users edited, orders) is
# not review work and stays organizer-only.
REVIEW_AUDIT_ACTIONS = [
    "t1_decision",
    "t1_rollback",
    "t2_decision",
    "t3_decision",
    "internal_comment",
    "lock_project",
    "unlock_project",
]

def _audit_page(request, logs, template_context):
    action_filter = request.GET.get("action", "").strip()
    actor_filter = request.GET.get("actor", "").strip()
    target_type_filter = request.GET.get("target_type", "").strip()

    # Worked out from the scoped entries, so a page never offers a filter
    # that leads outside its scope.
    actions = logs.order_by("action").values_list("action", flat=True).distinct()

    target_types = sorted({
        target.split(" ", 1)[0]
        for target in logs.exclude(target="").values_list("target", flat=True).distinct()
        if target
    })

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
        **template_context,
    })

@staff_member_required
@check_perms(["atlantis_site.organizer"])
def audit_log(request):
    return _audit_page(request, AuditLog.objects.select_related("actor").all(), {
        "heading": "Audit Log",
        "subheading": "A record of every admin action",
        "show_request": True,
    })

@staff_member_required
@check_perms(["atlantis_site.reviewer_lead", "atlantis_site.organizer"])
def review_audit(request):
    logs = AuditLog.objects.select_related("actor").filter(action__in=REVIEW_AUDIT_ACTIONS)
    return _audit_page(request, logs, {
        "heading": "Review Audit",
        "subheading": "Every review decision, rollback and internal comment",
        # A reviewer's IP address is not something auditing their reviews needs.
        "show_request": False,
    })
