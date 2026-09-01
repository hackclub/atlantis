"""Connecting a Lapse account, and reading the timelapses on it.

Three things live here: the start of the authorization, the half that finishes
it, and the JSON the picker on the book runs on.

The authorization is OAuth2 with PKCE. The redirect URI is registered with
Lapse as the projects list and cannot vary per request, so there is no callback
route of its own — `projects` calls `complete_authorization` when it sees a
`code` on the query string, and the book the shipper started from is remembered
in the session rather than round-tripped through Lapse.

Nothing here trusts what comes back through the browser. `state` is compared
against the session before the code is spent, and the durations the picker
renders are re-read from the API when the lapse is actually written — see
create_journal. A timelapse's tracked time is the one number on this page that
turns into money, so it never arrives from the client.
"""

import logging
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from ... import lapse
from ...models import LapseAccount, Project, Timelapse
from ..helpers import rate_limit

logger = logging.getLogger(__name__)

# Where the in-flight authorization is kept between the redirect out and the
# code coming back. The verifier is the secret half of PKCE and never leaves
# the server; the state is what proves the code belongs to this browser.
SESSION_KEY = "lapse_oauth"


def account_for(user):
	"""The user's Lapse connection, or None if they have never connected."""
	return LapseAccount.objects.filter(user=user).first()


def _safe_next(request, raw):
	"""A posted `next`, or "" if it points anywhere but this site."""
	if raw and url_has_allowed_host_and_scheme(
		raw, allowed_hosts={request.get_host()}, require_https=request.is_secure()
	):
		return raw
	return ""


@login_required
@require_POST
@rate_limit("lapse_connect", 2)
def lapse_connect(request):
	"""Send the shipper to Lapse to authorize us.

	The book they came from is remembered here so finishing the flow puts them
	back on it — the redirect URI is fixed at the projects list, so Lapse
	cannot carry it for us.
	"""
	verifier, challenge = lapse.generate_pkce()
	state = secrets.token_urlsafe(32)

	request.session[SESSION_KEY] = {
		"state": state,
		"verifier": verifier,
		"next": _safe_next(request, request.POST.get("next", "")),
	}
	return redirect(lapse.authorize_url(state, challenge))


@login_required
@require_POST
@rate_limit("lapse_disconnect", 2)
def lapse_disconnect(request):
	"""Forget the token. The timelapses already taped in stay where they are.

	Those hours are already claimed and already reviewable; disconnecting is
	about the credential, not about the work.
	"""
	LapseAccount.objects.filter(user=request.user).delete()
	messages.success(request, "Disconnected your Lapse account.")
	return redirect(_safe_next(request, request.POST.get("next", "")) or "projects")


def complete_authorization(request):
	"""Finish an authorization that has come back to the projects list.

	Called by `projects` when it sees a `code`. Returns a redirect when it
	handled one — to the book the shipper started from, or back to a clean
	projects URL so a reload doesn't replay a spent code — and None when there
	was nothing to handle.
	"""
	code = request.GET.get("code")
	error = request.GET.get("error")
	if not code and not error:
		return None

	pending = request.session.pop(SESSION_KEY, None) or {}
	destination = pending.get("next") or reverse("projects")

	if error:
		# The shipper said no, or Lapse refused. Either way it is their flow to
		# retry; say so and leave the connection as it was.
		messages.error(request, f"Lapse didn't connect: {error}")
		return redirect(destination)

	# A code with no state of ours behind it is not one we asked for.
	state = request.GET.get("state", "")
	if not pending.get("state") or state != pending["state"]:
		messages.error(
			request,
			"That Lapse sign-in didn't match the one this browser started. Try again.",
		)
		return redirect(destination)

	try:
		token = lapse.exchange_code(code, pending["verifier"])
	except lapse.LapseError as exc:
		logger.warning("Lapse token exchange failed for user %s: %s", request.user.pk, exc)
		messages.error(request, "Couldn't finish connecting to Lapse. Try again in a moment.")
		return redirect(destination)

	account, _ = LapseAccount.objects.get_or_create(user=request.user)
	account.save_token(token)

	# Name the account on the connection, so the picker can say whose
	# timelapses it is showing. A failure here costs the label and nothing
	# else, so it must not lose the token we just got.
	try:
		user = lapse.fetch_myself(account.access_token)
	except lapse.LapseError as exc:
		logger.warning("Lapse user lookup failed for user %s: %s", request.user.pk, exc)
		user = None

	if user:
		account.lapse_user_id = user.get("id", "")
		account.handle = user.get("handle", "")
		account.display_name = user.get("displayName", "")
		account.profile_picture_url = user.get("profilePictureUrl", "")

	account.save()
	messages.success(
		request,
		f"Connected your Lapse account{f' (@{account.handle})' if account.handle else ''}.",
	)
	return redirect(destination)


def _entry(timelapse, attached_ids):
	"""One published timelapse, as the picker needs it.

	Everything Lapse says about it that the picker draws, plus the one thing it
	cannot know: whether this footage has already been taped into a lapse here.
	Recordings that can't be picked are still listed — a shipper looking for
	one that is missing is owed the reason it is missing.
	"""
	lapse_id = timelapse.get("id") or ""
	playback = timelapse.get("playbackUrl")
	tracked = int(timelapse.get("duration") or 0)

	if lapse_id in attached_ids:
		state = "attached"
	elif timelapse.get("visibility") == "FAILED_PROCESSING":
		state = "failed"
	elif not playback:
		# The docs are explicit that a null playbackUrl is the way to tell a
		# timelapse is still being processed.
		state = "processing"
	else:
		state = "available"

	return {
		"id": lapse_id,
		"name": timelapse.get("name") or "Untitled timelapse",
		"state": state,
		"trackedSeconds": tracked,
		"trackedDisplay": f"{tracked // 3600}h {(tracked % 3600) // 60}m",
		"recordedAt": timelapse.get("createdAt"),
		"thumbnailUrl": timelapse.get("thumbnailUrl") or "",
		"watchUrl": lapse.watch_url(lapse_id) if lapse_id else "",
	}


@login_required
@rate_limit("lapse_timelapses", 2, methods=("GET",), json=True)
def lapse_timelapses(request, project_id):
	"""The picker's list, read live from Lapse.

	Fetched when the picker opens and again on every refresh, which is the
	point of it: a timelapse published thirty seconds ago should be tapeable
	without reloading the book.
	"""
	get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)

	account = account_for(request.user)
	if account is None or not account.access_token:
		return JsonResponse({"ok": True, "connected": False})

	if account.is_expired:
		# There is no refresh grant on the Lapse token endpoint, so this is not
		# something we can fix without the shipper.
		return JsonResponse({"ok": True, "connected": False, "expired": True})

	try:
		fetched = lapse.fetch_published_timelapses(account.access_token)
	except lapse.LapseAuthError:
		return JsonResponse({"ok": True, "connected": False, "expired": True})
	except lapse.LapseError as exc:
		logger.warning("Lapse fetch failed for user %s: %s", request.user.pk, exc)
		return JsonResponse(
			{"ok": False, "error": "Couldn't reach Lapse right now."}, status=502
		)

	# Anything already taped in, anywhere — the same footage must not be paid
	# for twice, and a lapse in another book counts.
	attached_ids = set(
		Timelapse.objects.filter(owner=request.user).values_list("lapse_id", flat=True)
	)

	return JsonResponse({
		"ok": True,
		"connected": True,
		"account": {
			"handle": account.handle,
			"displayName": account.display_name,
		},
		"timelapses": [_entry(item, attached_ids) for item in fetched],
	})
