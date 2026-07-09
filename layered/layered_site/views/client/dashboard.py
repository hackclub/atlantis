from django.shortcuts import render

def index(request):
    return render(request, "layered_site/home.html")

def dashboard(request):
    profile = request.user.hackclub_profile
    return render(request, "layered_site/dashboard.html", {'profile': profile})