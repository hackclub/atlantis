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
    AuditLog, InternalComment, Journal, Ship, T1, T2, T3, Timelapse, TimelapseRemoval,
    TimelapseReview,
    PAYOUT_MULTIPLIER_DEFAULT, PEARLS_PER_HOUR, detect_editor, is_editor_model_file
)
from ..hca import (
    AddressUnavailable, IdentityUnavailable, VERIFICATION_INELIGIBLE,
    VERIFICATION_PENDING, VERIFICATION_VERIFIED, fetch_addresses, refresh_verification
)

from decimal import Decimal, ROUND_HALF_EVEN
from functools import wraps

from slack_sdk.errors import SlackApiError
from slack_sdk import WebClient

from urllib.parse import urlparse, urljoin, urlunparse

from requests.adapters import HTTPAdapter

from PIL import Image

import os
import uuid
import logging
import threading
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

logger = logging.getLogger(__name__)

def check_perms(perms):
    return user_passes_test(lambda user: any(user.has_perm(p) for p in perms))

def is_valid_printables_url(value):
    """True when value is an https URL whose host really is printables.com.

    An allowlist on the parsed host rather than a regex on the URL: linear on
    input it can't match, and it drops `https://printables.com@evil.com`,
    where the lookalike is only the userinfo.
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

def layers_for_minutes(minutes, multiplier=PAYOUT_MULTIPLIER_DEFAULT):
    # PEARLS_PER_HOUR prorated over six-minute buckets, scaled by the T3
    # reviewer's multiplier. Decimal because every part of the rate is exact in
    # tenths, and half a bucket short of a pearl must not round its way up.
    tenths_of_hour = minutes // 6
    layers = (Decimal(tenths_of_hour) * PEARLS_PER_HOUR / 10) * Decimal(multiplier)
    return int(layers.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))

def tracked_seconds_for_journals(journals):
    return Timelapse.objects.filter(journal__in=journals).aggregate(
        total=Sum("tracked_seconds")
    )["total"] or 0

def tracked_minutes_for_journals(journals):
    return tracked_seconds_for_journals(journals) // 60

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

def approved_minutes_by_project(project_ids):
    """{project id: approved minutes over all its journals}, in two queries.

    approved_minutes_for_journals(project.journals.all()) for a whole page of
    projects at once.
    """
    project_ids = set(project_ids)
    tracked = dict(
        Timelapse.objects.filter(journal__project_id__in=project_ids)
        .values_list("journal__project_id")
        .order_by()
        .annotate(total=Sum("tracked_seconds"))
    )
    removed = dict(
        TimelapseRemoval.objects.filter(review__journal__project_id__in=project_ids)
        .values_list("review__journal__project_id")
        .order_by()
        .annotate(total=Sum(F("end_seconds") - F("start_seconds"), output_field=IntegerField()))
    )
    return {
        project_id: max((tracked.get(project_id) or 0) - (removed.get(project_id) or 0), 0) // 60
        for project_id in project_ids
    }

def payable_journals_for_ship(ship):
    """Every journal this ship's payout has to cover.

    A journal stays attached to the ship it was shipped under, so a reship
    carries only the entries written since the last one went out. That is the
    right scope only when the last ship paid: a rejected ship pays nothing, and
    scoping its successor to its own journals would drop every hour the
    rejection covered — the first payout on a project would be for the last
    few minutes of it.

    So a payout covers the work logged since the last ship that actually paid:
    this ship's journals, plus those of every ship after the most recent
    finalized one. On a project that has never been finalized that is the whole
    project; on an update it is only what the update added.
    """
    last_paid_id = (
        ship.project.ships
        .filter(status=Ship.ShipStatus.FINALIZED, id__lt=ship.id)
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
    )
    # Bounded above by this ship, which also leaves out the journals written
    # since it went out: they belong to the next ship, not this payout.
    journals = Journal.objects.filter(ship__project=ship.project, ship_id__lte=ship.id)
    if last_paid_id is not None:
        journals = journals.filter(ship_id__gt=last_paid_id)
    return journals

def payable_minutes_for_ship(ship):
    return approved_minutes_for_journals(payable_journals_for_ship(ship))


def ship_payout(ship, minutes, multiplier=PAYOUT_MULTIPLIER_DEFAULT, brackets=None):
    """What paying `minutes` on this ship is worth, split across its weeks.

    Returns (pearls, lines, drawn) — see challenge.payout_breakdown. `brackets`
    defaults to what the owner has already drawn, which is what both the
    reviewer's preview and the finalization itself want; the finalization
    passes its own locked copy.
    """
    from ..challenge import brackets_for, payout_breakdown

    journals = payable_journals_for_ship(ship)
    if brackets is None:
        brackets = brackets_for(ship.project.owner)
    return payout_breakdown(journals, minutes, multiplier, brackets)


def payout_buckets(ship, brackets=None):
    """The weights and rates the reviewer's live preview recomputes from.

    The pearls a payout is worth depend on which weeks its hours were recorded
    in, so the slider can't just multiply by a rate any more. This is that
    shape as plain JSON: one entry per week the ship touches, carrying the
    approved seconds that decide its share of the minutes and the rates that
    share will be priced at.
    """
    from ..challenge import (
        CHALLENGE_BASE_PEARLS_PER_HOUR, CHALLENGE_BONUS_PEARLS_PER_HOUR, PREP,
        _approved_seconds_by_week, brackets_for,
    )
    from .. import weeks as weeks_mod

    owner = ship.project.owner
    if brackets is None:
        brackets = brackets_for(owner)

    approved = _approved_seconds_by_week(payable_journals_for_ship(ship))
    out = []
    for key in sorted(approved, key=lambda k: -1 if k is PREP else k):
        prep = key is PREP
        out.append({
            "week": 0 if prep else key,
            "weight": approved[key],
            # How many of this bucket's minutes still price at the base rate.
            # Prep has no bracket: all of it is one flat rate.
            "room": 0 if prep else max(weeks_mod.WEEKLY_MINUTES - brackets.get(key, 0), 0),
            "base": float(PEARLS_PER_HOUR if prep else CHALLENGE_BASE_PEARLS_PER_HOUR),
            "bonus": float(PEARLS_PER_HOUR if prep else CHALLENGE_BONUS_PEARLS_PER_HOUR),
        })
    return out

def timelapse_cleared_ships(ships):
    # "No review" as its own NOT EXISTS rather than timelapse_review__isnull,
    # whose LEFT JOIN ... IS NULL Postgres estimates at one row: nested inside
    # this EXISTS that became a nested loop over every journal for every ship,
    # which every admin page paid for through the T1 nav badge.
    unreviewed = Journal.objects.filter(ship=OuterRef("pk")).exclude(
        Exists(TimelapseReview.objects.filter(journal=OuterRef("pk")))
    )
    return ships.exclude(Exists(unreviewed))

def format_minutes(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60}h {minutes % 60}m"

def can_bypass_ship_requirements(user):
    return bool(settings.DEBUG and user.has_perm("atlantis_site.organizer"))


# The two doors HCA keeps for this: one to start (or redo) a verification, one
# to watch a submitted one.
HCA_VERIFY_URL = "https://auth.hackclub.com/verifications/new"
HCA_VERIFY_STATUS_URL = "https://auth.hackclub.com/verifications/status"

# How long a "not eligible" answer stands before we ask HCA again. Verification
# is approved asynchronously, so the answer stored at login goes stale while the
# user is still on the site — but only the blocked pay for the re-ask, and only
# this often.
VERIFICATION_REFRESH_AFTER = 60


def _ineligible_message(status, eligible):
    """The sentence a user who can't create or ship is owed, and where to go."""
    if status == VERIFICATION_VERIFIED and eligible is False:
        return (
            "Your Hack Club identity is verified, but it isn't eligible for YSWS "
            "programs, so you can't create projects or ship. If that looks wrong, "
            f"check {HCA_VERIFY_STATUS_URL}"
        )
    if status == VERIFICATION_PENDING:
        return (
            "Hack Club is still reviewing your identity. You can create projects "
            f"and ship once it's approved. Track it at {HCA_VERIFY_STATUS_URL}"
        )
    if status == VERIFICATION_INELIGIBLE:
        return (
            "Hack Club couldn't verify your identity, so you can't create projects "
            f"or ship. Take another run at it at {HCA_VERIFY_URL}"
        )
    # Nothing submitted, or HCA never told us anything at all.
    return (
        "Verify your identity with Hack Club before you create projects or ship: "
        f"{HCA_VERIFY_URL}"
    )


