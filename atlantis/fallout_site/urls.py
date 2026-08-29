from django.urls import path

from . import lookout_media, views

urlpatterns = [
    # --- Time audit queue (Fallout paths, copied frontend needs no changes) ---
    path("admin/reviews/time_audits/", views.time_audit_index, name="time_audit_index"),
    path("admin/reviews/time_audits/next", views.time_audit_next, name="time_audit_next"),
    path("admin/reviews/time_audits/<int:review_id>/heartbeat", views.time_audit_heartbeat, name="time_audit_heartbeat"),
    path("admin/reviews/time_audits/<int:review_id>", views.time_audit_show, name="time_audit_show"),
    path("admin/reviews/time_audits/<int:review_id>/update", views.time_audit_update, name="time_audit_update"),
    # --- Reviewer notes (per project) ---
    path("admin/projects/<int:project_id>/reviewer_notes", views.reviewer_notes_create, name="reviewer_notes"),
    path("admin/project_flags", views.project_flags_create, name="project_flags"),
    path(
        "admin/projects/<int:project_id>/reviewer_notes/<int:note_id>",
        views.reviewer_notes_update,
        name="reviewer_note",
    ),
    # --- Redirects for fallout URLs the copied UI links to ---
    path("admin/projects/<int:project_id>", views.admin_project_redirect, name="admin_project_redirect"),
    path("admin/users/<int:user_id>", views.admin_user_redirect, name="admin_user_redirect"),
    # --- Mock Lookout media server (dev-only sample videos/thumbnails) ---
    path("mock-lookout/", lookout_media.mock_root, name="mock_lookout_root"),
    path(
        "mock-lookout/<str:session_id>/video.mp4",
        lookout_media.mock_video,
        name="mock_lookout_video",
    ),
    path(
        "mock-lookout/<str:session_id>/thumbnail.jpg",
        lookout_media.mock_thumbnail,
        name="mock_lookout_thumbnail",
    ),
]