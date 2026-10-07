from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from django.http import JsonResponse
from django.db.models import Exists, OuterRef

from ...models import Profile, Item, Order, SaverCredit, ShopCategory
from ...crypto import format_address
from ...hca import (
    IdentityUnavailable, extract_addresses, extract_contact, extract_phone,
    fetch_userinfo, select_address,
)
from ..helpers import check_perms, record_audit, run_in_background, send_slack_dm, is_valid_image_url, INT_FIELD_MAX, field_max_length, too_long

@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def shop_dash(request):
    # Printer rows are left out: they are generated from printers.py by
    # sync_printer_items, nothing here can edit them, and there are dozens of
    # them. Their prices live on the charts, which is where they are decided.
    items = Item.objects.exclude(kind=Item.Kind.PRINTER).order_by("id")
    # Categories a rename or a delete left behind are still listed, flagged as
    # unused: they hold their slot for whenever an item lands back in them.
    categories = ShopCategory.objects.annotate(
        in_use=Exists(Item.objects.filter(category=OuterRef("name"), deleted=False))
    )
    return render(request, "root/shop.html", {
        "items": items,
        "categories": categories,
        "item_kinds": Item.Kind.choices,
        "printer_item_count": Item.objects.filter(kind=Item.Kind.PRINTER, deleted=False).count(),
    })

@staff_member_required
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def fulfillment_dash(request):    
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
        return redirect("fulfillment_dash")
    
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
            return redirect("fulfillment_dash")
        if prev_status == order.status:
            order.status = prev_status
            messages.error(request, f"Order status is already { {'P': 'pending', 'D': 'denied', 'F': 'fulfilled', 'R': 'refunded'}.get(order.status) }!")
            return redirect("fulfillment_dash")

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
            Order.OrderStatus.REFUNDED: f"Your order for {order.quantity}x {order.item.name} was refunded and {amount_refunded} pearls have been added back to your balance.",
            Order.OrderStatus.PENDING: f"Your order for {order.quantity}x {order.item.name} has been marked as pending again.",
        }
        run_in_background(send_slack_dm, dm_messages[order.status], owner_slack_id)

    messages.success(request, f"Order #{order.id} updated to {order.get_status_display().lower()}.")
    return redirect("fulfillment_dash")

@staff_member_required
@require_POST
@check_perms(["atlantis_site.organizer", "atlantis_site.fulfillment"])
def view_order_address(request, order_id):
    """Fetch and return the buyer's shipping address and contact details.

    The address, full name, email and phone number are pulled live from HCA
    with the buyer's stored token — none of it is stored on our side — and
    access to a customer's plaintext address is audit-logged since it is PII.
    """
    order = get_object_or_404(Order.objects.select_related("owner"), id=order_id)
    profile = getattr(order.owner, "hackclub_profile", None)

    # One userinfo call for all of it: the address the parcel goes to and the
    # name, email and phone to reach the buyer at come out of the same
    # response.
    userinfo = {}
    if profile is not None:
        try:
            userinfo = fetch_userinfo(profile)
        except IdentityUnavailable:
            return JsonResponse({"ok": False, "error": "address_unavailable"}, status=503)

    address = format_address(select_address(extract_addresses(userinfo), order.address_id))

    if address is None:
        return JsonResponse({"ok": False, "error": "no_address"}, status=404)

    name, email = extract_contact(userinfo)
    phone = extract_phone(userinfo, order.address_id)

    record_audit(request, "view_order_address", target=f"Order #{order.id}", metadata={
        "order_id": order.id,
        "owner": order.owner.username,
        "address_id": address.get("id", ""),
    })

    return JsonResponse({
        "ok": True,
        "address": address,
        "contact": {"name": name, "email": email, "phone": phone},
    })


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