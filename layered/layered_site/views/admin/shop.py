from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from django.shortcuts import get_object_or_404

from ...models import Profile, Item, Order
from ..helpers import check_perms, record_audit, send_slack_dm, is_valid_image_url

@staff_member_required
@check_perms(["layered_site.organizer", "layered_site.fulfillment"])
def shop_dash(request):
    items = Item.objects.order_by("id")
    categories = (
        Item.objects.filter(deleted=False)
        .order_by("category")
        .values_list("category", flat=True)
        .distinct()
    )
    return render(request, "root/shop.html", {"items": items, "categories": categories})

@staff_member_required
@check_perms(["layered_site.organizer", "layered_site.fulfillment"])
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
@check_perms(["layered_site.organizer", "layered_site.fulfillment"])
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
    
    with transaction.atomic():
        order = Order.objects.select_for_update().get(id=order_id)
        profile = Profile.objects.select_for_update().get(user=order.owner)

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

        if order.status == Order.OrderStatus.REFUNDED:
            amount_refunded = order.cost * order.quantity
            profile.layers += amount_refunded
            profile.save()
            order.refunded = True
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
    })

    owner_slack_id = order.owner.hackclub_profile.slack_id
    if owner_slack_id:
        dm_messages = {
            Order.OrderStatus.FULFILLED: f"Your order for {order.quantity}x {order.item.name} has been fulfilled!",
            Order.OrderStatus.DENIED: f"Your order for {order.quantity}x {order.item.name} was denied. Ask in #layered-help for more details.",
            Order.OrderStatus.REFUNDED: f"Your order for {order.quantity}x {order.item.name} was refunded and {amount_refunded} spools have been added back to your balance.",
            Order.OrderStatus.PENDING: f"Your order for {order.quantity}x {order.item.name} has been marked as pending again.",
        }
        send_slack_dm(dm_messages[order.status], owner_slack_id)

    messages.success(request, f"Order #{order.id} updated to {order.get_status_display().lower()}.")
    return redirect("fulfillment_dash")

@staff_member_required
@require_POST
@check_perms(["layered_site.organizer", "layered_site.fulfillment"])
def create_item(request):
    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    cost = request.POST.get("cost", "").strip()
    imageUrl = request.POST.get("imageUrl", "").strip()
    category = request.POST.get("category", "").strip() or "Other"

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

    is_print_reward = request.POST.get("is_print_reward") == "on"

    item = Item.objects.create(
        name = name,
        description = description,
        cost = cost,
        imageUrl = imageUrl,
        category = category,
        is_print_reward = is_print_reward,
        stock = stock,
    )

    if is_print_reward:
        Item.objects.exclude(id=item.id).update(is_print_reward=False)

    record_audit(request, "create_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "name": item.name,
        "cost": item.cost,
        "category": item.category,
        "is_print_reward": is_print_reward,
        "stock": item.stock,
    })

    return redirect("shop_dash")

@staff_member_required
@require_POST
@check_perms(["layered_site.organizer", "layered_site.fulfillment"])
def edit_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()
    cost = request.POST.get("cost", "").strip()
    imageUrl = request.POST.get("imageUrl", "").strip()
    category = request.POST.get("category", "").strip() or "Other"

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

    is_print_reward = request.POST.get("is_print_reward") == "on"

    previous = {
        "name": item.name,
        "description": item.description,
        "cost": item.cost,
        "imageUrl": item.imageUrl,
        "category": item.category,
        "is_print_reward": item.is_print_reward,
        "stock": item.stock,
    }

    item.name = name
    item.description = description
    item.cost = cost
    item.imageUrl = imageUrl
    item.category = category
    item.is_print_reward = is_print_reward
    item.stock = stock
    item.save()

    if is_print_reward:
        Item.objects.exclude(id=item.id).update(is_print_reward=False)

    record_audit(request, "edit_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "previous": previous,
        "new": {"name": name, "description": description, "cost": cost, "imageUrl": imageUrl, "category": category, "is_print_reward": is_print_reward, "stock": stock},
    })

    return redirect("shop_dash")

@staff_member_required
@require_POST
@check_perms(["layered_site.organizer", "layered_site.fulfillment"])
def delete_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    item.deleted = True
    item.save()

    record_audit(request, "delete_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "name": item.name,
    })

    return redirect("shop_dash")