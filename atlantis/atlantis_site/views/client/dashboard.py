from django.shortcuts import render
from django.contrib.auth.decorators import login_required

def index(request):
    return render(request, "atlantis_site/home.html")

@login_required
def dashboard(request):
    profile = request.user.hackclub_profile
    return render(request, "atlantis_site/dashboard.html", {'profile': profile})