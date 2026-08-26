from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from ...models import Profile, Item, Order, ShopCategory
from ...crypto import format_address
from ...hca import AddressUnavailable
from ..helpers import rate_limit

@login_required
def shop(request):
    profile = request.user.hackclub_profile
    items = ShopCategory.order_items(Item.objects.filter(deleted=False))
    return render(request, "atlantis_site/shop.html", {"items": items, 'profile': profile})


@login_required
def item_detail(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    profile = request.user.hackclub_profile

    return render(request, "atlantis_site/item_detail.html", {
        "item": item,
        "profile": profile,
    })

@login_required
def order_page(request, item_id):
    return redirect("item_detail", item_id=item_id)

@login_required
@rate_limit("order_item", 2)
def order_item(request, item_id):
    # Ordering happens in the shop's pop-up, so every way this can go wrong
    # sends you back to the shelf with the message rather than to a page you
    # never meant to be on.
    if request.method != "POST":
        return redirect("shop")

    item = get_object_or_404(Item, id=item_id)
    quantity = request.POST.get("quantity", "").strip()
    user_notes = request.POST.get("user_notes", "").strip()

    if not quantity:
        messages.error(request, "Quantity is required.")
        return redirect("shop")
    
    try:
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        messages.error(request, "Quantity must be a positive number.")
        return redirect("shop")
    
    total_cost = item.cost * quantity

    # Resolved before the transaction: this calls out to HCA, which has no
    # business happening while row locks are held. An order still goes through
    # if the lookup fails — fulfillment resolves the primary address anyway.
    try:
        address_id = request.user.hackclub_profile.primary_address_id
    except AddressUnavailable:
        address_id = ""

    with transaction.atomic():
        item = Item.objects.select_for_update().get(id=item.id)
        profile = Profile.objects.select_for_update().get(user=request.user)

        if not item.unlimited_stock and item.stock <= 0:
            messages.error(request, "This item is out of stock.")
            return redirect("shop")

        if not item.unlimited_stock and quantity > item.stock:
            messages.error(
                request,
                f"Only {item.stock} of this item {'is' if item.stock == 1 else 'are'} left in stock."
            )
            return redirect("shop")

        if profile.layers < total_cost:
            messages.error(
                request,
                "You do not have enough layers to purchase this item."
            )
            return redirect("shop")

        profile.layers -= total_cost
        profile.save()

        if not item.unlimited_stock:
            item.stock -= quantity
            item.save(update_fields=["stock"])

        Order.objects.create(
            owner=request.user,
            item=item,
            quantity=quantity,
            user_notes=user_notes,
            address_id=address_id,
        )

    messages.success(request, f"Successfully ordered {quantity}x {item.name}!")
    return redirect("shop")


@login_required
@require_POST
@rate_limit("view_own_address", 2, json=True)
def view_own_address(request):
    profile = request.user.hackclub_profile

    try:
        address = format_address(profile.get_address())
    except AddressUnavailable:
        return JsonResponse(
            {"ok": False, "error": "address_unavailable"}, status=503
        )

    if address is None:
        return JsonResponse(
            {"ok": False, "error": "no_address"}, status=404
        )

    return JsonResponse({"ok": True, "address": address})
