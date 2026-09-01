"""Lapse — the timelapses a shipper records, read back out of their account.

Atlantis does not record anything. A shipper records on lapse.hackclub.com,
publishes there, and this module reads what they published so they can tape one
into a lapse. Everything below follows the API documented at
https://api.lapse.hackclub.com/docs; three things about it shape the code.

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

**`duration` is recorded seconds, not video seconds.** A timelapse's `duration`
is the time that went into it, and the compiled video runs sixty times faster —
the API reports 720 for a video that is twelve seconds long. That is the same
ratio the review desks already read footage in (see
TRACKED_SECONDS_PER_VIDEO_SECOND in models.py), so `duration` lands on
`tracked_seconds` directly and the video timeline is derived from it. Reading it
as a video length instead would multiply every shipper's hours by sixty.
"""

import base64
import hashlib
import logging
import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10

# What the app is registered for. `timelapse:read` is what the picker runs on,
# `user:read` names the account it is reading, and `snapshot:read` covers the
# frames a timelapse is stitched from.
SCOPES = ("timelapse:read", "snapshot:read", "user:read")

# The API caps a page at 100. Asking for the maximum keeps the round trips down
# for a shipper with a lot of footage; `fetch_published_timelapses` pages past
# it either way.
PAGE_SIZE = 100

# A shipper with more published timelapses than this has something wrong with
# them, and paging forever would hold a request open while it happened.
MAX_PAGES = 20


class LapseError(Exception):
	"""Raised when a Lapse API call fails."""


class LapseAuthError(LapseError):
	"""The token was rejected. The shipper has to reconnect; nothing else helps."""


def _api_base():
	return settings.LAPSE_API_BASE_URL.rstrip("/")


def _web_base():
	return settings.LAPSE_WEB_BASE_URL.rstrip("/")


def watch_url(lapse_id):
	"""The page on Lapse where a timelapse can be watched.

	This is the link that goes out to anyone reading a ship downstream — a
	reviewer here, HQ in Airtable. The `playbackUrl` on the API is a bare mp4
	behind a signed redirect that expires; this is the permalink.
	"""
	return f"{_web_base()}/timelapse/{lapse_id}"


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


def _post_json(url, payload, *, context):
	try:
		response = requests.post(url, json=payload, timeout=_TIMEOUT)
	except requests.RequestException as exc:
		logger.error("Lapse POST %s failed: %s", context, exc)
		raise LapseError(f"Lapse request failed ({context}): {exc}") from exc

	if not response.ok:
		body = response.text[:500]
		logger.error("Lapse POST %s -> %s: %s", context, response.status_code, body)
		raise LapseError(f"Lapse {context} returned {response.status_code}: {body}")

	try:
		return response.json()
	except ValueError as exc:
		raise LapseError(f"Lapse {context} returned a non-JSON body") from exc


def exchange_code(code, code_verifier):
	"""Trade an authorization code for a token.

	The body is JSON, not the form encoding OAuth2 usually uses — that is what
	the endpoint documents. The verifier is what proves this is the same client
	that started the flow, so it is sent here and nowhere else.

	Returns the token response: access_token, expires_in, token_type, scope,
	and possibly refresh_token.
	"""
	token = _post_json(
		f"{_api_base()}/auth/token",
		{
			"grant_type": "authorization_code",
			"code": code,
			"redirect_uri": settings.LAPSE_REDIRECT_URI,
			"client_id": settings.LAPSE_CLIENT_ID,
			"code_verifier": code_verifier,
		},
		context="auth/token",
	)
	if not token.get("access_token"):
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
			headers={"Authorization": f"Bearer {access_token}"},
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
		if error == "NO_PERMISSION":
			raise LapseAuthError("Lapse rejected the connection to this account.")
		logger.error("Lapse GET %s -> %s: %s", context or path, error, message)
		raise LapseError(f"Lapse {context or path} returned {error}: {message}")

	return payload.get("data") or {}


def fetch_myself(access_token):
	"""The Lapse account a token belongs to, or None if it names nobody."""
	data = _request("/user/myself", access_token, context="user/myself")
	return data.get("user")


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
