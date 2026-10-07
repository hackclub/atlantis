"""Keeps banned users off the site.

A ban is a flag on the user's Profile, set from the admin users page. Rather
than teach every view to check it, this middleware answers every request from
a banned user with the banned screen before the view runs, so there is no page
to read and no form to post: no projects, no journals, no orders.

Two things are let through. Logging out, so a banned person can still sign out
of the account (or sign in as someone else on a shared machine), and static
files. WhiteNoise sits ahead of this and answers those before they get here;
the exemption is kept for anything under STATIC_URL it doesn't have a file for.
"""

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse


def is_banned(user):
    from .models import Profile

    if user is None or not user.is_authenticated:
        return False
    return Profile.objects.filter(user=user, banned=True).exists()


class BanMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not self._exempt(request.path) and is_banned(getattr(request, "user", None)):
            return render(request, "banned.html", status=403)
        return self.get_response(request)

    @staticmethod
    def _exempt(path):
        return path == reverse("logout") or path.startswith(settings.STATIC_URL)
