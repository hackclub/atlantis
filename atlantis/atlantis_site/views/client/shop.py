from django.shortcuts import render, redirect
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction

from ...models import Profile, Item, Order

@login_required
def shop(request):
    profile = request.user.hackclub_profile
    items = Item.objects.filter(deleted=False).order_by("category", "id")
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
        item = Item.objects.select_for_update().get(id=item.id)
        profile = Profile.objects.select_for_update().get(user=request.user)

        if not item.unlimited_stock and item.stock <= 0:
            messages.error(request, "This item is out of stock.")
            return redirect("item_detail", item_id=item_id)

        if not item.unlimited_stock and quantity > item.stock:
            messages.error(
                request,
                f"Only {item.stock} of this item {'is' if item.stock == 1 else 'are'} left in stock."
            )
            return redirect("item_detail", item_id=item_id)

        if profile.layers < total_cost:
            messages.error(
                request,
                "You do not have enough layers to purchase this item."
            )
            return redirect("item_detail", item_id=item_id)

        profile.layers -= total_cost
        profile.save()

        if not item.unlimited_stock:
            item.stock -= quantity
            item.save(update_fields=["stock"])

        Order.objects.create(
            owner=request.user,
            item=item,
            quantity=quantity,
            user_notes=user_notes
        )

    messages.success(request, f"Successfully ordered {quantity}x {item.name}!")
    return redirect("shop")
