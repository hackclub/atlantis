import logging

from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.http import JsonResponse

from ...models import Project, LookoutSession
from ... import lookout
from ..helpers import rate_limit

logger = logging.getLogger(__name__)


def _wants_json(request):
	"""True when the recorder popup is asking, rather than a browser navigating."""
	return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _recorder_config(session):
	"""Everything the browser recorder needs to drive one session.

	The token is what the recorder talks to Lookout with; it only ever goes to
	the session's own owner, which the views checked before calling this.
	"""
	return {
		"ok": True,
		"sessionPk": session.pk,
		"sessionId": session.session_id,
		"token": session.token,
		"baseUrl": settings.LOOKOUT_BASE_URL.rstrip("/"),
		"appName": settings.LOOKOUT_APP_NAME,
		"syncUrl": reverse("sync_timelapse", args=[session.pk]),
	}


def _recorder_page(session):
	"""The project page, told to pop the recorder open on this session."""
	return redirect(
		f"{reverse('project_detail', args=[session.project_id])}?record={session.pk}"
	)


@login_required
@require_POST
@rate_limit("start_timelapse", 3)
def start_timelapse(request, project_id):
	"""Create a Lookout session (server-to-server) and hand the user its recorder.

	Only the project owner may record. The secret API key never leaves the
	server; we store the returned token associated with the user/project so we
	can look the session up later, then hand the popup its config.
	"""
	project = get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)

	def refuse(message, status=400):
		# The popup shows the reason itself; a plain form submit gets the
		# project page and a note on the desk.
		if _wants_json(request):
			return JsonResponse({"ok": False, "error": message}, status=status)
		messages.error(request, message)
		return redirect("project_detail", project_id=project_id)

	if project.locked:
		return refuse("You cannot record a Lookout on a locked project.")

	try:
		data = lookout.create_session(metadata={
			"userId": str(request.user.id),
			"username": request.user.username,
			"projectId": str(project.id),
			"projectTitle": project.title,
		})
	except lookout.LookoutError as exc:
		# Never fail silently — surface it.
		return refuse(f"Couldn't start a Lookout right now: {exc}", status=502)

	token = data.get("token")
	session_id = data.get("sessionId")
	if not token or not session_id:
		return refuse(
			"Lookout returned an unexpected response; recording not started.",
			status=502,
		)

	session = LookoutSession.objects.create(
		project=project,
		owner=request.user,
		session_id=session_id,
		token=token,
		status=LookoutSession.Status.PENDING,
	)
	if _wants_json(request):
		return JsonResponse(_recorder_config(session))
	return _recorder_page(session)


@login_required
def record_timelapse(request, session_pk):
	"""Hand the recorder a session the current user owns.

	The recorder is a popup on the project page, so this serves it its config
	over XHR; anyone who lands on the URL itself is sent to the page that
	hosts the popup, with the session to open.

	The config carries the Lookout token so the recorder can talk to Lookout
	directly. This is the documented design — the client is untrusted and all
	timing is validated server-side.
	"""
	session = get_object_or_404(LookoutSession, pk=session_pk, owner=request.user)

	if _wants_json(request):
		return JsonResponse(_recorder_config(session))
	return _recorder_page(session)


def _apply_session_payload(session, session_obj, tracked_seconds, screenshot_count):
	"""Copy server-authoritative fields from a Lookout payload onto our model."""
	status = (session_obj or {}).get("status")
	if status in LookoutSession.Status.values:
		session.status = status
	if tracked_seconds is not None:
		session.tracked_seconds = int(tracked_seconds)
	if screenshot_count is not None:
		session.screenshot_count = int(screenshot_count)
	total_active = (session_obj or {}).get("totalActiveSeconds")
	if total_active is not None:
		session.total_active_seconds = int(total_active)
	session.save(update_fields=[
		"status", "tracked_seconds", "screenshot_count",
		"total_active_seconds", "updated_at",
	])


@login_required
@require_POST
@rate_limit("sync_timelapse", 2, json=True)
def sync_timelapse(request, session_pk):
	"""Refresh our cached copy of a session from Lookout's authoritative state.

	Called by the recorder JS (on stop/compile and periodically) so the backend
	always has the tamper-proof trackedSeconds for verification and display.
	Uses the internal API by server-side session ID.
	"""
	session = get_object_or_404(LookoutSession, pk=session_pk, owner=request.user)

	try:
		data = lookout.get_internal_session(session.session_id)
	except lookout.LookoutError as exc:
		# LookoutError carries the internal endpoint and the upstream body, so it
		# stays in the server log; the client only needs to know the hop failed.
		logger.warning(
			"Lookout sync failed for session %s (owner %s): %s",
			session.pk, request.user.pk, exc,
		)
		return JsonResponse(
			{"ok": False, "error": "Could not reach Lookout right now."},
			status=502,
		)

	_apply_session_payload(
		session,
		data.get("session"),
		data.get("trackedSeconds"),
		data.get("screenshotCount"),
	)

	return JsonResponse({
		"ok": True,
		"status": session.status,
		"trackedSeconds": session.tracked_seconds,
		"screenshotCount": session.screenshot_count,
		"totalActiveSeconds": session.total_active_seconds,
	})
