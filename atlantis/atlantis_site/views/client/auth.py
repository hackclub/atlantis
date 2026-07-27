from django.shortcuts import redirect
from authlib.integrations.django_client import OAuth
from django.contrib.auth import login, logout, get_user_model
from django.views.decorators.http import require_POST

from ...models import Profile
from ...crypto import encrypt_addresses
from ..helpers import slack_client, rate_limit

import os

FORCE_REAUTH_COOKIE = "hca_force_reauth"

oauth = OAuth()


def _extract_addresses(data):
    if not isinstance(data, dict):
        return []

    identity = data.get("identity")
    source = identity if isinstance(identity, dict) else data

    raw = source.get("addresses")
    if isinstance(raw, dict):
        raw = [raw]
    if not raw:
        single = source.get("address")
        raw = [single] if isinstance(single, dict) and single else []

    return [
        {k: v for k, v in address.items() if k != "phone_number"}
        for address in raw
        if isinstance(address, dict)
    ]

oauth.register(
    name="hackclub",
    server_metadata_url="https://auth.hackclub.com/.well-known/openid-configuration",
    client_id = os.environ["HCA_CLIENT_ID"],
    client_secret = os.environ["HCA_CLIENT_SECRET"],
    client_kwargs = {
        "scope": "openid email name profile verification_status slack_id address"
    }
)

@require_POST
@rate_limit("login", 2)
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

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

    email = userinfo.get("email", "hackclubber@example.com")
    name = userinfo.get("name", "")
    sub = userinfo.get("sub")
    clean_sub = sub.replace("!", "_")
    slack_id = userinfo.get("slack_id", "")
    verification_status = userinfo.get("verification_status", "")

    addresses = _extract_addresses(userinfo)
    if not addresses:
        try:
            full_userinfo = oauth.hackclub.userinfo(token=token)
            addresses = _extract_addresses(full_userinfo)
        except Exception as e:
            print("Address fetch failed", e)

    user_model = get_user_model()
    user, created = user_model.objects.get_or_create(
        username=clean_sub, 
        defaults={
            "email": email,
            "first_name": userinfo.get("given_name", ""),
            "last_name": userinfo.get("family_name", "")
        },
    )  

    if slack_id:
        try:
            slack_user = slack_client.users_info(user=slack_id)["user"]
            slack_profile = slack_user["profile"]

            display_name = (
                slack_profile.get("display_name")
                or slack_profile.get("real_name")
            )
            avatar_url = slack_profile.get("image_512")

        except Exception as e:
            print("Slack profile fetch failed", e)
            display_name = name
            avatar_url = os.environ["DEFAULT_PFP"]

    defaults = {
        "verification_status": verification_status,
        "slack_id": slack_id,
        "slack_username": display_name,
        "slack_pfp_url": avatar_url,
    }

    if addresses:
        defaults["encrypted_address"] = encrypt_addresses(addresses)

    Profile.objects.update_or_create(
        user=user,
        defaults=defaults,
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