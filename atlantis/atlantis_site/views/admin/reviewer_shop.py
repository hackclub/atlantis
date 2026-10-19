from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ... import reviewer_shop
from ...hca import AddressUnavailable
from ...models import Item, Order
from .shop import _item_field_error
from ..helpers import (
    INT_FIELD_MAX, check_perms, field_max_length, is_valid_image_url, rate_limit,
    record_audit, staff_member_required, too_long,
)

# Everyone who reviews anything, plus the shopkeepers who stock the shelf.
REVIEWER_PERMS = [
    "atlantis_site.organizer",
    "atlantis_site.fulfillment",
    "atlantis_site.t1_review",
    "atlantis_site.t2_review",
    "atlantis_site.t3_review",
    "atlantis_site.timelapse_review",
    "atlantis_site.reviewer_lead",
]
SHOPKEEPER_PERMS = ["atlantis_site.organizer", "atlantis_site.fulfillment"]

# Your own reviewer orders listed under the shelf, newest first.
RECENT_ORDERS = 20


def _is_shopkeeper(user):
    return any(user.has_perm(p) for p in SHOPKEEPER_PERMS)


@staff_member_required
@check_perms(REVIEWER_PERMS)
def reviewer_shop_dash(request):
    balances = reviewer_shop.balances(request.user)
    stat_cards = [
        {
            "label": currency.label,
            "value": reviewer_shop.display_amount(currency, balances[currency]),
            "description": "yours to spend",
        }
        for currency in Item.ReviewCurrency
    ]

    items = list(reviewer_shop.items())
    for item in items:
        item.price = item.price_label(item.cost)
        item.affordable = balances[item.review_currency] >= reviewer_shop.price(item)

    orders = list(
        Order.objects.filter(owner=request.user, item__kind=Item.Kind.REVIEWER)
        .select_related("item")
        .order_by("-created_at")[:RECENT_ORDERS]
    )
    for order in orders:
        order.total_label = order.item.price_label(order.cost * order.quantity)

    return render(request, "root/reviewer_shop.html", {
        "stat_cards": stat_cards,
        "items": items,
        "orders": orders,
        "is_shopkeeper": _is_shopkeeper(request.user),
        "currencies": Item.ReviewCurrency.choices,
        "queue_label": "Reviewer shop",
        "queue_short": "Shop",
        "queue_accent": "good",
    })


@staff_member_required
@require_POST
@check_perms(REVIEWER_PERMS)
@rate_limit("buy_reviewer_item", 2)
def buy_reviewer_item(request, item_id):
    item = get_object_or_404(reviewer_shop.items(), id=item_id)
    quantity = request.POST.get("quantity", "").strip() or "1"
    user_notes = request.POST.get("user_notes", "").strip()

    try:
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        messages.error(request, "Quantity must be a positive number.")
        return redirect("reviewer_shop_dash")
    if quantity > INT_FIELD_MAX:
        messages.error(request, "That's more than anyone can order at once.")
        return redirect("reviewer_shop_dash")

    if too_long(user_notes, Order, "user_notes"):
        messages.error(request, f"Order notes too long (max {field_max_length(Order, 'user_notes')} chars).")
        return redirect("reviewer_shop_dash")

    # Looked up before any lock is taken, as the main shop does: it calls out
    # to HCA, and fulfillment falls back to the primary address without it.
    profile = getattr(request.user, "hackclub_profile", None)
    try:
        address_id = profile.primary_address_id if profile else ""
    except AddressUnavailable:
        address_id = ""
    if too_long(address_id, Order, "address_id"):
        address_id = ""

    with transaction.atomic():
        # The reviewer's row is the lock on their balance: two purchases at
        # once would otherwise both see the same work as unspent.
        get_user_model().objects.select_for_update().filter(pk=request.user.pk).first()
        item = Item.objects.select_for_update().get(id=item.id)

        if not item.unlimited_stock and quantity > max(item.stock, 0):
            messages.error(
                request,
                "This item is out of stock." if item.stock <= 0
                else f"Only {item.stock} of this item {'is' if item.stock == 1 else 'are'} left in stock.",
            )
            return redirect("reviewer_shop_dash")

        balance = reviewer_shop.balances(request.user)[item.review_currency]
        if balance < reviewer_shop.price(item, quantity):
            messages.error(
                request,
                f"{quantity}x {item.name} costs {item.price_label(item.cost * quantity)} and you have "
                f"{reviewer_shop.display_amount(item.review_currency, balance)} to spend.",
            )
            return redirect("reviewer_shop_dash")

        if not item.unlimited_stock:
            item.stock -= quantity
            item.save(update_fields=["stock"])

        order = Order.objects.create(
            owner=request.user,
            item=item,
            quantity=quantity,
            user_notes=user_notes,
            address_id=address_id,
        )

    record_audit(request, "buy_reviewer_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "order_id": order.id,
        "quantity": quantity,
        "currency": item.review_currency,
        "cost": item.cost * quantity,
    })
    messages.success(request, f"Ordered {quantity}x {item.name}! It's in the fulfillment queue.")
    return redirect("reviewer_shop_dash")


