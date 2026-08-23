from django.contrib.auth.decorators import user_passes_test
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db.models import Count, Exists, F, IntegerField, OuterRef, Sum
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from ..models import (
    AuditLog, InternalComment, Journal, LookoutSession, TimelapseRemoval,
    detect_editor
)

from functools import wraps

from slack_sdk.errors import SlackApiError
from slack_sdk import WebClient

from urllib.parse import urlparse, urljoin, urlunparse

from requests.adapters import HTTPAdapter

from PIL import Image

import os
import uuid
import requests
import socket
import ipaddress

ALLOWED_IMAGE_FORMATS = {
    "PNG": ".png",
    "JPEG": ".jpg",  
    "GIF": ".gif",
    "WEBP": ".webp",
}

# Longest URL we will look at anywhere in here. Every URL column in models.py
# is 2048 or smaller, so nothing legitimate is turned away, and it keeps
# attacker-supplied strings from being walked at all.
MAX_URL_LENGTH = 2048

PRINTABLES_HOSTS = frozenset({"printables.com", "www.printables.com"})

# The only ports _safe_head will connect to, per scheme.
ALLOWED_URL_PORTS = {"http": 80, "https": 443}

slack_client = WebClient(token=settings.SLACK_TOKEN, timeout=5)

def check_perms(perms):
    def check_perms_internal(user):
        for perm in perms:
            if user.has_perm(perm):
                return True
        return False
    return user_passes_test(check_perms_internal)

def is_valid_printables_url(value):
    """True when value is an https URL whose host really is printables.com.

    Compares the parsed host against an allowlist rather than matching the URL
    with a regex: a pattern ending in `.*$` backtracks over the whole string on
    input it can't match, and reading the host out of urlparse is both linear
    and harder to get wrong. It also drops `https://printables.com@evil.com`,
    where the lookalike is only the userinfo and the real host is evil.com.
    """
    if not value or len(value) > MAX_URL_LENGTH:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port not in (None, ALLOWED_URL_PORTS["https"]):
        return False
    return (parsed.hostname or "").lower() in PRINTABLES_HOSTS

def layers_for_minutes(minutes):
    tenths_of_hour = minutes // 6
    return round(tenths_of_hour * 0.5)

# All reported time comes from Lookout timelapses linked to journal entries.
# Nothing is self-reported, so these are the only ways to total time up.
def tracked_seconds_for_journals(journals):
    return LookoutSession.objects.filter(journal__in=journals).aggregate(
        total=Sum("tracked_seconds")
    )["total"] or 0

def tracked_minutes_for_journals(journals):
    return tracked_seconds_for_journals(journals) // 60

# Time a timelapse reviewer cut out of those journals, and what's left after it.
# The approved_* pair is the internal number — what a project actually gets paid
# for — so it belongs on /root pages and in the T2/T3 maths, never on anything
# the owner can load. Their side of the site keeps using tracked_*, which is why
# a removal can't quietly move a ship gate under them.
def removed_seconds_for_journals(journals):
    return TimelapseRemoval.objects.filter(review__journal__in=journals).aggregate(
        total=Sum(F("end_seconds") - F("start_seconds"), output_field=IntegerField())
    )["total"] or 0

def approved_seconds_for_journals(journals):
    return max(
        tracked_seconds_for_journals(journals) - removed_seconds_for_journals(journals),
        0,
    )

def approved_minutes_for_journals(journals):
    return approved_seconds_for_journals(journals) // 60

def timelapse_cleared_ships(ships):
    """Only the ships whose every journal has passed internal timelapse review.

    Ships still waiting are held out of the regular review queues rather than
    marked as held: their owner sees no change at all. Ships with no journals
    (the DEBUG-only ship bypass) are left in — there's no footage to review,
    which is why this asks whether an unreviewed journal exists rather than
    joining, where a ship with no journals at all matches on the NULL side.
    """
    return ships.exclude(
        Exists(Journal.objects.filter(ship=OuterRef("pk"), timelapse_review__isnull=True))
    )

def format_minutes(minutes):
    return f"{minutes // 60}h {minutes % 60}m"

# Local-development escape hatch. Recording real Lookout footage to clear the
# 3h/2h ship gates makes the ship -> review -> payout chain untestable
# by hand, so organizers running with DEBUG on may ship with no journals and no
# tracked time. BOTH conditions are required: with DEBUG off this is dead code
# no matter who is signed in, so an organizer in production gains nothing.
def can_bypass_ship_requirements(user):
    return bool(settings.DEBUG and user.has_perm("atlantis_site.organizer"))

def internal_comments_for_project(project):
    """Reviewer-only comments on every ship of a project, newest first."""
    return (
        InternalComment.objects.filter(ship__project=project)
        .select_related("author", "author__hackclub_profile")
    )

