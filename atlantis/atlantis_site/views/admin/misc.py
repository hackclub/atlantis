from django.shortcuts import render
from django.db.models import Q
from django.core.paginator import Paginator

from django.contrib.auth.models import User

from ...models import AuditLog, Project, Ship, TimelapseReview
from ..helpers import check_perms, display_name, format_minutes, staff_member_required

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
    "timelapse_review",
    "edit_timelapse_review",
]

# How many of a user's lapse reviews the audit lists at once. Far more than one
# person's eight weeks of journals, so in practice it's all of them.
USER_LAPSE_REVIEW_LIMIT = 200

def _audit_page(request, logs, template_context, extra_filter=None):
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
    if extra_filter is not None:
        logs = logs.filter(extra_filter)

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
    return _audit_page(request, AuditLog.objects.select_related("actor", "actor__hackclub_profile").all(), {
        "heading": "Audit Log",
        "subheading": "A record of every admin action",
        "show_request": True,
    })

def _matching_users(query):
    """Users whose username or Slack handle contains `query`, @ optional."""
    query = query.lstrip("@")
    if not query:
        return User.objects.none()
    return User.objects.filter(
        Q(username__icontains=query) | Q(hackclub_profile__slack_username__icontains=query)
    )


def _about_users(users):
    """A filter for review entries about these users' work.

    Entries name what they acted on as "Project #id (...)" or "Ship #id (...)",
    so the user's projects and ships are matched by those prefixes. The space
    keeps Project #1 from also matching Project #12.
    """
    project_ids = list(Project.objects.filter(owner__in=users).values_list("id", flat=True))
    ship_ids = list(Ship.objects.filter(project__owner__in=users).values_list("id", flat=True))
    about = Q(pk__in=[])
    for project_id in project_ids:
        about |= Q(target__startswith=f"Project #{project_id} ")
    for ship_id in ship_ids:
        about |= Q(target__startswith=f"Ship #{ship_id} ")
    return about


def _lapse_reviews_for(users):
    """The lapse reviews of these users' journals, newest first, ready to list."""
    reviews = list(
        TimelapseReview.objects
        .filter(journal__project__owner__in=users)
        .select_related(
            "reviewer", "reviewer__hackclub_profile", "journal", "journal__project",
            "journal__project__owner", "journal__project__owner__hackclub_profile",
        )
        .prefetch_related("removals", "edits", "journal__timelapses")
        [:USER_LAPSE_REVIEW_LIMIT]
    )
    for review in reviews:
        removed = sum(r.duration_seconds for r in review.removals.all())
        tracked = sum(t.tracked_seconds or 0 for t in review.journal.timelapses.all())
        review.owner_name = display_name(review.journal.project.owner)
        review.reviewer_name = display_name(review.reviewer)
        review.removed_total_display = format_minutes(removed // 60)
        review.tracked_total_display = format_minutes(tracked // 60)
        review.cut_count = len(review.removals.all())
        review.edit_count = len(review.edits.all())
    return reviews


@staff_member_required
@check_perms(["atlantis_site.reviewer_lead", "atlantis_site.organizer"])
def review_audit(request):
    """Review decisions, narrowable to the work of one user.

    `?user=` matches the person whose work was reviewed, not the reviewer
    (Actor already does that). It narrows the log to entries about their
    projects and ships, and lists their lapse reviews so each can be opened
    and corrected.
    """
    logs = AuditLog.objects.select_related("actor", "actor__hackclub_profile").filter(action__in=REVIEW_AUDIT_ACTIONS)
    user_filter = request.GET.get("user", "").strip()
    users = _matching_users(user_filter)
    extra_filter = _about_users(users) if user_filter else None
    return _audit_page(request, logs, {
        "heading": "Review Audit",
        "subheading": "Every review decision, rollback and internal comment",
        # A reviewer's IP address is not something auditing their reviews needs.
        "show_request": False,
        "show_user_filter": True,
        "user_filter": user_filter,
        "lapse_reviews": _lapse_reviews_for(users) if user_filter else None,
        "lapse_review_limit": USER_LAPSE_REVIEW_LIMIT,
    }, extra_filter=extra_filter)
