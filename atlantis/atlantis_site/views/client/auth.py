from django.shortcuts import redirect
from django.contrib.auth import login, logout, get_user_model
from django.views.decorators.http import require_POST

from ... import airtable
from ...models import Profile
from ...crypto import encrypt_token
from ...hca import extract_verification, oauth, storable_token
from ..helpers import slack_client, rate_limit, fit

import os

FORCE_REAUTH_COOKIE = "hca_force_reauth"

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
    if not sub:
        # Nothing to key an account on. Better a trip back to the front page
        # than an AttributeError on the next line.
        return redirect("/")
    clean_sub = sub.replace("!", "_")
    slack_id = userinfo.get("slack_id", "")
    verification_status, ysws_eligible = extract_verification(userinfo)

    user_model = get_user_model()
    user, created = user_model.objects.get_or_create(
        username=fit(clean_sub, user_model, "username"),
        defaults={
            "email": fit(email, user_model, "email"),
            "first_name": fit(userinfo.get("given_name", ""), user_model, "first_name"),
            "last_name": fit(userinfo.get("family_name", ""), user_model, "last_name"),
        },
    )  

    # Bound before the branch, not inside it: an identity with no Slack id
    # never entered it, and the defaults block below read two names that were
    # never assigned — a NameError, which is a 500 on somebody's login.
    display_name = name
    avatar_url = os.environ.get("DEFAULT_PFP", "")

    if slack_id:
        try:
            slack_user = slack_client.users_info(user=slack_id)["user"]
            slack_profile = slack_user["profile"]

            display_name = (
                slack_profile.get("display_name")
                or slack_profile.get("real_name")
                or name
            )
            avatar_url = slack_profile.get("image_512") or avatar_url

        except Exception as e:
            print("Slack profile fetch failed", e)

    # Trimmed rather than validated: none of this is ours to bounce a form
    # over — it is what HCA and Slack call this person — and a name longer than
    # its column is a DataError that would lock them out of the site entirely.
    defaults = {
        "verification_status": fit(verification_status, Profile, "verification_status"),
        "ysws_eligible": ysws_eligible,
        "slack_id": fit(slack_id, Profile, "slack_id"),
        "slack_username": fit(display_name, Profile, "slack_username"),
        "slack_pfp_url": fit(avatar_url, Profile, "slack_pfp_url"),
    }

    stored_token = storable_token(token)
    if stored_token:
        defaults["encrypted_hca_token"] = encrypt_token(stored_token)

    Profile.objects.update_or_create(
        user=user,
        defaults=defaults,
    )

    if created and airtable.emails_configured():
        contact = airtable.email_contact(user, fallback_name=name)
        if contact:
            airtable.upsert_emails_in_background([contact], f"signup for user #{user.id}")

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