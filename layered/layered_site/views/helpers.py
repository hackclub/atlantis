from django.contrib.auth.decorators import user_passes_test
from django.conf import settings
from django.contrib import messages
from django.db.models import Count
from django.contrib.auth import get_user_model
from django.urls import reverse
from ..models import AuditLog

from slack_sdk.errors import SlackApiError
from slack_sdk import WebClient

from urllib.parse import urlparse, urljoin

from PIL import Image

import os
import uuid
import requests
import re
import socket
import ipaddress

ALLOWED_IMAGE_FORMATS = {
    "PNG": ".png",
    "JPEG": ".jpg",  
    "GIF": ".gif",
    "WEBP": ".webp",
}

PRINTABLES_URL_RE = re.compile(r"https:\/\/(?:www\.)?printables\.com(?:\/.*)?$", re.IGNORECASE)
CLOUDFLARE_BUCKET_RE = re.compile(r"^https?:\/\/(?:[a-zA-Z0-9-]+\.)*pub-d9ac82fd80854a42ae2dde2757ff0a55\.r2\.dev(?:\/.*)?$", re.IGNORECASE)

slack_client = WebClient(token=settings.SLACK_TOKEN, timeout=5)

def check_perms(perms):
    def check_perms_internal(user):
        for perm in perms:
            if user.has_perm(perm):
                return True
        return False
    return user_passes_test(check_perms_internal)

def is_valid_printables_url(value):
    return bool(PRINTABLES_URL_RE.match(value))

def layers_for_minutes(minutes):
    tenths_of_hour = minutes // 6
    return round(tenths_of_hour * 0.5)

def build_journal_timeline(journals, ships):
    events = []
    for journal in journals:
        events.append({
            "type": "journal",
            "journal": journal,
            "sort_key": journal.created_at,
        })
    for ship in ships:
        total_time = sum(j.time_spent for j in ship.journals.all())
        events.append({
            "type": "ship",
            "ship": ship,
            "time_spent": total_time,
            "time_display": f"{total_time // 60}h {total_time % 60}m",
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

def _host_resolves_to_public(hostname):
    if not hostname:
        return False
    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for *_, sockaddr in addr_info:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not _is_public_ip(ip):
            return False
    return True

def _safe_head(url, max_redirects=5):
    for _ in range(max_redirects + 1):
        result = urlparse(url)
        if result.scheme not in ('http', 'https') or not result.netloc:
            return None
        if not _host_resolves_to_public(result.hostname):
            return None
        response = requests.head(url, allow_redirects=False, timeout=5)
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
        "User-Agent": "Mozilla/5.0 (compatible; Layered/1.0)",
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
    return bool(CLOUDFLARE_BUCKET_RE.match(value))

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
    """Rank users by how many `relation` rows they own (e.g. "t1_reviews").

    Returns rows shaped for root/_metric_chart.html: {label, value, bar}.
    """
    User = get_user_model()
    rows = (
        User.objects.annotate(n=Count(relation))
        .filter(n__gt=0)
        .select_related("hackclub_profile")
        .order_by("-n")[:limit]
    )
    return add_bars([{"label": display_name(u), "value": u.n} for u in rows])