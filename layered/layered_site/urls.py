from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("auth/login/", views.login_view, name="login"),
    path("auth/logout/", views.logout_view, name="logout"),
    path("oauth/callback/", views.auth_callback, name="auth_callback"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("projects/", views.project_list, name="projects"),
    path("projects/<int:project_id>/edit/", views.edit_project, name="edit_project"),
    path("projects/<int:project_id>/delete/", views.delete_project, name="delete_project"),
    path("explore/", views.explore, name="explore"),
    path("shop/", views.shop, name="shop"),
    path("projects/create", views.create_project, name="create_project")
]