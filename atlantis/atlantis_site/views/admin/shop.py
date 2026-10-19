from datetime import timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.core.paginator import Paginator
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.http import JsonResponse
from django.db.models import Count, Exists, Min, OuterRef, Q

from ...models import Profile, Item, Order, SaverCredit, ShopCategory
from ...crypto import format_address
from ...hca import (
    IdentityUnavailable, extract_addresses, extract_contact, extract_phone,
    fetch_userinfo, select_address,
)
from .queue import age_bucket, age_display, forget_pending_counts
from ..helpers import check_perms, staff_member_required, record_audit, run_in_background, send_slack_dm, is_valid_image_url, INT_FIELD_MAX, field_max_length, too_long

@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def shop_dash(request):
    # Printer rows are left out: they are generated from printers.py by
    # sync_printer_items, nothing here can edit them, and there are dozens of
    # them. Their prices live on the charts, which is where they are decided.
    # Reviewer items are left out too: they are priced in review work, not
    # pearls, and are managed on the reviewer shop.
    items = Item.objects.exclude(kind__in=Item.HIDDEN_KINDS).order_by("id")
    # Categories a rename or a delete left behind are still listed, flagged as
    # unused: they hold their slot for whenever an item lands back in them.
    categories = ShopCategory.objects.annotate(
        in_use=Exists(Item.objects.filter(category=OuterRef("name"), deleted=False))
    )
    return render(request, "root/shop.html", {
        "items": items,
        "categories": categories,
        "item_kinds": [(v, label) for v, label in Item.Kind.choices if v not in Item.HIDDEN_KINDS],
        "printer_item_count": Item.objects.filter(kind=Item.Kind.PRINTER, deleted=False).count(),
    })

# Days an order may wait before its row is called out as overdue. Nothing
# enforces it; it only decides when the waiting time turns amber, then red.
FULFILLMENT_SLA_DAYS = 7

# One page of the decided-orders table. The pending queue is never paged: it
# is the work, and all of it should be on screen.
ORDERS_PER_PAGE = 50

ORDER_SEARCH_MAX_LENGTH = 100

# The decided table's status tabs, in the order they are drawn.
DECIDED_FILTERS = (
    ("", "All"),
    (Order.OrderStatus.FULFILLED, "Fulfilled"),
    (Order.OrderStatus.DENIED, "Denied"),
    (Order.OrderStatus.REFUNDED, "Refunded"),
)


def _search_orders(orders, query):
    """Narrow orders to those matching `query`: an #id, an item, or a buyer."""
    if not query:
        return orders
    order_id = query.lstrip("#")
    if order_id.isdigit():
        return orders.filter(id=int(order_id))
    return orders.filter(
        Q(item__name__icontains=query)
        | Q(owner__username__icontains=query)
        | Q(owner__first_name__icontains=query)
        | Q(owner__last_name__icontains=query)
        | Q(owner__hackclub_profile__slack_username__icontains=query)
    )


def _order_rows(orders):
    """Stamp each order with what its row prints that isn't a field."""
    for order in orders:
        order.total_cost = order.cost * order.quantity
        order.total_label = order.item.price_label(order.total_cost)
        order.each_label = order.item.price_label(order.cost)
        order.age_display = age_display(order.created_at)
        order.age_bucket = age_bucket(order.created_at, FULFILLMENT_SLA_DAYS)
    return orders


