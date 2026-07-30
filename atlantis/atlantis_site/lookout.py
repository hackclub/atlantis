import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10


class LookoutError(Exception):
	"""Raised when a Lookout API call fails."""


def _base():
	return settings.LOOKOUT_BASE_URL.rstrip("/")


def _internal_headers():
	return {
		"Content-Type": "application/json",
		"X-API-Key": settings.LOOKOUT_TOKEN,
	}


def redact_token(token):
	if not token:
		return "<none>"
	return f"{token[:6]}…{token[-4:]}" if len(token) > 12 else "<redacted>"


def _request(method, url, *, headers=None, json=None, context=""):
	try:
		response = requests.request(
			method, url, headers=headers, json=json, timeout=_TIMEOUT
		)
	except requests.RequestException as exc:
		logger.error("Lookout %s %s failed: %s", method, context or url, exc)
		raise LookoutError(f"Lookout request failed ({context or url}): {exc}") from exc

	if not response.ok:
		body = response.text[:500]
		logger.error(
			"Lookout %s %s -> %s: %s", method, context or url, response.status_code, body
		)
		raise LookoutError(
			f"Lookout {method} {context or url} returned {response.status_code}: {body}"
		)

	if not response.content:
		return {}
	try:
		return response.json()
	except ValueError as exc:
		raise LookoutError(
			f"Lookout {method} {context or url} returned non-JSON body"
		) from exc



def create_session(metadata=None):
	payload = {"metadata": metadata or {}}
	return _request(
		"POST",
		f"{_base()}/api/internal/sessions",
		headers=_internal_headers(),
		json=payload,
		context="create_session",
	)


def get_internal_session(session_id):
	return _request(
		"GET",
		f"{_base()}/api/internal/sessions/{session_id}",
		headers=_internal_headers(),
		context=f"get_internal_session({session_id})",
	)


def stop_session(session_id):
	return _request(
		"POST",
		f"{_base()}/api/internal/sessions/{session_id}/stop",
		headers=_internal_headers(),
		context=f"stop_session({session_id})",
	)


def recompile_session(session_id):
	return _request(
		"POST",
		f"{_base()}/api/internal/sessions/{session_id}/recompile",
		headers=_internal_headers(),
		context=f"recompile_session({session_id})",
	)

def get_session_by_token(token):
	"""Fetch public session details for a single token.

	Returns status, trackedSeconds, screenshotCount, videoUrl, metadata, etc.
	"""
	return _request(
		"GET",
		f"{_base()}/api/sessions/{token}",
		context=f"get_session({redact_token(token)})",
	)


def get_timings(token):
	"""Fetch the capture timestamps of every confirmed screenshot.

	Returns {status, count, first, last, timestamps: [...]}.
	"""
	return _request(
		"GET",
		f"{_base()}/api/sessions/{token}/timings",
		context=f"get_timings({redact_token(token)})",
	)


def batch_sessions(tokens):
	"""Fetch up to 100 sessions in one call (for gallery/dashboard views).

	Returns the raw batch payload keyed/listed by the Lookout server.
	"""
	tokens = list(tokens)
	if not tokens:
		return {}
	if len(tokens) > 100:
		raise LookoutError("batch_sessions accepts at most 100 tokens")
	return _request(
		"POST",
		f"{_base()}/api/sessions/batch",
		headers={"Content-Type": "application/json"},
		json={"tokens": tokens},
		context="batch_sessions",
	)

def video_url(session_id):
	return f"{_base()}/api/media/{session_id}/video.mp4"


def thumbnail_url(session_id):
	return f"{_base()}/api/media/{session_id}/thumbnail.jpg"

HACKATIME_BULK_URL = "https://hackatime.hackclub.com/api/hackatime/v1/users/current/heartbeats.bulk"


def forward_timelapse_to_hackatime(token, hackatime_api_key, *, entity="timelapse"):
	timings = get_timings(token)
	timestamps = timings.get("timestamps") or []
	if not timestamps:
		return 0

	from datetime import datetime, timezone as _tz

	def _epoch_seconds(iso):
		return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(
			tzinfo=_tz.utc
		).timestamp()

	heartbeats = [
		{
			"type": "file",
			"entity": entity,
			"category": "coding",
			"editor": "Lookout",
			"time": _epoch_seconds(iso),
		}
		for iso in timestamps
	]

	try:
		response = requests.post(
			HACKATIME_BULK_URL,
			headers={
				"Content-Type": "application/json",
				"Authorization": f"Bearer {hackatime_api_key}",
			},
			json=heartbeats,
			timeout=_TIMEOUT,
		)
	except requests.RequestException as exc:
		raise LookoutError(f"Hackatime push failed: {exc}") from exc

	if not response.ok:
		raise LookoutError(
			f"Hackatime push failed: {response.status_code}: {response.text[:300]}"
		)
	return len(heartbeats)