def ysws_block_reason(user):
    """Why this user may not create projects or ship, or "" if they may.

    The answer HCA gave at login is the starting point, but it is only as fresh
    as that login — someone approved an hour ago should not have to log out and
    back in to get moving. So when the stored answer is no, HCA is asked again
    (at most once a minute per user) before the user is turned away. If HCA
    can't be reached, the stored answer is the only one there is and it stands.
    """
    profile = getattr(user, "hackclub_profile", None)
    if profile is None:
        return _ineligible_message("", None)

    if profile.is_ysws_eligible:
        return ""

    if cache.add(f"idv-refresh:{user.id}", 1, timeout=VERIFICATION_REFRESH_AFTER):
        try:
            refresh_verification(profile)
        except IdentityUnavailable:
            pass  # HCA is unreachable; the stored answer is the only one there is.
        if profile.is_ysws_eligible:
            return ""

    return _ineligible_message(profile.verification_status, profile.ysws_eligible)

NO_ADDRESS_MESSAGE = "Add an address on HCA to ship"
ADDRESS_UNAVAILABLE_MESSAGE = (
    "Couldn't reach Hack Club to check your address. Try again in a moment."
)

# How long a "yes, there's an address" answer is trusted before HCA is asked
# again. Only the yes is cached: someone who has just added one should be able
# to go straight back and try again.
ADDRESS_CHECK_TTL = 10 * 60