def _read_item_form(request):
    """The item fields posted by the shopkeeper's form, or an error."""
    fields = {
        "name": request.POST.get("name", "").strip(),
        "description": request.POST.get("description", "").strip(),
        "imageUrl": request.POST.get("imageUrl", "").strip(),
        "review_currency": request.POST.get("review_currency", "").strip(),
    }
    cost = request.POST.get("cost", "").strip()
    stock = request.POST.get("stock", "").strip() or "-1"

    if not fields["name"]:
        return None, "Name is required."
    if not fields["description"]:
        return None, "Description is required."
    if fields["review_currency"] not in Item.ReviewCurrency.values:
        return None, "Pick what the item costs: lapse hours, T1 reviews or T2 reviews."
    try:
        fields["cost"] = int(cost)
    except ValueError:
        return None, "Cost must be a whole number."
    if fields["cost"] <= 0:
        return None, "Cost must be at least 1."
    try:
        fields["stock"] = int(stock)
    except ValueError:
        return None, "Stock must be a whole number (-1 for unlimited)."
    if fields["stock"] < -1:
        return None, "Stock must be -1 (unlimited) or a non-negative number."

    # A picture is optional here: a lot of what reviewers get (grants, say)
    # has nothing to show. Without one the row keeps the placeholder.
    if fields["imageUrl"]:
        if not is_valid_image_url(fields["imageUrl"]):
            return None, "Image URL must be a valid http or https URL."
    else:
        fields["imageUrl"] = Item._meta.get_field("imageUrl").default

    error = _item_field_error(
        fields["name"], fields["description"], reviewer_shop.ITEM_CATEGORY,
        fields["imageUrl"], fields["cost"], fields["stock"],
    )
    if error:
        return None, error
    return fields, None


@staff_member_required
@require_POST
@check_perms(SHOPKEEPER_PERMS)
def create_reviewer_item(request):
    fields, error = _read_item_form(request)
    if error:
        messages.error(request, error)
        return redirect("reviewer_shop_dash")

    item = Item.objects.create(
        **fields,
        kind=Item.Kind.REVIEWER,
        category=reviewer_shop.ITEM_CATEGORY,
    )
    record_audit(request, "create_reviewer_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        **fields,
    })
    messages.success(request, f"Added {item.name} to the reviewer shop.")
    return redirect("reviewer_shop_dash")


@staff_member_required
@require_POST
@check_perms(SHOPKEEPER_PERMS)
def edit_reviewer_item(request, item_id):
    item = get_object_or_404(reviewer_shop.items(), id=item_id)
    fields, error = _read_item_form(request)
    if error:
        messages.error(request, error)
        return redirect("reviewer_shop_dash")

    previous = {field: getattr(item, field) for field in fields}
    for field, value in fields.items():
        setattr(item, field, value)
    item.save(update_fields=list(fields))

    record_audit(request, "edit_reviewer_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "previous": previous,
        "new": fields,
    })
    messages.success(request, f"Saved {item.name}.")
    return redirect("reviewer_shop_dash")


@staff_member_required
@require_POST
@check_perms(SHOPKEEPER_PERMS)
def delete_reviewer_item(request, item_id):
    item = get_object_or_404(reviewer_shop.items(), id=item_id)
    item.deleted = True
    item.save(update_fields=["deleted"])

    record_audit(request, "delete_reviewer_item", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "name": item.name,
    })
    messages.success(request, f"Removed {item.name} from the reviewer shop.")
    return redirect("reviewer_shop_dash")
