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
from django.db.models import Sum
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.paginator import Paginator

from .models import (
    Profile, Project, Item, Order, Ship, T1, T2, T3, Print, Journal, AuditLog,
    ALLOWED_EDITORS, EDITOR_FILE_EXTENSIONS, detect_editor_from_filename, detect_editor_from_link,
)

from urllib.parse import urlparse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from math import floor

import os
import re
import requests

FORCE_REAUTH_COOKIE = "hca_force_reauth"
PRINTABLES_URL_RE = re.compile(r"https:\/\/(?:www\.)?printables\.com(?:\/.*)?", re.IGNORECASE)
slack_client = WebClient(token=os.environ["SLACK_TOKEN"])

# helper functions
def is_valid_printables_url(value):
    return bool(PRINTABLES_URL_RE.match(value))

def get_client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")

def record_audit(request, action, target="", metadata=None):
    """Record an admin action in the audit log.

    Captures every value submitted through the form (minus the CSRF token),
    the names of any uploaded files, who acted, and where from. `metadata`
    holds the resulting state/extra context for the action.
    """
    form_data = {
        key: request.POST.getlist(key) if len(request.POST.getlist(key)) > 1 else value
        for key, value in request.POST.items()
        if key != "csrfmiddlewaretoken"
    }
    if request.FILES:
        form_data["_uploaded_files"] = {
            field: [f.name for f in request.FILES.getlist(field)]
            for field in request.FILES
        }

    try:
        AuditLog.objects.create(
            actor=request.user if request.user.is_authenticated else None,
            action=action,
            target=str(target)[:255],
            path=request.path,
            method=request.method,
            ip_address=get_client_ip(request),
            form_data=form_data,
            metadata=metadata or {},
        )
    except Exception as e:
        print("Failed to record audit log entry:", e)

def is_valid_image_url(url):
    try:
        result = urlparse(url)
        if result.scheme not in ('http', 'https') or not result.netloc:
            return False
        response = requests.head(url, allow_redirects=True, timeout=5)
        content_type = response.headers.get('Content-Type', '')
        return content_type.startswith('image/')
    except Exception:
        return False

def is_valid_stl_url(url):
    try:
        result = urlparse(url)
        if result.scheme not in ('http', 'https') or not result.netloc:
            return False
        response = requests.head(url, allow_redirects=True, timeout=5)
        content_type = response.headers.get('Content-Type', '')
        stl_content_types = ('model/stl', 'model/x.stl-ascii', 'model/x.stl-binary', 'application/sla')
        if any(content_type.startswith(ct) for ct in stl_content_types):
            return True
        if content_type.startswith('application/octet-stream') or not content_type:
            return result.path.lower().endswith('.stl')
        return False
    except Exception:
        return False

def get_model_info(model_id: str) -> dict:
    PRINTABLES_GRAPHQL_URL = os.environ['PRINTABLES_GRAPHQL_URL']
    QUERY = """
    query GetModelInfo($id: ID!) {
    print(id: $id) {
        id
        name
        slug
        makesCount
        license {
        id
        name
        disallowRemixing
        }
    }
    }
    """

    payload = {
        "operationName": "GetModelInfo",
        "variables": {"id": model_id},
        "query": QUERY,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.printables.com",
        "Referer": "https://www.printables.com/",
        "User-Agent": "Mozilla/5.0 (compatible; Layered/1.0)",
    }
    response = requests.post(PRINTABLES_GRAPHQL_URL, json=payload, headers=headers)
    response.raise_for_status()

    data = response.json()

    if "errors" in data:
        raise ValueError(f"GraphQL API errors: {data['errors']}")
    
    return data["data"]["print"]