def _is_usable_address(address):
    """An address we could actually put on a parcel."""
    street = address.get("line_1") or address.get("street_address")
    return bool(street and address.get("country"))


def address_block_reason(user):
    """Why this user may not create projects or ship for want of an address, or
    "" if HCA has a usable one on file.

    A project only exists to be shipped, and a ship only pays out in things
    that get mailed, so there is no point starting one without somewhere to
    send them. Addresses live on HCA, never here, so it is asked live.
    """
    cache_key = f"has-address:{user.id}"
    if cache.get(cache_key):
        return ""

    profile = getattr(user, "hackclub_profile", None)
    if profile is None:
        return NO_ADDRESS_MESSAGE

    try:
        addresses = fetch_addresses(profile)
    except AddressUnavailable:
        return ADDRESS_UNAVAILABLE_MESSAGE

    if not any(_is_usable_address(address) for address in addresses):
        return NO_ADDRESS_MESSAGE

    cache.set(cache_key, 1, timeout=ADDRESS_CHECK_TTL)
    return ""


def ship_block_reason(user):
    """Why this user may not create projects or ship, or "" if they may.

    Eligibility comes first: somebody HCA has turned down should hear that, not
    be sent off to add an address they can't use.
    """
    return ysws_block_reason(user) or address_block_reason(user)

def internal_comments_for_project(project):
    """Reviewer-only comments on every ship of a project, newest first."""
    return (
        InternalComment.objects.filter(ship__project=project)
        .select_related("author", "author__hackclub_profile")
    )

def build_review_history(ship):
    """Everything reviewers did to any ship of the project, oldest first.

    Earlier ships are part of the story: a reviewer deciding on this ship
    needs to see why the previous ones were returned or rejected.

    /root pages only: it carries internal notes the owner must never see.
    """
    project = ship.project
    events = []
    for t1 in T1.objects.filter(ship__project=project).select_related(
        "reviewer", "reviewer__hackclub_profile"
    ):
        events.append({
            "type": "t1",
            "label": "T1 Review",
            "review": t1,
            "actor": display_name(t1.reviewer),
            "other_ship": t1.ship_id != ship.id,
            "ship_id": t1.ship_id,
            "at": t1.reviewed_at,
        })
    for t2 in T2.objects.filter(ship__project=project).select_related(
        "reviewer", "reviewer__hackclub_profile"
    ):
        events.append({
            "type": "t2",
            "label": "T2 Review",
            "review": t2,
            "actor": display_name(t2.reviewer),
            "other_ship": t2.ship_id != ship.id,
            "ship_id": t2.ship_id,
            "at": t2.reviewed_at,
        })
    # T3 sends ships back to T1 and T2, and its notes are the only word on
    # why: a reviewer picking a returned ship up again needs to read them.
    for t3 in T3.objects.filter(ship__project=project).select_related(
        "reviewer", "reviewer__hackclub_profile"
    ):
        events.append({
            "type": "t3",
            "label": "T3 Review",
            "review": t3,
            "actor": display_name(t3.reviewer),
            "other_ship": t3.ship_id != ship.id,
            "ship_id": t3.ship_id,
            "at": t3.reviewed_at,
        })
    for comment in internal_comments_for_project(project):
        events.append({
            "type": "comment",
            "label": "Internal comment",
            "comment": comment,
            "actor": display_name(comment.author),
            "other_ship": comment.ship_id != ship.id,
            "ship_id": comment.ship_id,
            "at": comment.created_at,
        })
    events.sort(key=lambda e: e["at"])
    return events

def build_journal_timeline(journals, ships):
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

    Guarded per hop, because a redirect is as attacker-controlled as the
    original URL: reject non-http(s) schemes and off-default ports, refuse the
    host unless every address it resolves to is public, then pin _pinned_head
    to the address we vetted so it cannot be re-resolved (DNS rebinding).
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

def send_slack_message(content, channel):
    if settings.DEBUG:
        content = f"[DEBUG/LOCAL DEV] {content}"
    try:
        slack_client.chat_postMessage(
            channel=channel,
            text=content
        )
        return True
    except SlackApiError:
        return False

