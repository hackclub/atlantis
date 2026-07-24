from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.conf import settings
from django.http import JsonResponse

from ...models import Project, LookoutSession
from ... import lookout


@login_required
@require_POST
def start_timelapse(request, project_id):
	"""Create a Lookout session (server-to-server) and hand the user its recorder.

	Only the project owner may record. The secret API key never leaves the
	server; we store the returned token associated with the user/project so we
	can look the session up later, then redirect to our recorder page.
	"""
	project = get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)

	if project.locked:
		messages.error(request, "You cannot record a timelapse on a locked project.")
		return redirect("project_detail", project_id=project_id)

	try:
		data = lookout.create_session(metadata={
			"userId": str(request.user.id),
			"username": request.user.username,
			"projectId": str(project.id),
			"projectTitle": project.title,
		})
	except lookout.LookoutError as exc:
		# Never fail silently — surface it.
		messages.error(request, f"Couldn't start a timelapse right now: {exc}")
		return redirect("project_detail", project_id=project_id)

	token = data.get("token")
	session_id = data.get("sessionId")
	if not token or not session_id:
		messages.error(request, "Lookout returned an unexpected response; timelapse not started.")
		return redirect("project_detail", project_id=project_id)

	session = LookoutSession.objects.create(
		project=project,
		owner=request.user,
		session_id=session_id,
		token=token,
		status=LookoutSession.Status.PENDING,
	)
	return redirect("record_timelapse", session_pk=session.pk)


@login_required
def record_timelapse(request, session_pk):
	"""Render the browser recorder for a session the current user owns.

	The token is emitted into the page for the JS recorder to talk to Lookout
	directly. This is the documented design — the client is untrusted and all
	timing is validated server-side.
	"""
	session = get_object_or_404(LookoutSession, pk=session_pk, owner=request.user)

	return render(request, "atlantis_site/record_timelapse.html", {
		"session": session,
		"project": session.project,
		"lookout_base_url": settings.LOOKOUT_BASE_URL.rstrip("/"),
		"lookout_app_name": settings.LOOKOUT_APP_NAME,
	})


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
		return JsonResponse({"ok": False, "error": str(exc)}, status=502)

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