def send_slack_dm(content, user):
    try:
        response = slack_client.chat_postMessage(
            channel=user,
            text=content
        )
        return True
    except SlackApiError:
        return False

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
    slack_id = userinfo.get("slack_id", "")
    verification_status = userinfo.get("verification_status", "")

    user, created = User.objects.get_or_create(
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

    Profile.objects.update_or_create(
        user=user,
        defaults={
            "verification_status": verification_status,
            "slack_id": slack_id,
            "slack_username": display_name,
            "slack_pfp_url": avatar_url
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

    projects = Project.objects.exclude(owner=request.user)
    return render(request, "layered_site/explore.html", {'profile': profile, 'projects': projects})

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

    project.title = title
    project.description = description
    project.printablesUrl = printables_url
    project.save()

    return redirect("projects")


@login_required
@require_POST
def update_editor_model(request, project_id):
    project = get_object_or_404(request.user.projects, id=project_id, deleted=False)

    if project.locked:
        messages.error(request, "You cannot edit a locked project.")
        return redirect("project_detail", project_id=project_id)

    editor_model_file = request.FILES.get("editor_model_file")
    editor_model_link = request.POST.get("editor_model_link", "").strip()

    if editor_model_file:
        if not detect_editor_from_filename(editor_model_file.name):
            messages.error(request, f"Unsupported editor model file. Supported editors: {', '.join(ALLOWED_EDITORS)}.")
            return redirect("project_detail", project_id=project_id)
        editor_model_key = default_storage.save(
            f"editor_models/{os.path.basename(editor_model_file.name)}", editor_model_file
        )
        project.editor_model_url = default_storage.url(editor_model_key)
    elif editor_model_link:
        if not editor_model_link.lower().startswith(("http://", "https://")):
            messages.error(request, "Editor model link must be a valid URL.")
            return redirect("project_detail", project_id=project_id)
        if not detect_editor_from_link(editor_model_link):
            messages.error(request, f"Unsupported editor model link. Supported editors: {', '.join(ALLOWED_EDITORS)}.")
            return redirect("project_detail", project_id=project_id)
        project.editor_model_url = editor_model_link
    else:
        messages.error(request, "Upload a file or provide a link for the editor model.")
        return redirect("project_detail", project_id=project_id)

    project.save()
    messages.success(request, "Editor model updated successfully.")
    return redirect("project_detail", project_id=project_id)


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
    journals = project.journals.order_by('-id')

    total_time = journals.aggregate(total=Sum('time_spent'))['total'] or 0
    time_spent = f"{floor(total_time / 60)}h {total_time % 60}m"

    latest_ship = ships.first()
    ship_pending = latest_ship is not None and latest_ship.status not in (Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED)

    if project.locked:
        can_ship = False
        ship_disabled_reason = "This project is locked and cannot be shipped."
    elif not is_valid_printables_url(project.printablesUrl):
        can_ship = False
        ship_disabled_reason = "You need a valid Printables URL before you can ship."
    elif not project.editor_model_url:
        can_ship = False
        ship_disabled_reason = "You need to upload or link your editor model before you can ship."
    elif ship_pending:
        can_ship = False
        ship_disabled_reason = "Your most recent ship must be finalized or rejected before you can reship."
    elif not project.journals.exists():
        can_ship = False
        ship_disabled_reason = "You must have at least one journal entry before you can ship."
    else:
        can_ship = True
        ship_disabled_reason = ""
    
    if project.printablesUrl:
        try:
            printablesData = get_model_info(project.printablesUrl.split('/model/')[1].split('-')[0])
        except:
            printablesData = {"makesCount": 0}
    else:
        printablesData = {"makesCount": 0}
    
    def get_latest_feedback(ship):
        candidates = []
        t1 = ship.t1_reviews.order_by('-reviewed_at').first()
        if t1 and t1.feedback:
            candidates.append((t1.reviewed_at, t1.feedback))
        t2 = ship.t2_reviews.order_by('-reviewed_at').first()
        if t2 and t2.feedback:
            candidates.append((t2.reviewed_at, t2.feedback))
        pr = ship.prints.exclude(decision=Print.Decision.PRINTING).order_by('-finished_time').first()
        if pr and pr.feedback and pr.finished_time:
            candidates.append((pr.finished_time, pr.feedback))
        return max(candidates, key=lambda x: x[0])[1] if candidates else ""

    ships_with_feedback = [(ship, get_latest_feedback(ship)) for ship in ships]

    return render(request, "layered_site/project_detail.html", {
        "project": project,
        "user": user,
        "profile": profile,
        "ships": ships,
        "ships_with_feedback": ships_with_feedback,
        "journals": journals,
        "time_spent": time_spent,
        "can_ship": can_ship,
        "ship_disabled_reason": ship_disabled_reason,
        "printablesData": printablesData,
        "allowed_editors": ALLOWED_EDITORS,
        "allowed_editor_extensions": ",".join(EDITOR_FILE_EXTENSIONS.keys()),
    })

@login_required
def project_detail_explore(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    user = request.user
    profile = user.hackclub_profile
    ships = project.ships.order_by("-created_at")
    journals = project.journals.order_by("-id")

    total_time = journals.aggregate(total=Sum('time_spent'))['total'] or 0
    time_spent = f"{floor(total_time / 60)}h {total_time % 60}m"

    if project.printablesUrl:
        try:
            printablesData = get_model_info(project.printablesUrl.split('/model/')[1].split('-')[0])
        except:
            printablesData = {"makesCount": 0}
    else:
        printablesData = {"makesCount": 0}

    return render(request, "layered_site/project_detail_explore.html", {
        "project": project,
        "user": user,
        "profile": profile,
        "ships": ships,
        "journals": journals,
        "time_spent": time_spent,
        "printablesData": printablesData,
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
def create_journal(request, project_id):
    if request.method != 'POST':
        return redirect("project_detail", project_id=project_id)
    
    if not settings.ALLOW_JOURNALING and not request.user.has_perm("layered_site.organizer"):
        messages.error(request, "Journaling is disallowed on this instance!")
        return redirect("project_detail", project_id=project_id)

    project = get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)

    if project.locked:
        messages.error(request, "You cannot create a journal on a locked project.")
        return redirect("projects")
    time_spent_raw = request.POST.get("time_spent", "0").strip()
    try:
        time_spent = int(time_spent_raw)

        if time_spent > 240:
            messages.error(request, "Time spent must not be greater than 4 hours!")
            return redirect("project_detail", project_id=project_id)
        if time_spent <= 30:
            messages.error(request, "You must spend at least 30 minutes on your journal entry!")
            return redirect("project_detail", project_id=project_id)
    except ValueError:
        messages.error(request, "Journal time spent must be an integer!")
        return redirect("project_detail", project_id=project_id)
    
    title = request.POST.get("title", "").strip()
    text = request.POST.get("text", "").strip()

    if len(text) <= 200:
        messages.error(request, "Journals must have at least 200 characters!")
        return redirect("project_detail", project_id=project_id)
    elif len(text) >= 2000:
        messages.error(request, "Journals must not be greater than 2000 characters!")
        return redirect("project_detail", project_id=project_id)

    image_file = request.FILES.get("image")
    model_file = request.FILES.get("STL")

    if not image_file:
        messages.error(request, "An image is required.")
        return redirect("project_detail", project_id=project_id)
    if not model_file:
        messages.error(request, "An STL model is required.")
        return redirect("project_detail", project_id=project_id)

    if not (image_file.content_type or "").startswith("image/"):
        messages.error(request, "Uploaded image must be an image file.")
        return redirect("project_detail", project_id=project_id)
    if not os.path.basename(model_file.name).lower().endswith(".stl"):
        messages.error(request, "Uploaded model must be an STL file.")
        return redirect("project_detail", project_id=project_id)

    image_key = default_storage.save(f"images/{os.path.basename(image_file.name)}", image_file)
    model_key = default_storage.save(f"models/{os.path.basename(model_file.name)}", model_file)

    image_url = default_storage.url(image_key)
    model_url = default_storage.url(model_key)

    Journal.objects.create(
        project=project,
        time_spent=time_spent,
        title=title,
        text=text,
        image_url=image_url,
        model_url=model_url
    )

    messages.success(request, "Journal entry created successfully")
    return redirect("project_detail", project_id=project_id)
    
@login_required
def ship_project(request, project_id):
    # remember to check if the weight is greater than the time spent x 100
    if request.method != 'POST':
        return redirect("project_detail", project_id=project_id)
    
    project = get_object_or_404(Project, id=project_id, owner=request.user, deleted=False)
    if project.locked:
        messages.error(request, "This project is locked. You cannot ship a locked project.")
        return redirect("projects")
    if not is_valid_printables_url(project.printablesUrl):
        messages.error(request, "you need a printables URL to ship!")
        return redirect("projects")
    if not project.editor_model_url:
        messages.error(request, "you need to upload or link your editor model before you can ship!")
        return redirect("projects")
    if not project.description:
        messages.error(request, "your project must have a description before you can ship!")
        return redirect("projects")
    if not project.journals.exists():
        messages.error(request, "your project must have at least one journal to be shipped")
        return redirect("projects")
    if (project.journals.aggregate(total=Sum('time_spent'))['total'] or 0) <= 180:
        messages.error(request, "you must have atleast 3 hours of logged time before you can ship!")
        return redirect("projects")

    latest_ship = project.ships.order_by('-created_at').first()
    if latest_ship and latest_ship.status not in (Ship.ShipStatus.FINALIZED, Ship.ShipStatus.REJECTED):
        messages.error(request, "You cannot reship until your most recent ship has been finalized or rejected.")
        return redirect("project_detail", project_id=project_id)

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
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.fulfillment", "layered_site.t1_review", "layered_site.t2_review", "layered_site.t3_review", "layered_site.printer"]):
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
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.fulfillment"]):
        raise PermissionDenied
    items = Item.objects.order_by("id")
    return render(request, "root/shop.html", {"items": items})

@staff_member_required
def fulfillment_dash(request):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.fulfillment"]):
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
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.fulfillment"]):
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

    order.fulfiller = request.user
    amount_refunded = None

    if order.status == Order.OrderStatus.REFUNDED:
        amount_refunded = order.item.cost * order.quantity
        with transaction.atomic():
            profile = Profile.objects.select_for_update().get(user=order.owner)
            profile.layers += amount_refunded
            profile.save()
    else:
        order.fulfilled_at = timezone.now()
    order.save(update_fields=["status", "fulfilled_at", "fulfiller"])

    record_audit(request, "update_order_status", target=f"Order #{order.id}", metadata={
        "order_id": order.id,
        "item": order.item.name,
        "owner": order.owner.username,
        "quantity": order.quantity,
        "previous_status": prev_status,
        "new_status": order.status,
    })

    owner_slack_id = order.owner.hackclub_profile.slack_id
    if owner_slack_id:
        dm_messages = {
            Order.OrderStatus.FULFILLED: f"Your order for {order.quantity}x {order.item.name} has been fulfilled!",
            Order.OrderStatus.DENIED: f"Your order for {order.quantity}x {order.item.name} was denied. Ask in #layered-help for more details.",
            Order.OrderStatus.REFUNDED: f"Your order for {order.quantity}x {order.item.name} was refunded and {amount_refunded} layers have been added back to your balance.",
            Order.OrderStatus.PENDING: f"Your order for {order.quantity}x {order.item.name} has been marked as pending again.",
        }
        send_slack_dm(dm_messages[order.status], owner_slack_id)

    messages.success(request, f"Order #{order.id} updated to {order.get_status_display().lower()}.")
    return redirect("fulfillment_dash")

@staff_member_required
def print_dash(request):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.printer", "layered_site.organizer"]):
        raise PermissionDenied

    claimed_prints = (
        Print.objects.filter(
            printer=user,
            unclaimed_time__isnull=True,
            finished_time__isnull=True,
        )
        .select_related("ship", "ship__project")
        .order_by("-claimed_time")
    )
    queued_ships = Ship.objects.filter(status=Ship.ShipStatus.PRINT_QUEUE).select_related("project")
    return render(request, "root/print.html", {
        "claimed_prints": claimed_prints,
        "ships": queued_ships,
        "user": user
    })

@staff_member_required
@require_POST
def claim_print(request, ship_id):
    user = request.user

    if not any(user.has_perm(p) for p in ["layered_site.printer", "layered_site.organizer"]):
        raise PermissionDenied

    ship = get_object_or_404(Ship, id=ship_id)

    if ship.prints.filter(unclaimed_time__isnull=True, finished_time__isnull=True).exists():
        messages.error(request, "already claimed")
        return redirect("print_dash")

    ship.status = Ship.ShipStatus.BEING_PRINTED
    ship.save()

    new_print = Print.objects.create(
        printer=user,
        ship=ship
    )

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    if owner_slack_id:
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> is being printed!", owner_slack_id)

    record_audit(request, "claim_print", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "print_id": new_print.id,
        "project": ship.project.title,
        "new_ship_status": ship.status,
    })

    messages.success(request, f"Claimed print '{ship.project.title}'.")
    return redirect("print_project", ship_id=ship_id)

@staff_member_required
@require_POST
def unclaim_print(request, ship_id):
    user = request.user

    if not any(user.has_perm(p) for p in ["layered_site.printer", "layered_site.organizer"]):
        raise PermissionDenied

    ship = get_object_or_404(Ship, id=ship_id)

    active_print = ship.prints.filter(
        unclaimed_time__isnull=True,
        finished_time__isnull=True
    ).order_by("-id").first()

    if not active_print:
        messages.error(request, "no active print found")
        return redirect("print_dash")

    active_print.unclaimed_time = timezone.now()
    active_print.decision = Print.Decision.UNCLAIMED
    active_print.save()

    ship.status = Ship.ShipStatus.PRINT_QUEUE
    ship.save()

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    if owner_slack_id:
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> is no longer being printed!", owner_slack_id)

    record_audit(request, "unclaim_print", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "print_id": active_print.id,
        "project": ship.project.title,
        "new_ship_status": ship.status,
    })

    messages.success(request, f"Unclaimed print '{ship.project.title}'")
    return redirect("print_dash")

@staff_member_required
def print_project(request, ship_id):
    user = request.user
    if not any(user.has_perm(p) for p in ["layered_site.printer", "layered_site.organizer"]):
        raise PermissionDenied
    
    ship = get_object_or_404(Ship, id=ship_id)
    journals = ship.project.journals.order_by('-id')
    if ship.prints.exists():
        current_print = ship.prints.all().order_by("-id").first()
    else:
        current_print = None

    return render(request, "root/print_project.html", {
        "current_print": current_print,
        "ship": ship,
        "journals": journals,
        "can_claim": ship.status == Ship.ShipStatus.PRINT_QUEUE,
    })

@staff_member_required
@require_POST
def print_decision(request, ship_id):
    user = request.user

    if not any(user.has_perm(p) for p in ["layered_site.printer", "layered_site.organizer"]):
        raise PermissionDenied

    ship = get_object_or_404(Ship, id=ship_id)

    weight_raw = request.POST.get("weight", "0").strip()
    image_url = request.POST.get("image_url", "").strip()

    if not is_valid_image_url(image_url):
        messages.error(request, "Invalid image URL")
        return redirect("print_project")

    try:
        weight = int(weight_raw)
    except ValueError:
        messages.error(request, f"Weight must be a whole number, received {weight_raw})")
        return redirect("print_dash")

    feedback = request.POST.get("feedback", "").strip()
    internal_notes = request.POST.get("internal_notes", "").strip()
    decision = request.POST.get("decision", "").strip()

    active_print = ship.prints.filter(
        unclaimed_time__isnull=True,
        finished_time__isnull=True
    ).order_by("-id").first()

    if not active_print:
        messages.error(request, "no active print found")
        return redirect("print_dash")

    active_print.finished_time = timezone.now()
    active_print.weight = weight
    active_print.internal_notes = internal_notes
    active_print.feedback = feedback
    active_print.decision = decision
    active_print.image_url = image_url
    active_print.save()
    
    match decision:
        case Print.Decision.RETURN_T1:
            ship.status = Ship.ShipStatus.T1_QUEUE
        case Print.Decision.APPROVE:
            ship.status = Ship.ShipStatus.T2_QUEUE
        case _:
            messages.error(request, f"Invalid decision (got: {decision})")
            return redirect("print_dash")

    ship.save()

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    if owner_slack_id:
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been printed and {"sent back to T1" if decision == Print.Decision.RETURN_T1 else "approved"}! Here's what they said about it: _{feedback}_", owner_slack_id)

    record_audit(request, "print_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "print_id": active_print.id,
        "project": ship.project.title,
        "decision": decision,
        "weight": weight,
        "new_ship_status": ship.status,
    })

    messages.success(
        request,
        f"Print {ship.project.title} with {decision}"
    )

    return redirect("print_dash")


@staff_member_required
def review_dash(request):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.t1_review", "layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"]):
        raise PermissionDenied
    
    ships = Ship.objects.filter(status=Ship.ShipStatus.T1_QUEUE)
    return render(request, "root/review.html", {
        "ships": ships
    })

@staff_member_required
def review_project(request, ship_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.t1_review", "layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"]):
        raise PermissionDenied

    ship = get_object_or_404(Ship, id=ship_id)
    journals = ship.project.journals.order_by('-id')
    try:
        hasMake = bool(get_model_info(ship.project.printablesUrl.split('/model/')[1].split('-')[0])["makesCount"])
    except:
        hasMake = False

    return render(request, "root/review_project.html", {
        "ship": ship,
        "journals": journals,
        "hasMake": hasMake,
    })

@require_POST
@staff_member_required
def t1_decision(request, ship_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.t1_review", "layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"]):
        raise PermissionDenied
    
    reviewer = request.user
    ship = get_object_or_404(Ship, id=ship_id)
    feedback = request.POST.get("feedback", "").strip()
    internal_notes = request.POST.get("internal_notes", "").strip()
    print_requested = "print" in request.POST
    approved_raw = request.POST.get("approved", "").strip()

    if approved_raw not in ("approved", "denied"):
        messages.error(request, f"How did we get here? (approved: {approved_raw})")
        return redirect("review_dash")

    approved = approved_raw == "approved"

    if approved:
        ship.status = Ship.ShipStatus.PRINT_QUEUE if print_requested else Ship.ShipStatus.T2_QUEUE
    else:
        ship.status = Ship.ShipStatus.REJECTED
    
    ship.save()

    t1 = T1.objects.create(
        reviewer=reviewer,
        ship=ship,
        feedback=feedback,
        internal_notes=internal_notes,
        print=print_requested,
        approved=approved
    )

    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    if owner_slack_id:
        send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been T1 reviewed and {"approved" if approved else "rejected"}! Here's what they said about it: _{feedback}_", owner_slack_id)

    record_audit(request, "t1_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t1_id": t1.id,
        "project": ship.project.title,
        "approved": approved,
        "print_requested": print_requested,
        "new_ship_status": ship.status,
    })

    messages.success(request, f'Successfully reviewed project "{ship.project.title}" with approved = {approved} and print = {print_requested}!')
    return redirect("review_dash")

@staff_member_required
def ysws_review_dash(request):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"]):
        raise PermissionDenied

    ships = Ship.objects.filter(status=Ship.ShipStatus.T2_QUEUE)
    return render(request, "root/ysws_review.html", {
        "ships": ships
    })

@staff_member_required
def ysws_review_project(request, ship_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"]):
        raise PermissionDenied

    ship = get_object_or_404(Ship, id=ship_id)
    journals = ship.project.journals.order_by('-id')
    return render(request, "root/ysws_review_project.html", {
        "ship": ship,
        "journals": journals,
    })

@require_POST
@staff_member_required
def t2_decision(request, ship_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.t2_review", "layered_site.organizer", "layered_site.t3_review"]):
        raise PermissionDenied
    
    reviewer = request.user
    ship = get_object_or_404(Ship, id=ship_id)
    decision = request.POST.get("decision", "").strip()
    deductions = request.POST.get("deductions", "0").strip()

    # remember to verify if deductions are greater than project time spent

    try:
        deductions = int(deductions) if deductions else 0
    except ValueError:
        messages.error(request, f"Expected integer, got {deductions}")
        return redirect("ysws_review_dash")

    feedback = request.POST.get("feedback", "").strip()
    justification = request.POST.get("justification", "").strip()
    owner_slack_id = ship.project.owner.hackclub_profile.slack_id
    
    match decision:
        case T2.Decision.APPROVE:
            ship.status = Ship.ShipStatus.T3_QUEUE
            message = "approved"
        case T2.Decision.RETURN_PRINT:
            ship.status = Ship.ShipStatus.PRINT_QUEUE
            message = "returned to the printers"
        case T2.Decision.RETURN_T1:
            ship.status = Ship.ShipStatus.T1_QUEUE
            message = "returned to T1 reviewers"
        case _:
            messages.error(request, f"How did we get here? (decision: {decision})")
            message = "(invalid decision, ask in <#C0AUP8VUU6T>)"
            return redirect("ysws_review_dash")
        
    send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been T2 reviewed and {message}! Here's what they said about it: _{feedback}_", owner_slack_id)
    
    with transaction.atomic():
        ship.save()

        t2 = T2.objects.create(
            ship=ship,
            reviewer=reviewer,
            decision=decision,
            deductions=deductions,
            feedback=feedback,
            justification=justification
        )

        remaining = deductions
        for journal in ship.project.journals.order_by('-id'):
            if remaining <= 0:
                break
            deduct = min(journal.time_spent, remaining)
            journal.time_spent -= deduct
            journal.save(update_fields=['time_spent'])
            remaining -= deduct
        
    record_audit(request, "t2_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t2_id": t2.id,
        "project": ship.project.title,
        "decision": decision,
        "deductions": deductions,
        "new_ship_status": ship.status,
    })

    messages.success(request, f'Successfully reviewed project "{ship.project.title}" with decision {decision} and deduction of {deductions} minutes!')
    return redirect("ysws_review_dash")

@staff_member_required
def fraud_review_dash(request):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.t3_review"]):
        raise PermissionDenied
    
    ships = Ship.objects.filter(status=Ship.ShipStatus.T3_QUEUE)
    return render(request, "root/fraud_review.html", {
        "ships": ships
    })

@staff_member_required
def fraud_review_project(request, ship_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.t3_review"]):
        raise PermissionDenied

    ship = get_object_or_404(Ship, id=ship_id)
    journals = ship.project.journals.order_by('-id')
    total_time = journals.aggregate(total=Sum('time_spent'))['total'] or 0

    return render(request, "root/fraud_review_project.html", {
        "ship": ship,
        "journals": journals,
        "total_time": total_time
    })

@require_POST
@staff_member_required
def t3_decision(request, ship_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.t3_review"]):
        raise PermissionDenied
    
    ship = get_object_or_404(Ship, id=ship_id)
    reviewer = request.user
    decision = request.POST.get("decision", "").strip()
    internal_notes = request.POST.get("internal_notes", "").strip()
    
    owner_slack_id = ship.project.owner.hackclub_profile.slack_id

    match decision:
        case T3.Decision.RETURN_T1:
            ship.status = Ship.ShipStatus.T1_QUEUE
            message = "returned to T1 reviewers"
        case T3.Decision.RETURN_T2:
            ship.status = Ship.ShipStatus.T2_QUEUE
            message = "returned to T2 reviewers"
        case T3.Decision.RETURN_PRINT:
            ship.status = Ship.ShipStatus.PRINT_QUEUE
            message = "returned to printers"
        case T3.Decision.APPROVE:
            ship.status = Ship.ShipStatus.FINALIZED
            # remember to payout here
        case _:
            messages.error(request, f"Invalid decision (received decision: {decision})")
            return redirect("fraud_review_dash")
    
    ship.save()

    send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been finalized!", owner_slack_id) if decision == T3.Decision.APPROVE else send_slack_dm(f"Your project <https://layered.hacklub.com/projects/{ship.project.id}|{ship.project.title}> has been {message}!", owner_slack_id)
    
    payout_time_raw = request.POST.get("payout_time", "0").strip()
    airtable_time_raw = request.POST.get("airtable_time", "0").strip()

    try:
        payout_time = int(payout_time_raw)
    except ValueError:
        messages.error(request, f"Expected integer, receieved {payout_time_raw}")
        return redirect(request, "fraud_review")
    
    try:
        airtable_time = int(airtable_time_raw)
    except ValueError:
        messages.error(request, f"Expected integer, receieved {airtable_time_raw}")
        return redirect(request, "fraud_review")

    t3 = T3.objects.create(
        ship=ship,
        reviewer=reviewer,
        decision=decision,
        internal_notes=internal_notes,
        payout_time=payout_time,
        airtable_time=airtable_time
    )

    record_audit(request, "t3_decision", target=f"Ship #{ship.id} ({ship.project.title})", metadata={
        "ship_id": ship.id,
        "t3_id": t3.id,
        "project": ship.project.title,
        "decision": decision,
        "payout_time": payout_time,
        "airtable_time": airtable_time,
        "new_ship_status": ship.status,
    })

    messages.success(request, f"Sucessfully reviewed project '{ship.project.title}' with decision {decision}")
    return redirect("fraud_review_dash")

@staff_member_required
@require_POST
def create_item(request):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.fulfillment"]):
        raise PermissionDenied
    
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

    record_audit(request, "create_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "name": item.name,
        "cost": item.cost,
    })

    return redirect("shop_dash")

@staff_member_required
@require_POST
def edit_item(request, item_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.fulfillment"]):
        raise PermissionDenied

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

    previous = {
        "name": item.name,
        "description": item.description,
        "cost": item.cost,
        "imageUrl": item.imageUrl,
    }

    item.name = name
    item.description = description
    item.cost = cost
    item.imageUrl = imageUrl
    item.save()

    record_audit(request, "edit_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "previous": previous,
        "new": {"name": name, "description": description, "cost": cost, "imageUrl": imageUrl},
    })

    return redirect("shop_dash")

@staff_member_required
@require_POST
def delete_item(request, item_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.fulfillment"]):
        raise PermissionDenied

    item = get_object_or_404(Item, id=item_id)

    item.deleted = True
    item.save()

    record_audit(request, "delete_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "name": item.name,
    })

    return redirect("shop_dash")

@staff_member_required
@require_POST
def lock_project(request, project_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.t2_review", "layered_site.t3_review"]):
        raise PermissionDenied

    project = get_object_or_404(Project, id=project_id, deleted=False)
    
    project.locked = True
    project.save()

    record_audit(request, "lock_project", target=f"Project #{project.id} ({project.title})", metadata={
        "project_id": project.id,
        "project": project.title,
        "owner": project.owner.username,
    })

    previous_page = request.META.get("HTTP_REFERER", "root/review")
    return redirect(previous_page)

@staff_member_required
def audit_log(request):
    user = request.user
    # organizers only
    if not user.has_perm("layered_site.organizer"):
        raise PermissionDenied

    logs = AuditLog.objects.select_related("actor").all()

    action_filter = request.GET.get("action", "").strip()
    actor_filter = request.GET.get("actor", "").strip()

    if action_filter:
        logs = logs.filter(action=action_filter)
    if actor_filter:
        logs = logs.filter(actor__username__icontains=actor_filter)

    actions = AuditLog.objects.order_by("action").values_list("action", flat=True).distinct()

    paginator = Paginator(logs, 50)
    page = paginator.get_page(request.GET.get("page"))

    return render(request, "root/audit_log.html", {
        "page": page,
        "logs": page.object_list,
        "actions": actions,
        "action_filter": action_filter,
        "actor_filter": actor_filter,
    })

@staff_member_required
@require_POST
def unlock_project(request, project_id):
    user = request.user
    if not any(user.has_perm(perm) for perm in ["layered_site.organizer", "layered_site.t2_review", "layered_site.t3_review"]):
        raise PermissionDenied

    project = get_object_or_404(Project, id=project_id, deleted=False)
    
    project.locked = False
    project.save()

    record_audit(request, "unlock_project", target=f"Project #{project.id} ({project.title})", metadata={
        "project_id": project.id,
        "project": project.title,
        "owner": project.owner.username,
    })

    previous_page = request.META.get("HTTP_REFERER", "root/review")
    return redirect(previous_page)
