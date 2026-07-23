from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required

from ..helpers import check_perms

@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment", "atlantis_site.t1_review", "atlantis_site.t2_review", "atlantis_site.t3_review", "atlantis_site.printer"])
def admin_dash(request):
    return render(request, "root/home.html")