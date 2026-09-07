"""Lapse — the timelapses a shipper records, read back out of their account.

Atlantis does not record anything any more. A shipper records on
lapse.hackclub.com, publishes there, and this module reads what they published
so they can tape one into a lapse. (Lookout, which *did* record here, is still
around for the footage already on it — see lookout.py — but nothing new is
meant to start there.) Everything below follows the API documented at
https://api.lapse.hackclub.com/docs; four things about it shape the code.

**The envelope.** Every API response is `{"ok": true, "data": …}` or
`{"ok": false, "error": …, "message": …}`, and both arrive as 200s. The
envelope is what says whether a call worked, so that is what `_request` reads —
checking the status code alone would take a refusal for a result.

**PKCE, and no refresh.** `/auth/authorize` requires `code_challenge` with
`code_challenge_method=S256`, and `/auth/token` accepts exactly one
`grant_type`: `authorization_code`. There is no documented refresh grant, so an
expired token is not something this module can quietly renew — it means sending
the shipper back through the authorize page, which `LapseAccount.is_expired`
exists to notice before a request fails rather than after.

**Three things the API does that a normal OAuth2 server does not**, each of
which had to be handled explicitly because the obvious code is wrong against it:

1. `/user/myself` never fails. A dead, forged or absent token gets
   `200 {"ok": true, "data": {"user": null}}`, not a 401 — the endpoint
   documents this. So a null user *is* the authentication failure, and it is
   the only signal available at connect time. `token_names_a_user` is what the
   callback uses to avoid storing a token that does not work.
2. `/auth/token` returns a 500, not `invalid_grant`, for any code it doesn't
   like — expired, already spent, or issued to another client. It validates
   the request *shape* (a malformed body gets a 400), so a 500 here means the
   code was refused, and retrying with the same one will never help.
3. `expires_in` is not reliable. The access token is a JWT and Lapse enforces
   the `exp` inside it; the two have been seen to disagree, which strands a
   shipper on a token this side believes is live and the API rejects. So
   `token_expiry` reads the JWT and that is what LapseAccount stores.

**`duration` is recorded seconds, not video seconds.** A timelapse's `duration`
is the time that went into it, and the compiled video runs sixty times faster —
the API reports 720 for a video that is twelve seconds long. That is the same
ratio the review desks already read footage in (see
TRACKED_SECONDS_PER_VIDEO_SECOND in models.py), so `duration` lands on
`tracked_seconds` directly and the video timeline is derived from it. Reading it
as a video length instead would multiply every shipper's hours by sixty.

**`redirect_uri` is not checked at the authorize step.** Lapse accepts any
value there and only pins it between the authorize and token calls, so the
`state` check in the callback is doing the whole job of tying a code to the
browser that asked for it. It is not optional decoration — see
views/client/lapse.py.
"""

import base64
import binascii
import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10

# What the app is registered for, and exactly what Lapse lists back for this
# client id. `timelapse:read` is what the picker runs on, `user:read` names the
# account it is reading, and `snapshot:read` covers the frames a timelapse is
# stitched from.
SCOPES = ("timelapse:read", "snapshot:read", "user:read")

# The API caps a page at 100. Asking for the maximum keeps the round trips down
# for a shipper with a lot of footage; `fetch_published_timelapses` pages past
# it either way.
PAGE_SIZE = 100

# A shipper with more published timelapses than this has something wrong with
# them, and paging forever would hold a request open while it happened.
MAX_PAGES = 20

# Visibilities a timelapse can be attached under. FAILED_PROCESSING is the
# third value the API returns and it means there is no footage behind the row,
# which is the one thing hours may never be logged against.
ATTACHABLE_VISIBILITIES = ("PUBLIC", "UNLISTED")


class LapseError(Exception):
	"""Raised when a Lapse API call fails."""


class LapseAuthError(LapseError):
	"""The token was rejected. The shipper has to reconnect; nothing else helps."""


class LapseCodeRejected(LapseError):
	"""An authorization code was refused. Retrying the same one cannot work."""


def _api_base():
	return settings.LAPSE_API_BASE_URL.rstrip("/")


def _web_base():
	return settings.LAPSE_WEB_BASE_URL.rstrip("/")


def is_configured():
	"""Whether this deployment can talk to Lapse at all.

	Without a client id there is no authorization to start, and the book says so
	rather than sending somebody to a page that will refuse them.
	"""
	return bool(settings.LAPSE_CLIENT_ID)


def watch_url(lapse_id):
	"""The page on Lapse where a timelapse can be watched.

	This is the link that goes out to anyone reading a ship downstream — a
	reviewer here, HQ in Airtable. The `playbackUrl` on the API is a bare mp4;
	this is the page a person can actually read, with the timelapse's name, its
	owner and its comments on it.
	"""
	return f"{_web_base()}/timelapse/{lapse_id}" if lapse_id else _web_base()


