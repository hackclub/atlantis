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
from django.utils import timezone
from django.db import transaction

from .models import Profile, Project, Item, Order, Ship

from urllib.parse import urlparse

import os
import re

FORCE_REAUTH_COOKIE = "hca_force_reauth"
PRINTABLES_URL_RE = re.compile(r"https:\/\/(?:www\.)?printables\.com(?:\/.*)?", re.IGNORECASE)

def is_valid_image_url(url):
    try:
        result = urlparse(url)
        return result.scheme in ('http', 'https') and bool(result.netloc)
    except ValueError:
        return False

def is_valid_printables_url(value):
    return bool(PRINTABLES_URL_RE.match(value))

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
    clean_sub = sub.replace("!", "_")

    user, created = User.objects.get_or_create(
        username=clean_sub, 
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
    profile = request.user.hackclub_profile
    return render(request, "layered_site/dashboard.html", {'profile': profile})

def projects(request):
    profile = request.user.hackclub_profile
    return render(request, "layered_site/projects.html", {'profile': profile})

def explore(request):
    profile = request.user.hackclub_profile
    return render(request, "layered_site/explore.html", {'profile': profile})

def shop(request):
    profile = request.user.hackclub_profile
    items = Item.objects.filter(deleted=False).order_by("id")
    return render(request, "layered_site/shop.html", {"items": items, 'profile': profile})

@login_required
def project_list(request):
    projects = request.user.projects.filter(deleted=False).order_by("id")
    profile = request.user.hackclub_profile
    return render(request, "layered_site/projects.html", {"projects": projects, "profile": profile})

@login_required
@require_POST
def create_project(request):
    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    printables_url = request.POST.get("printables_url", "").strip()
    locked = False

    if not title:
        messages.error(request, "Title is required.")
        return redirect("projects")
    
    if not description:
        messages.error(request, "Description is required")
        return redirect("projects")
    
    if not is_valid_printables_url(printables_url):
        messages.error(request, "Printables URL must be a valid https://printables.com/xyz URL.")
        return redirect("projects")

    project = Project.objects.create(
        owner = request.user,
        title = title,
        description = description,
        printablesUrl = printables_url,
        locked = locked
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
    
    if not description:
        messages.error(request, "Description is required")
        return redirect("projects")
    
    if not is_valid_printables_url(printables_url):
        messages.error(request, "Printables URL must be a valid https://printables.com/xyz URL.")
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

    project.deleted = True
    project.save()

    return redirect("projects")

@login_required
def project_detail(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id)
    user = request.user
    profile = request.user.hackclub_profile
    ships = project.ships.order_by('-created_at')

    return render(request, "layered_site/project_detail.html", {
        "project": project,
        "user": user,
        "profile": profile,
        "ships": ships
    })

@login_required
def item_detail(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    profile = request.user.hackclub_profile

    return render(request, "layered_site/item_detail.html", {
        "item": item,
        "profile": profile,
    })

@login_required
def order_page(request, item_id):
    return redirect("item_detail", item_id=item_id)

@login_required
def order_item(request, item_id):
    if request.method != "POST":
        return redirect("item_detail", item_id=item_id)

    item = get_object_or_404(Item, id=item_id)
    quantity = request.POST.get("quantity", "").strip()
    user_notes = request.POST.get("user_notes", "").strip()

    if not quantity:
        messages.error(request, "Quantity is required.")
        return redirect("item_detail", item_id=item_id)
    
    try:
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        messages.error(request, "Quantity must be a positive number.")
        return redirect("item_detail", item_id=item_id)
    
    total_cost = item.cost * quantity

    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(user=request.user)

        if profile.layers < total_cost:
            messages.error(
                request,
                "You do not have enough layers to purchase this item."
            )
            return redirect("item_detail", item_id=item_id)
        
        profile.layers -= total_cost
        profile.save()

        Order.objects.create(
            owner=request.user,
            item=item,
            quantity=quantity,
            user_notes=user_notes
        )

    messages.success(request, f"Successfully ordered {quantity}x {item.name}!")
    return redirect("shop")

@login_required
def ship_project(request, project_id):
    if request.method != 'POST':
        return redirect("project_detail", project_id=project_id)
    
    project = get_object_or_404(Project, id=project_id)
    Ship.objects.create(
        project = project,
        status = Ship.ShipStatus.T1_QUEUE
    )
    
    messages.success(request, f'Successfully shipped project "{project.title}"!')
    return redirect("projects")


# staff views ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
# ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

@staff_member_required
def admin_dash(request):
    # extra layer of security never hurt anyone eh
    if not request.user.has_perm("layered_site.t1_review") and not request.user.has_perm("layered_site.t2_review") and not request.user.has_perm("layered_site.t3_review") and not request.user.has_perm("layered_site.printer") and not request.user.has_perm("layered_site.fulfillment") and not request.user.has_perm("layered_site.organizer"):
        raise PermissionDenied
    
    user_count = User.objects.count()
    project_count = Project.objects.count()
    ship_count = Ship.objects.count()
    return render(request, "root/home.html", {
        "users": user_count,
        "projects": project_count,
        "ships": ship_count
    })

@staff_member_required
def shop_dash(request):
    if not request.user.has_perm("layered_site.fulfillment") and not request.user.has_perm("layered_site.organizer"):
        raise PermissionDenied
    items = Item.objects.order_by("id")
    return render(request, "root/shop.html", {"items": items})

@staff_member_required
def fulfillment_dash(request):
    if not request.user.has_perm("layered_site.fulfillment") and not request.user.has_perm("layered_site.organizer"):
        raise PermissionDenied
    orders = Order.objects.select_related("item", "owner").order_by("-created_at")
    pending_orders = orders.filter(status=Order.OrderStatus.PENDING)
    other_orders = orders.exclude(status=Order.OrderStatus.PENDING)
    profile = request.user.hackclub_profile
    return render(request, "root/fulfillment.html", {
        "pending_orders": pending_orders,
        "other_orders": other_orders,
        "profile": profile,
    })


@staff_member_required
@require_POST
def update_order_status(request, order_id):
    if not request.user.has_perm("layered_site.fulfillment") and not request.user.has_perm("layered_site.organizer"):
        raise PermissionDenied

    order = get_object_or_404(Order.objects.select_related("item", "owner"), id=order_id)
    action = request.POST.get("action", "").strip()

    status_map = {
        "pending": Order.OrderStatus.PENDING,
        "fulfilled": Order.OrderStatus.FULFILLED,
        "denied": Order.OrderStatus.DENIED,
        "refunded": Order.OrderStatus.REFUNDED,
    }

    if action not in status_map:
        messages.error(request, "Invalid order action.")
        return redirect("fulfillment_dash")

    prev_status = order.status
    order.status = status_map[action]

    if prev_status == order.status:
        order.status = prev_status
        messages.error(request, f"Order status is already { {'P': 'pending', 'D': 'denied', 'F': 'fulfilled', 'R': 'refunded'}.get(order.status) }!")
        return redirect("fulfillment_dash")

    if order.status == Order.OrderStatus.FULFILLED:
        order.fulfilled_at = timezone.now()
    elif order.status == Order.OrderStatus.REFUNDED:
        amount_to_refund = order.item.cost * order.quantity
        with transaction.atomic():
            profile = Profile.objects.select_for_update().get(user=order.owner)

            profile.layers += amount_to_refund
            profile.save()
    else:
        order.fulfilled_at = None
    order.save(update_fields=["status", "fulfilled_at"])

    messages.success(request, f"Order #{order.id} updated to {order.get_status_display().lower()}.")
    return redirect("fulfillment_dash")

@staff_member_required
def print_dash(request):
    if not request.user.has_perm("layered_site.printer") and not request.user.has_perm("layered_site.organizer"):
        raise PermissionDenied
    
    ships = Ship.objects.filter(status=Ship.ShipStatus.PRINT_QUEUE)
    return render(request, "root/print.html")

@staff_member_required
def review_dash(request):
    if not request.user.has_perm("layered_site.t1_review") and not request.user.has_perm("layered_site.organizer") and not request.user.has_perm("layered_site.t2_review") and not request.user.has_perm("layered_site.t3_review"):
        raise PermissionDenied
    
    ships = Ship.objects.filter(status=Ship.ShipStatus.T1_QUEUE)
    return render(request, "root/review.html", {
        "ships": ships
    })

@staff_member_required
def review_project(request, ship_id):
    if not request.user.has_perm("layered_site.t1_review") and not request.user.has_perm("layered_site.organizer") and not request.user.has_perm("layered_site.t2_review") and not request.user.has_perm("layered_site.t3_review"):
        raise PermissionDenied

    ship = get_object_or_404(Ship, id=ship_id)
    return render(request, "root/review_project.html", {
        "ship": ship
    })

@staff_member_required
def ysws_review_dash(request):
    if not request.user.has_perm("layered_site.t2_review") and not request.user.has_perm("layered_site.organizer") and not request.user.has_perm("layered_site.t3_review"):
        raise PermissionDenied
    
    ships = Ship.objects.filter(status=Ship.ShipStatus.T2_QUEUE)
    return render(request, "root/ysws_review.html", {
        "ships": ships
    })

@staff_member_required
def fraud_review_dash(request):
    if not request.user.has_perm("layered_site.t3_review") and not request.user.has_perm("layered_site.organizer"):
        raise PermissionDenied
    
    ships = Ship.objects.filter(status=Ship.ShipStatus.T3_QUEUE)
    return render(request, "root/fraud_review.html", {
        "ships": ships
    })

@staff_member_required
@require_POST
def create_item(request):
    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    cost = request.POST.get("cost", "").strip()
    imageUrl = request.POST.get("imageUrl", "").strip()

    if not name:
        messages.error(request, "Name is required.")
        return redirect("shop_dash")
    
    if not description:
        messages.error(request, "Description is required.")
        return redirect("shop_dash")
    
    if not cost:
        messages.error(request, "Cost is required.")
        return redirect("shop_dash")

    if not imageUrl:
        messages.error(request, "Image URL is required.")
        return redirect("shop_dash")

    if not is_valid_image_url(imageUrl):
        messages.error(request, "Image URL must be a valid http or https URL.")
        return redirect("shop_dash")

    try:
        cost = int(cost)
    except ValueError:
        messages.error(request, "Cost must be a whole number.")
        return redirect("shop_dash")

    item = Item.objects.create(
        name = name,
        description = description,
        cost = cost,
        imageUrl = imageUrl
    )

    return redirect("shop_dash")

@staff_member_required
@require_POST
def edit_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    cost = request.POST.get("cost", "").strip()
    imageUrl = request.POST.get("imageUrl", "").strip()

    if not name:
        messages.error(request, "Name is required.")
        return redirect("shop_dash")
    
    if not description:
        messages.error(request, "Description is required.")
        return redirect("shop_dash")
    
    if not cost:
        messages.error(request, "Cost is required.")
        return redirect("shop_dash")

    if imageUrl and not is_valid_image_url(imageUrl):
        messages.error(request, "Image URL must be a valid http or https URL.")
        return redirect("shop_dash")

    try:
        cost = int(cost)
    except ValueError:
        messages.error(request, "Cost must be a whole number.")
        return redirect("shop_dash")

    item.name = name
    item.description = description
    item.cost = cost
    item.imageUrl = imageUrl
    item.save()

    return redirect("shop_dash")

@staff_member_required
@require_POST
def delete_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    item.deleted = True
    item.save()

    return redirect("shop_dash")

@staff_member_required
@require_POST
def lock_project(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    
    project.locked = True
    project.save()

    previous_page = request.META.get("HTTP_REFERER", "root/review")
    return redirect(previous_page)

@staff_member_required
@require_POST
def unlock_project(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    
    project.locked = False
    project.save()

    previous_page = request.META.get("HTTP_REFERER", "root/review")
    return redirect(previous_page)
