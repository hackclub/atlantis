from django.shortcuts import render, redirect
from authlib.integrations.django_client import OAuth
from django.contrib.auth import login, logout
from django.contrib.auth.models import User
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import PermissionDenied
from django.contrib import messages

from .models import Profile
from .models import Project

import os

FORCE_REAUTH_COOKIE = "hca_force_reauth"

# setting up auth
oauth = OAuth()

oauth.register(
    name="hackclub",
    server_metadata_url="https://auth.hackclub.com/.well-known/openid-configuration",
    client_id = os.environ["HCA_CLIENT_ID"],
    client_secret = os.environ["HCA_CLIENT_SECRET"],
    client_kwargs = {
        "scope": "openid profile email verification_status slack_id"
    }
)

# auth views
@require_POST
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    # go to hackclub login
    redirect_uri = os.environ["HCA_CALLBACK_URI"]

    authorize_kwargs = {}
    if request.COOKIES.get(FORCE_REAUTH_COOKIE) == "1":
        authorize_kwargs["prompt"] = "login"

    response = oauth.hackclub.authorize_redirect(request, redirect_uri, **authorize_kwargs)
    response.delete_cookie(FORCE_REAUTH_COOKIE)
    return response

def auth_callback(request):
    token = oauth.hackclub.authorize_access_token(request)
    
    userinfo = token.get("userinfo")
    
    if not userinfo:
        userinfo = oauth.hackclub.userinfo(token=token)

    email = userinfo.get("email")
    name = userinfo.get("name", "")
    sub = userinfo.get("sub")

    user, created = User.objects.get_or_create(
        username=sub,
        defaults={
            "email": email,
            "first_name": userinfo.get("given_name", ""),
            "last_name": userinfo.get("family_name", "")
        },
    )

    Profile.objects.update_or_create(
        user=user,
        defaults={
            "verification_status": userinfo.get("verification_status", ""),
            "slack_id": userinfo.get("slack_id", ""),
        },
    )

    login(request, user)
    response = redirect("dashboard")
    response.delete_cookie(FORCE_REAUTH_COOKIE)
    return response

@require_POST
def logout_view(request):
    response = redirect("/")
    response.set_cookie(FORCE_REAUTH_COOKIE, "1", max_age=60 * 60 * 24, samesite="Lax")
    logout(request)
    return response

# regular views
def index(request):
    return render(request, "layered_site/home.html")

def dashboard(request):
    return render(request, "layered_site/dashboard.html")

def projects(request):
    return render(request, "layered_site/projects.html")

def explore(request):
    return render(request, "layered_site/explore.html")

def shop(request):
    return render(request, "layered_site/shop.html")

@login_required
def project_list(request):
    projects = request.user.projects.order_by("id")
    return render(request, "layered_site/projects.html", {"projects": projects})

@login_required
@require_POST
def create_project(request):
    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    printables_url = request.POST.get("printables_url", "").strip()

    if not title:
        messages.error(request, "Title is required.")
        return redirect("projects")

    project = Project.objects.create(
        owner = request.user,
        title = title,
        description = description,
        printablesUrl = printables_url
    )

    return redirect("projects")


@login_required
@require_POST
def edit_project(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id)

    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    printables_url = request.POST.get("printables_url", "").strip()

    if not title:
        messages.error(request, "Title is required.")
        return redirect("projects")

    project.title = title
    project.description = description
    project.printablesUrl = printables_url
    project.save()

    return redirect("projects")


@login_required
@require_POST
def delete_project(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id)
    project.delete()
    return redirect("projects")

@login_required
def project_detail(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id)

    return render(request, "layered_site/project_detail.html", {
        "project": project
    })

# admin views
@staff_member_required
def admin_dash(request):
    return render(request, "root/home.html")

@staff_member_required
def shop_dash(request):
    if not request.user.has_perm("layered_site.fulfillment"):
        raise PermissionDenied
    # fetch items and return later
    return render(request, "root/shop.html")

@staff_member_required
def fulfillment_dash(request):
    if not request.user.has_perm("layered_site.fulfillment"):
        raise PermissionDenied
    # fetch orders to fulfill later
    return render(request, "root/fulfillment.html")

@staff_member_required
def review_dash(request):
    if not request.user.has_perm("layered_site.t1_review"):
        raise PermissionDenied
    # fetch projects to review later
    return render(request, "root/review.html")

@staff_member_required
def ysws_review_dash(request):
    if not request.user.has_perm("layered_site.t2_review"):
        raise PermissionDenied
    # fetch projects to review later
    return render(request, "root/ysws_review.html")

@staff_member_required
def fraud_review_dash(request):
    if not request.user.has_perm("layered_site.t3_review"):
        raise PermissionDenied
    # fetch projects to review later
    return render(request, "root/fraud_review.html")