def generate_pkce():
	"""A fresh (verifier, challenge) pair for one authorization.

	RFC 7636: the verifier is high-entropy and secret until the token call, the
	challenge is its unpadded base64url SHA-256, and only the challenge travels
	through the browser.
	"""
	verifier = secrets.token_urlsafe(64)
	digest = hashlib.sha256(verifier.encode("ascii")).digest()
	challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
	return verifier, challenge


def authorize_url(state, code_challenge):
	"""Where to send the browser to start an authorization.

	Every parameter here is required by the endpoint, `state` and
	`code_challenge` included — this is a user-agent endpoint and is meant to
	be opened in a browser, never fetched.
	"""
	query = urlencode({
		"response_type": "code",
		"client_id": settings.LAPSE_CLIENT_ID,
		"redirect_uri": settings.LAPSE_REDIRECT_URI,
		"scope": " ".join(SCOPES),
		"state": state,
		"code_challenge": code_challenge,
		"code_challenge_method": "S256",
	})
	return f"{_api_base()}/auth/authorize?{query}"


def exchange_code(code, code_verifier):
	"""Trade an authorization code for a token.

	The body is JSON, not the form encoding OAuth2 usually uses — that is what
	the endpoint documents. The verifier is what proves this is the same client
	that started the flow, so it is sent here and nowhere else.

	Returns the token response: access_token, expires_in, token_type, scope,
	and possibly refresh_token. This is the one endpoint that answers outside
	the `ok`/`data` envelope, so it is read as a plain object.
	"""
	url = f"{_api_base()}/auth/token"
	payload = {
		"grant_type": "authorization_code",
		"code": code,
		"redirect_uri": settings.LAPSE_REDIRECT_URI,
		"client_id": settings.LAPSE_CLIENT_ID,
		"code_verifier": code_verifier,
	}
	# Undocumented — the endpoint's schema doesn't mention it and PKCE is what
	# actually proves the caller. Sent anyway because it is what Fallout sends
	# against this same API in production, and matching the configuration that
	# is known to work costs nothing.
	if settings.LAPSE_CLIENT_SECRET:
		payload["client_secret"] = settings.LAPSE_CLIENT_SECRET
	try:
		response = requests.post(url, json=payload, timeout=_TIMEOUT)
	except requests.RequestException as exc:
		logger.error("Lapse POST auth/token failed: %s", exc)
		raise LapseError(f"Lapse request failed (auth/token): {exc}") from exc

	if not response.ok:
		# The code, the verifier and the redirect URI are all in the body and
		# none of them belong in a log, so only the status and Lapse's own
		# message are kept.
		body = response.text[:500]
		logger.error("Lapse POST auth/token -> %s: %s", response.status_code, body)
		# A 500 here is Lapse refusing the code rather than failing to process
		# it: the endpoint validates the request shape separately and answers a
		# malformed body with a 400. Retrying the same code cannot work, so this
		# is raised as its own thing and the view says so instead of offering a
		# retry that is guaranteed to fail the same way.
		if response.status_code >= 500:
			raise LapseCodeRejected(
				"Lapse refused that sign-in code. Start the connection again."
			)
		raise LapseError(f"Lapse auth/token returned {response.status_code}")

	try:
		token = response.json()
	except ValueError as exc:
		raise LapseError("Lapse auth/token returned a non-JSON body") from exc

	if not isinstance(token, dict) or not token.get("access_token"):
		raise LapseError("Lapse returned a token response with no access token")
	return token


def _request(path, access_token, *, params=None, context=""):
	"""One authenticated GET, with the envelope unwrapped.

	Returns the `data` object. A refusal in the envelope is an error here: the
	callers all want a result or a reason, and none of them can do anything
	sensible with `{"ok": false}` treated as success.
	"""
	url = f"{_api_base()}{path}"
	try:
		response = requests.get(
			url,
			headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
			params=params,
			timeout=_TIMEOUT,
		)
	except requests.RequestException as exc:
		logger.error("Lapse GET %s failed: %s", context or path, exc)
		raise LapseError(f"Lapse request failed ({context or path}): {exc}") from exc

	# A rejected token is worth its own exception: it is the one failure the
	# shipper can fix themselves, by reconnecting.
	if response.status_code in (401, 403):
		raise LapseAuthError("Lapse rejected the connection to this account.")

	if not response.ok:
		body = response.text[:500]
		logger.error("Lapse GET %s -> %s: %s", context or path, response.status_code, body)
		raise LapseError(
			f"Lapse {context or path} returned {response.status_code}: {body}"
		)

	try:
		payload = response.json()
	except ValueError as exc:
		raise LapseError(f"Lapse {context or path} returned a non-JSON body") from exc

	if not payload.get("ok"):
		error = payload.get("error", "ERROR")
		message = payload.get("message", "")
		# NO_PERMISSION and EXPIRED both come back inside a 200 envelope rather
		# than as a 401, and both mean the connection is what's wrong.
		if error in ("NO_PERMISSION", "EXPIRED"):
			raise LapseAuthError("Lapse rejected the connection to this account.")
		logger.error("Lapse GET %s -> %s: %s", context or path, error, message)
		raise LapseError(f"Lapse {context or path} returned {error}: {message}")

	return payload.get("data") or {}