def send_slack_dm(content, user):
    # A DM is the same API call as a channel post, addressed to a Slack ID.
    return send_slack_message(content, user)

# Most users conversations.invite takes in one call.
SLACK_INVITE_BATCH = 1000

def invite_to_channel(channel, slack_ids):
    """Invite these Slack users to one channel; returns how many joined.

    force=True so one deactivated account or bad id doesn't sink the rest of
    its batch. Anybody already in the channel is reported back as an error,
    which is expected on a re-run and not worth a line in the log.
    """
    ids = list(dict.fromkeys(slack_id for slack_id in slack_ids if slack_id))
    if not channel or not ids:
        return 0

    invited = 0
    for start in range(0, len(ids), SLACK_INVITE_BATCH):
        batch = ids[start:start + SLACK_INVITE_BATCH]
        try:
            response = slack_client.conversations_invite(channel=channel, users=batch, force=True)
        except SlackApiError as exc:
            # Nobody in the batch joined. A single-user batch names its reason
            # in `error`; a larger one lists them per user in `errors`.
            _log_invite_errors(channel, exc.response.get("errors") or [
                {"user": ",".join(batch), "error": exc.response.get("error")}
            ])
            continue
        errors = response.get("errors") or []
        _log_invite_errors(channel, errors)
        invited += len(batch) - len(errors)
    return invited

def _log_invite_errors(channel, errors):
    for error in errors:
        if error.get("error") != "already_in_channel":
            logger.warning("Invite to %s skipped %s: %s", channel, error.get("user"), error.get("error"))

def invite_to_autojoin_channels(slack_ids):
    """Invite these Slack users to every channel new signups auto-join.

    Every channel gets the full list independently, so one channel rejecting a
    batch (wrong permissions, a bad id) doesn't stop the others.
    """
    slack_ids = list(slack_ids)
    return sum(
        invite_to_channel(channel, slack_ids)
        for channel in settings.SLACK_AUTOJOIN_CHANNEL_IDS
    )

def invite_to_autojoin_channels_in_background(slack_ids, label):
    """invite_to_autojoin_channels off the request thread; the outcome goes to the log."""
    slack_ids = list(slack_ids)

    def run():
        try:
            invited = invite_to_autojoin_channels(slack_ids)
        except Exception:
            logger.exception("Autojoin invite %s crashed", label)
        else:
            logger.info("Autojoin invite %s added %s membership(s)", label, invited)

    _start_thread(run)

def _start_thread(target):
    threading.Thread(target=target, daemon=True).start()

def slack_mention(user):
    profile = getattr(user, "hackclub_profile", None)
    slack_id = profile.slack_id if profile else ""
    return f"<@{slack_id}>" if slack_id else display_name(user)

def notify_followers(request, project, message):
    url = request.build_absolute_uri(reverse("project_detail", args=[project.id]))
    content = f"{message} {url}"
    for follower in project.followers.all():
        if follower == project.owner:
            continue
        profile = getattr(follower, "hackclub_profile", None)
        if profile and profile.slack_id:
            send_slack_dm(content, profile.slack_id)
    
def is_valid_editor_model_url(value):
    # An archive names no editor, so detect_editor can't vouch for it — but a
    # .zip upload is still a source file we accept.
    return detect_editor(value) is not None or is_editor_model_file(value)

# What Postgres will take in an `integer` column. Anything past it comes back
# as NumericValueOutOfRange from the driver — a 500 on a form the user could
# have been told about — so views that build a number out of POST data check
# the range themselves first.
INT_FIELD_MAX = 2**31 - 1
INT_FIELD_MIN = -(2**31)


def field_max_length(model, field_name):
    """How long a posted value may be before its column refuses it.

    Read off the model rather than written down in each view. Postgres answers
    an over-long CharField with a DataError, which surfaces as a 500 rather
    than the sentence the user should have got, and a hardcoded number goes
    stale the first time a migration widens the column.
    """
    return model._meta.get_field(field_name).max_length


def too_long(value, model, field_name):
    """True when `value` wouldn't fit `model.field_name`."""
    limit = field_max_length(model, field_name)
    return limit is not None and len(value or "") > limit


def fit(value, model, field_name):
    """`value` trimmed to what `model.field_name` will hold.

    For values we don't control and can't bounce a form over — what an upstream
    identity provider calls someone, say. Losing the tail of a display name is
    a worse name; failing the write is a 500.
    """
    limit = field_max_length(model, field_name)
    value = value or ""
    return value[:limit] if limit is not None else value


def validate_file_size(file, max_mb):
    return file.size <= max_mb * 1024 * 1024

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