@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def fulfillment_dash(request):
    query = request.GET.get("q", "").strip()[:ORDER_SEARCH_MAX_LENGTH]
    status_filter = request.GET.get("status", "")
    if status_filter not in dict(DECIDED_FILTERS):
        status_filter = ""

    orders = _search_orders(
        Order.objects.select_related(
            "item", "owner__hackclub_profile", "fulfiller__hackclub_profile"
        ),
        query,
    )
    # The queue is worked oldest first, like every other desk; what has been
    # decided reads newest first.
    pending_orders = _order_rows(list(
        orders.filter(status=Order.OrderStatus.PENDING).order_by("created_at")
    ))
    other_orders = orders.exclude(status=Order.OrderStatus.PENDING).order_by("-created_at")

    decided_counts = dict(
        other_orders.order_by().values_list("status").annotate(n=Count("id"))
    )
    filter_options = [
        {
            "value": value,
            "label": label,
            "count": sum(decided_counts.values()) if not value else decided_counts.get(value, 0),
            "active": value == status_filter,
        }
        for value, label in DECIDED_FILTERS
    ]
    if status_filter:
        other_orders = other_orders.filter(status=status_filter)
    other_page = Paginator(other_orders, ORDERS_PER_PAGE).get_page(request.GET.get("page"))
    _order_rows(other_page.object_list)

    # The header is about the queue as a whole, so it ignores the search.
    all_pending = Order.objects.filter(status=Order.OrderStatus.PENDING)
    oldest = all_pending.aggregate(oldest=Min("created_at"))["oldest"]
    week_ago = timezone.now() - timedelta(days=7)
    stat_cards = [
        {
            "label": "Waiting",
            "value": all_pending.count(),
            "description": "orders not yet sent out",
        },
        {
            "label": "Oldest",
            "value": age_display(oldest) if oldest else None,
            "description": f"against a {FULFILLMENT_SLA_DAYS}d SLA",
            "breached": oldest is not None and age_bucket(oldest, FULFILLMENT_SLA_DAYS) == "overdue",
        },
        {
            "label": "Fulfilled this week",
            "value": Order.objects.filter(
                status=Order.OrderStatus.FULFILLED, fulfilled_at__gte=week_ago
            ).count(),
            "description": "marked fulfilled in the last 7 days",
        },
        {
            "label": "Yours this week",
            "value": Order.objects.filter(
                status=Order.OrderStatus.FULFILLED, fulfilled_at__gte=week_ago,
                fulfiller=request.user,
            ).count(),
            "description": "fulfilled by you in the last 7 days",
        },
    ]

    return render(request, "root/fulfillment.html", {
        "pending_orders": pending_orders,
        "other_orders": other_orders,
        "other_page": other_page,
        "filter_options": filter_options,
        "status_filter": status_filter,
        "filter_query": query,
        "filter_search_max": ORDER_SEARCH_MAX_LENGTH,
        "stat_cards": stat_cards,
        "sla_days": FULFILLMENT_SLA_DAYS,
        "queue_label": "Fulfillment",
        "queue_short": "Shop",
        "queue_accent": "good",
    })

def _back_to_dash(request):
    """Back to the dash as it was: same search, same tab, same page."""
    next_url = request.POST.get("next", "")
    if next_url.startswith(reverse("fulfillment_dash")):
        return redirect(next_url)
    return redirect("fulfillment_dash")

@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def update_order_status(request, order_id):
    action = request.POST.get("action", "").strip()

    status_map = {
        "pending": Order.OrderStatus.PENDING,
        "fulfilled": Order.OrderStatus.FULFILLED,
        "denied": Order.OrderStatus.DENIED,
        "refunded": Order.OrderStatus.REFUNDED,
    }

    if action not in status_map:
        messages.error(request, "Invalid order action.")
        return _back_to_dash(request)
    
    order = get_object_or_404(Order, id=order_id)

    with transaction.atomic():
        order = Order.objects.select_for_update().get(id=order.id)
        # get_or_create rather than get: a user who has never been through the
        # HCA login (a `createsuperuser` account, say) has no profile row, and
        # refunding their order used to be a DoesNotExist rather than a refund.
        profile, _ = Profile.objects.get_or_create(user=order.owner)
        profile = Profile.objects.select_for_update().get(pk=profile.pk)

        prev_status = order.status
        order.status = status_map[action]

        if order.refunded:
            order.status = prev_status
            messages.error(request, "This order has already been refunded and cannot be further edited.")
            return _back_to_dash(request)
        if prev_status == order.status:
            order.status = prev_status
            messages.error(request, f"Order status is already { {'P': 'pending', 'D': 'denied', 'F': 'fulfilled', 'R': 'refunded'}.get(order.status) }!")
            return _back_to_dash(request)

        order.fulfiller = request.user
        amount_refunded = None

        held_statuses = {Order.OrderStatus.PENDING, Order.OrderStatus.FULFILLED}
        was_held = prev_status in held_statuses
        now_held = order.status in held_statuses
        if was_held != now_held:
            item = Item.objects.select_for_update().get(id=order.item_id)
            if not item.unlimited_stock:
                if now_held:
                    item.stock = max(0, item.stock - order.quantity)
                else:
                    item.stock += order.quantity
                item.save(update_fields=["stock"])

        saver_hours_reversed = 0
        if order.status == Order.OrderStatus.REFUNDED:
            amount_refunded = order.cost * order.quantity
            # A reviewer item was paid for in review work, which comes back on
            # its own: reviewer_shop.py stops counting a refunded order as spent.
            if not order.item.is_reviewer_item:
                profile.layers += amount_refunded
                profile.save()
            order.refunded = True
            # Savers are spent the moment they are bought, so refunding one has
            # to take the hours back with the pearls — otherwise a refund is a
            # free week. This can put the buyer back out of the program, which
            # is the correct outcome: they no longer paid for the rescue.
            saver_hours_reversed = SaverCredit.objects.filter(order=order).delete()[0]
        elif order.status == Order.OrderStatus.FULFILLED:
            order.fulfilled_at = timezone.now()
        order.save(update_fields=["status", "fulfilled_at", "fulfiller", "refunded"])

    record_audit(request, "update_order_status", target=f"Order #{order.id}", metadata={
        "order_id": order.id,
        "item": order.item.name,
        "owner": order.owner.username,
        "quantity": order.quantity,
        "previous_status": prev_status,
        "new_status": order.status,
        "saver_hours_reversed": saver_hours_reversed,
    })

    owner_slack_id = profile.slack_id
    if owner_slack_id:
        dm_messages = {
            Order.OrderStatus.FULFILLED: f"Your order for {order.quantity}x {order.item.name} has been fulfilled!",
            Order.OrderStatus.DENIED: f"Your order for {order.quantity}x {order.item.name} was denied. Ask in #atlantis-help for more details.",
            Order.OrderStatus.REFUNDED: (
                f"Your order for {order.quantity}x {order.item.name} was refunded and "
                f"{order.item.price_label(amount_refunded or 0)} "
                f"{'have' if amount_refunded != 1 else 'has'} been added back to your "
                f"{'reviewer shop ' if order.item.is_reviewer_item else ''}balance."
            ),
            Order.OrderStatus.PENDING: f"Your order for {order.quantity}x {order.item.name} has been marked as pending again.",
        }
        run_in_background(send_slack_dm, dm_messages[order.status], owner_slack_id)

    # The fulfillment badge is the one desk that doesn't end in go_to_next.
    forget_pending_counts()
    messages.success(request, f"Order #{order.id} updated to {order.get_status_display().lower()}.")
    return _back_to_dash(request)

