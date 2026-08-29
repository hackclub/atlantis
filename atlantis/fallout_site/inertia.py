"""Inertia glue for the Fallout→Atlantis frontend.

Wraps `django-inertia` so that:
  * per-request shared props (auth, flash, admin_permissions) are injected
    *per request* — django-inertia's default shared props are a class-level
    singleton which would leak user A's auth onto user B's page;
  * asset tags come from Vite's `manifest.json` (built into ./dist).
"""

import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.utils.safestring import mark_safe

from django_inertia.core import Inertia
from django_inertia.props import StaticProp

from atlantis_site.models import Profile

ASSETS_DIR = Path(settings.BASE_DIR) / "dist"
MANIFEST_PATH = ASSETS_DIR / "manifest.json"


# (manifest mtime_ns, token) — see asset_version().
_version_cache = (None, None)


def asset_version():
    """A stable version token for the Inertia asset-version check.

    The middleware resolves this once per request (so a rebuild invalidates
    connected clients), so memoise the hash against the manifest's mtime
    rather than re-reading and hashing the file on every request.
    """
    global _version_cache
    try:
        mtime = MANIFEST_PATH.stat().st_mtime_ns
    except OSError:
        return "dev"
    cached_mtime, cached_token = _version_cache
    if cached_mtime == mtime:
        return cached_token
    try:
        token = hashlib.md5(MANIFEST_PATH.read_bytes()).hexdigest()[:12]
    except OSError:
        return "dev"
    _version_cache = (mtime, token)
    return token


def _manifest():
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _user_dict(user):
    if not user or not user.is_authenticated:
        return None
    profile = getattr(user, "hackclub_profile", None)
    display = profile.slack_username if profile and profile.slack_username else user.username
    avatar = (
        profile.slack_pfp_url
        if profile and profile.slack_pfp_url
        else "https://cdn.hackclub.com/019ee160-b8f6-7920-aca0-6e35fffc2b6a/slack_hash_256.png"
    )
    return {
        "id": user.id,
        "display_name": display,
        "avatar": avatar,
        "roles": [],
        "is_admin": user.is_superuser,
        "is_staff": user.is_staff,
        "is_banned": False,
        "ban_type": None,
        "is_trial": False,
        "is_onboarded": True,
        "professor_enrolled": False,
        "professor_recently_enrolled": False,
        "professor_enrollment_eligible": False,
    }


def _admin_permissions(user):
    from fallout_site.models import TimeAuditReview

    return {
        "is_admin": user.is_superuser,
        "is_hcb": False,
        "can_review_time_audits": user.has_perm("atlantis_site.timelapse_review"),
        "can_review_requirements_checks": False,
        "can_review_design_reviews": False,
        "can_review_build_reviews": False,
        "performance_enabled": False,
    }


def _admin_stats(user):
    from fallout_site.models import TimeAuditReview

    return {
        "users_count": 0,
        "projects_count": 0,
        "pending_reviews_count": 0,
        "pending_time_audits_count": TimeAuditReview.objects.filter(status="pending").count(),
        "pending_requirements_checks_count": 0,
        "pending_design_reviews_count": 0,
        "pending_build_reviews_count": 0,
        "pending_design_review_backfills_count": 0,
        "pending_build_review_backfills_count": 0,
        "flagged_projects_count": 0,
    }


def _shared_props(request):
    """Per-request shared props as StaticProps so partial reloads keep them."""
    user = request.user
    return {
        "auth": StaticProp({"user": _user_dict(user) if user.is_authenticated else None}),
        "impersonation": StaticProp(None),
        "flash": StaticProp({"alert": "", "notice": ""}),
        "features": StaticProp({"grant_fulfillment": True}),
        "sign_in_path": StaticProp("/auth/login/"),
        "sign_out_path": StaticProp("/auth/logout/"),
        "trial_session_path": StaticProp("/"),
        "rsvp_path": StaticProp("/"),
        "has_unread_mail": StaticProp(False),
        "current_streak": StaticProp(0),
        "unsubmitted_hours": StaticProp(None),
        "streak_freezes": StaticProp(0),
        "identity_gate": StaticProp(None),
        "show_feedback_banner": StaticProp(False),
        "errors": StaticProp({}),
        "admin_permissions": StaticProp(_admin_permissions(user)),
        "admin_stats": StaticProp(_admin_stats(user) if user.is_authenticated else {}),
    }


def _asset_tags():
    """<link>/<script> tags for the built bundle, read from the Vite manifest.

    The Vite "classic" manifest nests a build's CSS under the entry key's
    `css` array; emit those links plus the entry/module scripts.
    """
    manifest = _manifest()
    if not manifest:
        return []
    tags = []
    seen = set()

    def emit(file):
        if file in seen or not file:
            return
        seen.add(file)
        if file.endswith(".css"):
            tags.append(f'<link rel="stylesheet" href="/static/{file}">')
        elif file.endswith(".js"):
            tags.append(f'<script type="module" src="/static/{file}"></script>')

    for entry in manifest.values():
        for css in entry.get("css", []) or []:
            emit(css)
        emit(entry.get("file", ""))
        for dep in entry.get("imports", []) or []:
            if dep in manifest:
                emit(manifest[dep].get("file", ""))
    return tags


def render(request, component, props=None, view_data=None):
    props = dict(props or {})
    props.update(_shared_props(request))
    view_data = dict(view_data or {})
    view_data["vite_assets"] = mark_safe("\n".join(_asset_tags()))
    return Inertia.render(request, component, props=props, view_data=view_data)