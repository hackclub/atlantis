from django.shortcuts import render
from authlib.integrations.django_client import OAuth
from django.contrib.auth import login, get_user_model
from django.contrib.auth.models import User
from dotenv import load_dotenv
from django.http import HttpResponse
from django.shortcuts import redirect

import os
import requests

from .models import Profile

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
def login_view(request):
    # go to hackclub login
    redirect_uri = os.environ["HCA_CALLBACK_URI"]

    return oauth.hackclub.authorize_redirect(request, redirect_uri)

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
    return redirect("dashboard")

# Create your views here.
def index(request):
    return render(request, "layered_site/home.html")


def dashboard(request):
    return render(request, "layered_site/dashboard.html")