def address_lookup(user, address_id=""):
    """`(status, body)` for the JSON a "View address" button reads.

    The address, full name, email and phone number are pulled live from HCA
    with the user's stored token — none of it is stored on our side.
    `address_id` picks which of their addresses (an order's); without one it
    is the primary. Callers audit-log a 200, since that is a plaintext
    address handed to someone.
    """
    profile = getattr(user, "hackclub_profile", None)

    # One userinfo call for all of it: the address and the name, email and
    # phone to reach the person at come out of the same response.
    userinfo = {}
    if profile is not None:
        try:
            userinfo = fetch_userinfo(profile)
        except IdentityUnavailable:
            return 503, {"ok": False, "error": "address_unavailable"}

    address = format_address(select_address(extract_addresses(userinfo), address_id))
    if address is None:
        return 404, {"ok": False, "error": "no_address"}

    name, email = extract_contact(userinfo)
    return 200, {
        "ok": True,
        "address": address,
        "contact": {"name": name, "email": email, "phone": extract_phone(userinfo, address_id)},
    }


@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def view_order_address(request, order_id):
    """The buyer's shipping address for this order, and how to reach them."""
    order = get_object_or_404(Order.objects.select_related("owner"), id=order_id)
    status, body = address_lookup(order.owner, order.address_id)

    if status == 200:
        record_audit(request, "view_order_address", target=f"Order #{order.id}", metadata={
            "order_id": order.id,
            "owner": order.owner.username,
            "address_id": body["address"].get("id", ""),
        })
    return JsonResponse(body, status=status)


# Item names, costs and stock arrive as free text on the shop form, and every
# one of them lands in a column that answers a bad value with a driver error
# rather than a message. Checked in one place because create and edit take the
# same fields and have to agree about what fits.
def _item_field_error(name, description, category, imageUrl, cost, stock, kind=Item.Kind.REGULAR):
    """The first reason these values can't be stored, or None."""
    # PRINTER rows are generated from printers.py and joined to a tree by
    # printer_key; one typed in here would have no key and no printer behind it.
    if kind not in (Item.Kind.REGULAR, Item.Kind.SAVER_CURRENT, Item.Kind.SAVER_PAST):
        return "Pick a valid item kind."

    for label, value, field in (
        ("Name", name, "name"),
        ("Description", description, "description"),
        ("Category", category, "category"),
        ("Image URL", imageUrl, "imageUrl"),
    ):
        if too_long(value, Item, field):
            return f"{label} too long (max {field_max_length(Item, field)} chars)."

    # `cost` is a PositiveIntegerField, so a negative one is a CHECK violation
    # — an IntegrityError, not a validation message — and either end of the
    # range overflows the column.
    if cost < 0:
        return "Cost can't be negative."
    if cost > INT_FIELD_MAX:
        return f"Cost must be {INT_FIELD_MAX} or less."
    if stock > INT_FIELD_MAX:
        return f"Stock must be {INT_FIELD_MAX} or less."
    return None


