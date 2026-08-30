"""Serializers mirroring Fallout's `TimeAuditSerialization` concern.

The payload shapes are kept byte-for-byte identical to what the copied
frontend (`app/frontend/pages/admin/reviews/time_audits/show.tsx`) expects.
Lookout-only: every recording is a LookoutTimelapse backed by an
atlantis_site LookoutSession.
"""

from django.conf import settings
from django.urls import reverse

from atlantis_site.models import Journal, LookoutSession

# Lookout stitches one recorded minute into one video second.
VIDEO_SECONDS_PER_TRACKED_SECOND = 60


def _profile_display(user):
    profile = getattr(user, "hackclub_profile", None)
    if profile and profile.slack_username:
        return profile.slack_username
    return user.username


def _avatar(user, request):
    profile = getattr(user, "hackclub_profile", None)
    if profile and profile.slack_pfp_url:
        return profile.slack_pfp_url
    return "https://cdn.hackclub.com/assets/avatar.png"


def _lookout_base(request):
    """The base URL for Lookout media in this deployment.

    In dev we serve generated sample media from the mock Lookout server;
    in production this would be the real Lookout media URLs.
    """
    return f"{settings.LOOKOUT_BASE_URL.rstrip('/')}/api/media"


def _recording_duration(session):  # lookoutsession
    return session.tracked_seconds or 0


def serialize_ta_recording(session, request, annotations=None):
    """A `ReviewRecording` for one LookoutSession."""
    base = _lookout_base(request)
    recording = {
        "id": session.id,
        "type": "LookoutTimelapse",
        "duration": _recording_duration(session),
        "name": getattr(session, "name", None) or "Lookout session",
        "inactive_segments": [],
        "inactive_percentage": None,
        "activity_checked": False,
        "playback_url": f"{base}/{session.session_id}/video.mp4",
        "thumbnail_url": f"{base}/{session.session_id}/thumbnail.jpg",
    }
    # Add existing annotations for this recording
    if annotations and "recordings" in annotations:
        rec_annotations = annotations["recordings"].get(str(session.id))
        if rec_annotations:
            recording["description"] = rec_annotations.get("description", "")
            recording["stretch_multiplier"] = rec_annotations.get("stretch_multiplier", 1)
            recording["segments"] = rec_annotations.get("segments", [])
            recording["reviewer_id"] = rec_annotations.get("reviewer_id")
    return recording


def serialize_ta_journal_entry(journal, request, annotations=None):
    """Mirror of Fallout's serialize_ta_journal_entry.

    The Atlantis journal has no free-text content; its title and image ARE the
    entry, and the review page's right-hand journal panel shows exactly that.
    """
    sessions = list(journal.timelapses.all())
    return {
        "id": journal.id,
        "content_html": _journal_content_html(journal),
        "images": [journal.image_display_url] if journal.image_display_url else [],
        "author_display_name": _profile_display(journal.project.owner),
        "author_avatar": _avatar(journal.project.owner, request),
        "created_at": journal.created_at.strftime("%b %d, %Y"),
        "created_at_iso": journal.created_at.isoformat(),
        "recordings": [serialize_ta_recording(s, request, annotations) for s in sessions],
        "total_duration": sum(_recording_duration(s) for s in sessions),
        "in_ship": True,  # Always true since we're reviewing all project journals
    }


def _journal_content_html(journal):
    """The Atlantis journal's title rendered as the entry's (markdown) content."""
    title = (journal.title or "").strip()
    return f"<h1>{title}</h1>" if title else ""


def serialize_ta_project_context(project, request):
    owner = project.owner
    collaborators = [
        {"id": u.id, "display_name": _profile_display(u), "avatar": _avatar(u, request)}
        for u in project.followers.all()[:20]
    ]
    return {
        "id": project.id,
        "name": project.title,
        "description": project.description,
        "repo_link": None,
        "demo_link": None,
        "user_id": owner.id,
        "user_display_name": _profile_display(owner),
        "user_avatar": _avatar(owner, request),
        "collaborators": collaborators,
    }


def serialize_review_detail(review):
    # For "ship" mode, frontend expects a ship_id. Use the ship from the first journal
    # or the oldest ship on the project.
    first_journal = review.project.journals.order_by("created_at").first()
    ship_id = first_journal.ship_id if first_journal else None
    return {
        "id": review.id,
        "project_id": review.project_id,
        "ship_id": ship_id,
        "status": review.status,
        "feedback": review.feedback or None,
        "approved_public_seconds": review.approved_public_seconds,
        "annotations": review.annotations or {"recordings": {}},
        "reviewed_journal_ids": review.reviewed_journal_ids or [],
        "reviewer_display_name": _profile_display(review.reviewer) if review.reviewer_id else None,
        "created_at": review.created_at.strftime("%B %d, %Y"),
    }


def serialize_review_row(review, flagged_project_ids=frozenset()):
    project = review.project
    oldest_unreviewed = review.get_oldest_unreviewed_journal()
    return {
        "id": review.id,
        "project_id": project.id,
        "project_name": project.title,
        "user_display_name": _profile_display(project.owner),
        "status": review.status,
        "project_flagged": project.id in flagged_project_ids,
        "reviewer_display_name": _profile_display(review.reviewer) if review.reviewer_id else None,
        "created_at": review.created_at.strftime("%b %d, %Y"),
        "waiting_since": oldest_unreviewed.created_at.isoformat() if oldest_unreviewed else project.created_at.isoformat(),
        "cycle_started_at": project.created_at.isoformat(),
        "is_claimed": review.claimed(),
        "claimed_by_display_name": _profile_display(review.claimed_by) if review.claimed() else None,
        "sibling_approved": False,
        "previously_reviewed_by_me": False,
        "approved_public_hours": None,
    }


def serialize_reviewer_notes(notes):
    return [
        {
            "id": n.id,
            "body": n.body,
            "ship_id": n.ship_id,
            "review_stage": n.review_stage or None,
            "author_display_name": _profile_display(n.author),
            "author_avatar": _avatar(n.author, None),
            "author_id": n.author_id,
            "created_at": n.created_at.isoformat(),
            "updated_at": n.updated_at.isoformat(),
        }
        for n in notes
    ]