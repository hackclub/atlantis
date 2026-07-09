from django.shortcuts import render
from django.contrib.auth import get_user_model
from django.contrib.admin.views.decorators import staff_member_required

from ...models import Project, Ship
from ..helpers import check_perms

@staff_member_required
@check_perms(["layered_site.organizer", "layered_site.fulfillment", "layered_site.t1_review", "layered_site.t2_review", "layered_site.t3_review", "layered_site.printer"])
def admin_dash(request):
    user_model = get_user_model()
    user_count = user_model.objects.count()
    project_count = Project.objects.count()
    ship_count = Ship.objects.count()
    return render(request, "root/home.html", {
        "users": user_count,
        "projects": project_count,
        "ships": ship_count
    })