def build_review_history(ship):
    """Everything reviewers did to a ship — decisions and internal
    comments — as one oldest-first list. /root pages only: it carries internal
    notes the project owner must never see."""
    events = []
    for t1 in ship.t1_reviews.all():
        events.append({
            "type": "t1",
            "label": "T1 Review",
            "review": t1,
            "actor": display_name(t1.reviewer),
            "at": t1.reviewed_at,
        })
    for t2 in ship.t2_reviews.all():
        events.append({
            "type": "t2",
            "label": "T2 Review",
            "review": t2,
            "actor": display_name(t2.reviewer),
            "at": t2.reviewed_at,
        })
    # Comments span the whole project: a note left on an earlier ship is still
    # what a reviewer needs to see on a reship, so it's flagged, not hidden.
    for comment in internal_comments_for_project(ship.project):
        events.append({
            "type": "comment",
            "label": "Internal comment",
            "comment": comment,
            "actor": display_name(comment.author),
            "other_ship": comment.ship_id != ship.id,
            "at": comment.created_at,
        })
    events.sort(key=lambda e: e["at"])
    return events

def build_journal_timeline(journals, ships):
    """A project's journals and ships as one newest-first list.

    /root pages only, like build_review_history: the time on a ship here is the
    approved figure, net of anything timelapse review took off it, which the
    owner is never shown."""
    events = []
    for journal in journals:
        events.append({
            "type": "journal",
            "journal": journal,
            "sort_key": journal.created_at,
        })
    for ship in ships:
        total_time = approved_minutes_for_journals(ship.journals.all())
        events.append({
            "type": "ship",
            "ship": ship,
            "time_spent": total_time,
            "time_display": format_minutes(total_time),
            "feedback": getattr(ship, "latest_feedback", ""),
            "sort_key": ship.created_at,
        })
    events.sort(key=lambda e: e["sort_key"], reverse=True)
    return events

def get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")

RATE_LIMIT_MESSAGE = "You're doing that too fast. Please wait a moment and try again."


def _rate_limit_key(request, scope):
    if request.user.is_authenticated:
        actor = f"user:{request.user.id}"
    else:
        actor = f"ip:{get_client_ip(request)}"
    return f"ratelimit:{scope}:{actor}"


def safe_redirect_back(request):
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(referer)
    return redirect("/")


def rate_limit(scope, seconds, methods=("POST",), json=False):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method in methods:
                key = _rate_limit_key(request, scope)

                if not cache.add(key, 1, timeout=seconds):
                    if json:
                        return JsonResponse(
                            {"ok": False, "error": "rate_limited"}, status=429
                        )
                    messages.error(request, RATE_LIMIT_MESSAGE)
                    return safe_redirect_back(request)
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def record_audit(request, action, target="", metadata=None):
    form_data = {
        key: request.POST.getlist(key) if len(request.POST.getlist(key)) > 1 else value
        for key, value in request.POST.items()
        if key != "csrfmiddlewaretoken"
    }
    if request.FILES:
        form_data["_uploaded_files"] = {
            field: [f.name for f in request.FILES.getlist(field)]
            for field in request.FILES
        }

    try:
        AuditLog.objects.create(
            actor=request.user if request.user.is_authenticated else None,
            action=action,
            target=str(target)[:255],
            path=request.path,
            method=request.method,
            ip_address=get_client_ip(request),
            form_data=form_data,
            metadata=metadata or {},
        )
    except Exception as e:
        messages.error(request, f"Failed to log audit: {e}")

def _is_public_ip(ip):
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )

def _validated_public_ip(hostname):
    if not hostname:
        return None
    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return None
    chosen = None
    for *_, sockaddr in addr_info:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return None
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not _is_public_ip(ip):
            return None
        if chosen is None:
            chosen = str(ip)
    return chosen

def _host_resolves_to_public(hostname):
    return _validated_public_ip(hostname) is not None

def _authority(host, port):
    bracketed = f"[{host}]" if ":" in host else host
    return f"{bracketed}:{port}" if port else bracketed

class _PinnedIPAdapter(HTTPAdapter):
    def __init__(self, dest_ip, **kwargs):
        self._dest_ip = dest_ip
        super().__init__(**kwargs)

    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        hostname = parsed.hostname
        request.headers["Host"] = _authority(hostname, parsed.port)
        request.url = urlunparse(
            parsed._replace(netloc=_authority(self._dest_ip, parsed.port))
        )
        pool_kw = self.poolmanager.connection_pool_kw
        if parsed.scheme == "https":
            pool_kw["server_hostname"] = hostname
            pool_kw["assert_hostname"] = hostname
        else:
            pool_kw.pop("server_hostname", None)
            pool_kw.pop("assert_hostname", None)
        return super().send(request, **kwargs)

def _pinned_head(url, dest_ip, timeout=5):
    parsed = urlparse(url)
    session = requests.Session()
    session.mount(f"{parsed.scheme}://{parsed.netloc}", _PinnedIPAdapter(dest_ip))
    try:
        return session.head(url, allow_redirects=False, timeout=timeout)
    finally:
        session.close()

