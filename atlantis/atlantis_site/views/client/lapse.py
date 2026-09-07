"""Connecting a Lapse account, and reading the timelapses on it.

Four things live here: the two halves of the authorization, disconnecting, and
the JSON the picker on the book runs on.

The authorization is OAuth2 with PKCE against api.lapse.hackclub.com. The
verifier is kept in the session and never leaves the server; the state is what
proves a code came back to the browser that asked for it. That check is doing
more work than usual here, because Lapse does not validate `redirect_uri` at
the authorize step — it will start a flow for any value — so `state` is the
only thing tying a code to us.

Nothing here trusts what comes back through the browser. `state` is compared
against the session before the code is spent, the `next` a form posts is
checked against this host, and the durations the picker renders are re-read
from the API when the lapse is actually written — see create_journal. A
timelapse's tracked time is the one number on this page that turns into money,
so it never arrives from the client.
"""

import logging
import secrets

from django.conf import settings
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

	The book they came from is remembered in the session so finishing the flow
	puts them back on it, rather than being round-tripped through Lapse where
	anyone could rewrite it.
	"""
	if not lapse.is_configured():
		messages.error(request, "Lapse isn't set up on this site yet.")
		return redirect(_safe_next(request, request.POST.get("next", "")) or "projects")

	verifier, challenge = lapse.generate_pkce()
	state = secrets.token_urlsafe(32)

	request.session[SESSION_KEY] = {
		"state": state,
		"verifier": verifier,
		"next": _safe_next(request, request.POST.get("next", "")),
	}
	return redirect(lapse.authorize_url(state, challenge))


@login_required
def lapse_callback(request):
	"""Finish an authorization Lapse has sent back here.

	Always ends in a redirect — to the book the shipper started from, or to the
	projects list — so a reload can't replay a code that has already been spent.
	"""
	pending = request.session.pop(SESSION_KEY, None) or {}
	destination = pending.get("next") or reverse("projects")

	error = request.GET.get("error")
	if error:
		# The shipper said no, or Lapse refused. Either way it is their flow to
		# retry; say so and leave the connection as it was.
		messages.error(request, "Lapse didn't connect. You can try again from the book.")
		logger.info("Lapse authorization refused for user %s: %s", request.user.pk, error)
		return redirect(destination)

	code = request.GET.get("code")
	if not code:
		messages.error(request, "That Lapse sign-in didn't come back with anything. Try again.")
		return redirect(destination)

	# A code with no state of ours behind it is not one we asked for. This is
	# the whole CSRF check on the flow — see the module docstring.
	state = request.GET.get("state", "")
	if not pending.get("state") or not secrets.compare_digest(state, pending["state"]):
		messages.error(
			request,
			"That Lapse sign-in didn't match the one this browser started. Try again.",
		)
		return redirect(destination)

	try:
		token = lapse.exchange_code(code, pending["verifier"])
	except lapse.LapseCodeRejected as exc:
		# Lapse answers a code it won't take with a 500 rather than
		# `invalid_grant`, so there is nothing to wait out — the code is spent
		# or stale and the only way forward is a fresh authorization.
		logger.warning("Lapse rejected an authorization code for user %s: %s", request.user.pk, exc)
		messages.error(request, "Lapse turned that sign-in down. Please connect again.")
		return redirect(destination)
	except lapse.LapseError as exc:
		logger.warning("Lapse token exchange failed for user %s: %s", request.user.pk, exc)
		messages.error(request, "Couldn't finish connecting to Lapse. Try again in a moment.")
		return redirect(destination)

	# Prove the token works before storing it, and before telling anybody they
	# are connected. This is not the nicety it looks like: /user/myself answers
	# a dead token with `200 {"user": null}` instead of a 401, so it is the only
	# place an already-expired token can be caught. Skipping it is how a shipper
	# ends up "connected" to a credential that fails on every call they make.
	try:
		user = lapse.token_names_a_user(token.get("access_token", ""))
	except lapse.LapseError as exc:
		logger.warning("Lapse user lookup failed for user %s: %s", request.user.pk, exc)
		messages.error(request, "Couldn't reach Lapse to check that connection. Try again in a moment.")
		return redirect(destination)

	if not user:
		logger.warning(
			"Lapse returned a token that names no user for %s (expiry %s)",
			request.user.pk, lapse.token_expiry(token.get("access_token", "")),
		)
		messages.error(
			request,
			"Lapse handed back a sign-in it won't accept. Try connecting again — "
			"if it keeps happening, sign out of lapse.hackclub.com and back in.",
		)
		return redirect(destination)

	account, _ = LapseAccount.objects.get_or_create(user=request.user)
	account.save_token(token)
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


def _entry(timelapse, attached_ids):
	"""One published timelapse, as the picker needs it.

	Everything Lapse says about it that the picker draws, plus the one thing it
	cannot know: whether this footage has already been taped into a lapse here.
	Recordings that can't be picked are still listed — a shipper looking for
	one that is missing is owed the reason it is missing.
	"""
	lapse_id = timelapse.get("id") or ""
	tracked = int(timelapse.get("duration") or 0)

	if lapse_id in attached_ids:
		state = "attached"
	elif timelapse.get("visibility") == "FAILED_PROCESSING":
		state = "failed"
	elif not timelapse.get("playbackUrl"):
		# The docs are explicit that a null playbackUrl is the way to tell a
		# timelapse is still being processed.
		state = "processing"
	elif not lapse.is_attachable(timelapse):
		# Anything else the attach would refuse — an unexpected visibility,
		# say. Named separately so the row doesn't claim to be pickable and
		# then fail at submit.
		state = "blocked"
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

	if not lapse.is_configured():
		return JsonResponse({"ok": False, "error": "Lapse isn't set up on this site yet."}, status=503)

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
		# Lapse has refused the credential, whatever this side believed about
		# its expiry. Drop it so the picker offers a reconnect instead of
		# failing the same way on every load.
		account.forget_token()
		return JsonResponse({"ok": True, "connected": False, "expired": True})
	except lapse.LapseError as exc:
		logger.warning("Lapse fetch failed for user %s: %s", request.user.pk, exc)
		return JsonResponse(
			{"ok": False, "error": "Couldn't reach Lapse right now."}, status=502
		)

	# Anything already taped in, anywhere — the same footage must not be paid
	# for twice, and a lapse in another book counts.
	attached_ids = set(
		Timelapse.objects.filter(
			owner=request.user, source=Timelapse.Source.LAPSE
		).exclude(lapse_id="").values_list("lapse_id", flat=True)
	)

	return JsonResponse({
		"ok": True,
		"connected": True,
		"account": {
			"handle": account.handle,
			"displayName": account.display_name,
		},
		"recordUrl": settings.LAPSE_WEB_BASE_URL,
		"timelapses": [_entry(item, attached_ids) for item in fetched],
	})