def token_expiry(access_token):
	"""When Lapse will stop accepting this token, read out of the token itself.

	The access token is a JWT and the `exp` claim inside it is what the API
	enforces — the `expires_in` beside it in the token response is a separate
	number that has been seen to disagree, and believing that one leaves a
	shipper holding a token this side thinks is live while every call 401s.

	The signature is deliberately not checked: it is signed with a key only
	Lapse has, and there is nothing to verify anyway. This reads a claim to
	find out when to stop using the token, and a forged `exp` would only make
	us reconnect early. Returns None if there is no readable `exp`, which
	leaves the caller to fall back on `expires_in`.
	"""
	parts = (access_token or "").split(".")
	if len(parts) != 3:
		return None
	try:
		# base64url, and JWT segments drop the padding.
		segment = parts[1]
		segment += "=" * (-len(segment) % 4)
		claims = json.loads(base64.urlsafe_b64decode(segment))
		exp = claims.get("exp")
		if exp is None:
			return None
		return datetime.fromtimestamp(int(exp), tz=timezone.utc)
	except (ValueError, TypeError, OverflowError, OSError, binascii.Error):
		return None


def fetch_myself(access_token):
	"""The Lapse account a token belongs to, or None if it names nobody.

	None is not just "no profile": this endpoint answers a dead, forged or
	missing token with `200 {"user": null}` rather than a 401, so a null user
	is how an unusable token presents. See `token_names_a_user`.
	"""
	data = _request("/user/myself", access_token, context="user/myself")
	return data.get("user")


def token_names_a_user(access_token):
	"""Whether Lapse will actually accept this token, asked before we rely on it.

	The one check available at connect time. `/user/myself` is the endpoint
	that never refuses — every other one 401s — so this is where a token that
	came back from `/auth/token` already dead gets caught, instead of being
	stored, reported as a successful connection, and then failing on every
	call the shipper makes afterwards.

	Returns the user on success and None when the token names nobody. Network
	and envelope failures still raise, because "Lapse is down" and "this token
	is no good" are different answers and only one of them is the shipper's
	to fix.
	"""
	return fetch_myself(access_token)


def fetch_published_timelapses(access_token):
	"""Every published timelapse on the authenticated account, newest page first.

	Cursor-paginated: `nextCursor` is the id to carry into the following call
	and null once there is nothing left. Drafts never appear here — this
	endpoint is published work only, which is what a shipper can attach.
	"""
	timelapses = []
	cursor = None
	for _ in range(MAX_PAGES):
		params = {"limit": PAGE_SIZE}
		if cursor:
			params["cursor"] = cursor
		data = _request(
			"/timelapse/myPublishedTimelapses",
			access_token,
			params=params,
			context="timelapse/myPublishedTimelapses",
		)
		timelapses.extend(data.get("timelapses") or [])
		cursor = data.get("nextCursor")
		if not cursor:
			break
	else:
		logger.warning(
			"Lapse paging stopped at %s pages with a cursor still open", MAX_PAGES
		)
	return timelapses


def fetch_timelapse(access_token, lapse_id):
	"""One timelapse by id, or None if the account can't see it.

	The owner's view of a timelapse carries more than a stranger's, so this is
	called with the shipper's own token.
	"""
	data = _request(
		"/timelapse/query",
		access_token,
		params={"id": lapse_id},
		context="timelapse/query",
	)
	return data.get("timelapse")


def is_attachable(timelapse):
	"""Whether a timelapse payload has real, viewable footage behind it.

	The gate for logging hours. A null `playbackUrl` is how the API says a
	timelapse is still being processed, and FAILED_PROCESSING is how it says
	the processing gave up; neither has a video anybody could review.
	"""
	if not timelapse:
		return False
	return bool(timelapse.get("playbackUrl")) and (
		timelapse.get("visibility") in ATTACHABLE_VISIBILITIES
	)