def _safe_head(url, max_redirects=5):
    """HEAD a caller-supplied URL without letting it reach anything internal.

    The guard is per hop, because a redirect is just as attacker-controlled as
    the original URL: reject non-http(s) schemes and off-default ports, resolve
    the host and refuse it unless every address is public, then hand
    _pinned_head the address we vetted so the connection cannot be re-resolved
    to something else in between (DNS rebinding).
    """
    for _ in range(max_redirects + 1):
        if not url or len(url) > MAX_URL_LENGTH:
            return None
        try:
            result = urlparse(url)
        except ValueError:
            return None
        if result.scheme not in ('http', 'https') or not result.netloc:
            return None
        # No legitimate image or model URL is served off-port, and allowing one
        # turns this into a port prober for any public host.
        try:
            port = result.port
        except ValueError:
            return None
        if port not in (None, ALLOWED_URL_PORTS[result.scheme]):
            return None
        dest_ip = _validated_public_ip(result.hostname)
        if dest_ip is None:
            return None
        response = _pinned_head(url, dest_ip)
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get('Location')
            if not location:
                return response
            url = urljoin(url, location)
            continue
        return response
    return None

def is_valid_image_url(url):
    try:
        response = _safe_head(url)
        if response is None:
            return False
        content_type = response.headers.get('Content-Type', '')
        return content_type.startswith('image/')
    except Exception:
        return False

def is_valid_stl_url(url):
    try:
        response = _safe_head(url)
        if response is None:
            return False
        content_type = response.headers.get('Content-Type', '')
        stl_content_types = ('model/stl', 'model/x.stl-ascii', 'model/x.stl-binary', 'application/sla')
        if any(content_type.startswith(ct) for ct in stl_content_types):
            return True
        if content_type.startswith('application/octet-stream') or not content_type:
            return urlparse(url).path.lower().endswith('.stl')
        return False
    except Exception:
        return False

def get_model_info(model_id: str) -> dict:
    PRINTABLES_GRAPHQL_URL = os.environ['PRINTABLES_GRAPHQL_URL']
    QUERY = """
    query GetModelInfo($id: ID!) {
    print(id: $id) {
        id
        name
        slug
        makesCount
        license {
        id
        name
        disallowRemixing
        }
    }
    }
    """

    payload = {
        "operationName": "GetModelInfo",
        "variables": {"id": model_id},
        "query": QUERY,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.printables.com",
        "Referer": "https://www.printables.com/",
        "User-Agent": "Mozilla/5.0 (compatible; Atlantis/1.0)",
    }
    response = requests.post(PRINTABLES_GRAPHQL_URL, json=payload, headers=headers, timeout=5)
    response.raise_for_status()

    data = response.json()

    if "errors" in data:
        raise ValueError(f"GraphQL API errors: {data['errors']}")
    
    return data["data"]["print"]

def send_slack_dm(content, user):
    try:
        response = slack_client.chat_postMessage(
            channel=user,
            text=content
        )
        return True
    except SlackApiError:
        return False

def notify_followers(request, project, message):
    url = request.build_absolute_uri(reverse("project_detail_explore", args=[project.id]))
    content = f"{message} {url}"
    for follower in project.followers.all():
        if follower == project.owner:
            continue
        profile = getattr(follower, "hackclub_profile", None)
        if profile and profile.slack_id:
            send_slack_dm(content, profile.slack_id)
    
def is_valid_editor_model_url(value):
    return detect_editor(value) is not None

def validate_file_size(file, max_mb):
    max_b = max_mb * 1024 * 1024
    if file.size > max_b:
        return False
    return True

def sniff_image_extension(file):
    try:
        file.seek(0)
        image = Image.open(file)
        image_format = image.format
        image.verify()
    except Exception:
        return None
    finally:
        file.seek(0)
    return ALLOWED_IMAGE_FORMATS.get(image_format)

def random_storage_key(prefix, extension):
    return f"{prefix}/{uuid.uuid4().hex}{extension}"

def display_name(user):
    if user is None:
        return "deleted user"
    profile = getattr(user, "hackclub_profile", None)
    if profile and profile.slack_username:
        return profile.slack_username
    return user.username


def add_bars(rows, value_key="value"):
    top = max((r[value_key] for r in rows), default=0) or 1
    for r in rows:
        r["bar"] = round(r[value_key] / top * 100, 1)
    return rows


def reviewer_leaderboard(relation, limit=10):
    User = get_user_model()
    rows = (
        User.objects.annotate(n=Count(relation))
        .filter(n__gt=0)
        .select_related("hackclub_profile")
        .order_by("-n")[:limit]
    )
    return add_bars([{"label": display_name(u), "value": u.n} for u in rows])