@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def create_item(request):
    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    cost = request.POST.get("cost", "").strip()
    imageUrl = request.POST.get("imageUrl", "").strip()
    category = request.POST.get("category", "").strip() or "Other"
    kind = request.POST.get("kind", "").strip() or Item.Kind.REGULAR

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

    stock = request.POST.get("stock", "").strip() or "-1"
    try:
        stock = int(stock)
    except ValueError:
        messages.error(request, "Stock must be a whole number (-1 for unlimited).")
        return redirect("shop_dash")
    if stock < -1:
        messages.error(request, "Stock must be -1 (unlimited) or a non-negative number.")
        return redirect("shop_dash")

    field_error = _item_field_error(name, description, category, imageUrl, cost, stock, kind)
    if field_error:
        messages.error(request, field_error)
        return redirect("shop_dash")

    ShopCategory.ensure(category)

    item = Item.objects.create(
        name = name,
        description = description,
        cost = cost,
        imageUrl = imageUrl,
        category = category,
        stock = stock,
        kind = kind,
    )

    record_audit(request, "create_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "name": item.name,
        "cost": item.cost,
        "category": item.category,
        "stock": item.stock,
        "kind": item.kind,
    })

    return redirect("shop_dash")

@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def edit_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    # A printer row is generated from printers.py and kept in step with it by
    # sync_printer_items; editing one here would be overwritten on the next
    # sync and would put its price out of line with the tree it is drawn on.
    if item.kind == Item.Kind.PRINTER:
        messages.error(request, "Printer items are generated from the tech trees and can't be edited here.")
        return redirect("shop_dash")
    if item.is_reviewer_item:
        messages.error(request, "Reviewer shop items are edited on the reviewer shop.")
        return redirect("shop_dash")

    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    cost = request.POST.get("cost", "").strip()
    imageUrl = request.POST.get("imageUrl", "").strip()
    category = request.POST.get("category", "").strip() or "Other"
    kind = request.POST.get("kind", "").strip() or Item.Kind.REGULAR

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

    stock = request.POST.get("stock", "").strip() or "-1"
    try:
        stock = int(stock)
    except ValueError:
        messages.error(request, "Stock must be a whole number (-1 for unlimited).")
        return redirect("shop_dash")
    if stock < -1:
        messages.error(request, "Stock must be -1 (unlimited) or a non-negative number.")
        return redirect("shop_dash")

    field_error = _item_field_error(name, description, category, imageUrl, cost, stock, kind)
    if field_error:
        messages.error(request, field_error)
        return redirect("shop_dash")

    previous = {
        "name": item.name,
        "description": item.description,
        "cost": item.cost,
        "imageUrl": item.imageUrl,
        "category": item.category,
        "stock": item.stock,
        "kind": item.kind,
    }

    ShopCategory.ensure(category)

    item.name = name
    item.description = description
    item.cost = cost
    item.imageUrl = imageUrl
    item.category = category
    item.stock = stock
    item.kind = kind
    item.save()

    record_audit(request, "edit_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "previous": previous,
        "new": {"name": name, "description": description, "cost": cost, "imageUrl": imageUrl, "category": category, "stock": stock, "kind": kind},
    })

    return redirect("shop_dash")

@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def reorder_categories(request):
    names = [name.strip() for name in request.POST.getlist("category") if name.strip()]
    categories = list(ShopCategory.objects.all())

    # The whole list has to come back, or the categories left out would keep
    # slots that now collide with the ones being saved.
    if sorted(names) != sorted(category.name for category in categories):
        messages.error(request, "Category order didn't match the categories on file. Reload and try again.")
        return redirect("shop_dash")

    position_of = {name: position for position, name in enumerate(names, start=1)}
    moved = [c for c in categories if c.sort_order != position_of[c.name]]
    for category in moved:
        category.sort_order = position_of[category.name]
    if moved:
        ShopCategory.objects.bulk_update(moved, ["sort_order"])
        record_audit(request, "reorder_shop_categories", metadata={"order": names})

    messages.success(request, "Category order saved.")
    return redirect("shop_dash")

@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def delete_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    item.deleted = True
    item.save()

    record_audit(request, "delete_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "name": item.name,
    })

    return redirect("shop_dash")