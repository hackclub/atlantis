from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ... import challenge, weeks
from ...challenge import SaverError
from ...models import Profile, Item, Order, ShopCategory
from ...crypto import format_address
from ...hca import AddressUnavailable
from ..helpers import (
    INT_FIELD_MAX, field_max_length, rate_limit, record_audit, send_slack_dm,
    too_long,
)


def shoppable():
    """Items a shopper can actually see on a shelf.

    Printer rows are Items only so that claiming one becomes an ordinary order
    that fulfillment already knows how to post; they are not merchandise and
    have no business on the shop page.
    """
    return Item.objects.filter(deleted=False).exclude(kind=Item.Kind.PRINTER)


# What every shelf but the restore says to someone who's out.
OUT_REASON = (
    "You're out of the program, so the shop is locked until you're back in. "
    "Buy missed-week streak savers to get back in."
)


def _out_blocks(item, eliminated):
    """Whether being out puts this item off limits.

    Someone who's out can spend pearls on exactly one thing: getting back in.
    Even the this-week saver waits, since it can't undo a week already missed.
    """
    return eliminated and item.kind != Item.Kind.SAVER_PAST


def _saver_state(user, item, now=None, eliminated=None):
    """(why it can't be bought, which week it would land on) for an item.

    Both empty for a regular item anyone can buy. Not a security check —
    order_item re-asks before taking anyone's pearls. This is so the shelf can
    grey an item out and name the week instead of letting someone find out by
    spending a click, or a pearl.
    """
    if eliminated is None:
        eliminated = challenge.standing(user, now).eliminated
    # A saver with nowhere to land says so first; that's the more specific
    # sentence, and stays true whether or not you're out.
    if item.is_saver:
        try:
            index = challenge.target_week(user, item.kind, now)
        except SaverError as err:
            return str(err), ""
    if _out_blocks(item, eliminated):
        return OUT_REASON, ""
    if not item.is_saver:
        return "", ""
    return "", weeks.week_label(index)


def _decorate(user, items, now=None):
    eliminated = challenge.standing(user, now).eliminated
    for item in items:
        item.blocked_reason, item.saver_target = _saver_state(user, item, now, eliminated)
    return items


@login_required
def shop(request):
    profile = request.user.hackclub_profile
    items = _decorate(request.user, list(ShopCategory.order_items(shoppable())))
    return render(request, "atlantis_site/shop.html", {
        "items": items,
        "profile": profile,
        "standing": challenge.standing(request.user),
    })


@login_required
def item_detail(request, item_id):
    item = get_object_or_404(shoppable(), id=item_id)
    profile = request.user.hackclub_profile
    blocked_reason, saver_target = _saver_state(request.user, item)

    return render(request, "atlantis_site/item_detail.html", {
        "item": item,
        "profile": profile,
        "blocked_reason": blocked_reason,
        "saver_target": saver_target,
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

    item = get_object_or_404(shoppable(), id=item_id)
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

    # Stock bounds this for anything limited, but an unlimited item has no
    # ceiling but the column's, and a quantity past it is a DataError out of
    # the driver rather than a sentence about the order.
    if quantity > INT_FIELD_MAX:
        messages.error(request, "That's more than anyone can order at once.")
        return redirect("shop")

    if too_long(user_notes, Order, "user_notes"):
        messages.error(request, f"Order notes too long (max {field_max_length(Order, 'user_notes')} chars).")
        return redirect("shop")

    blocked_reason, _ = _saver_state(request.user, item)
    if blocked_reason == OUT_REASON:
        messages.error(request, OUT_REASON)
        return redirect("shop")

    total_cost = item.cost * quantity

    if item.is_saver:
        return _order_saver(request, item, quantity, total_cost, user_notes)

    # Resolved before the transaction: this calls out to HCA, which has no
    # business happening while row locks are held. An order still goes through
    # if the lookup fails — fulfillment resolves the primary address anyway.
    try:
        address_id = request.user.hackclub_profile.primary_address_id
    except AddressUnavailable:
        address_id = ""

    # An id wider than the column is the same situation as not getting one at
    # all — fulfillment resolves the primary address either way — and a stored
    # prefix would be an id that resolves to nothing.
    if too_long(address_id, Order, "address_id"):
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


def _order_saver(request, item, quantity, total_cost, user_notes):
    """Buy streak savers, which are spent the moment they are paid for.

    Savers are not posted to anyone, so there is nothing for fulfillment to do
    and nothing to hold in an inventory: the hours land on a week here, inside
    the same transaction that takes the pearls. An Order is still written —
    marked fulfilled on the spot — because that is where the shop's audit
    trail, refunds and spend metrics all live, and a purchase that skipped it
    would be invisible to every one of them.
    """
    with transaction.atomic():
        item = Item.objects.select_for_update().get(id=item.id)
        profile = Profile.objects.select_for_update().get(user=request.user)

        if not item.unlimited_stock and quantity > max(item.stock, 0):
            messages.error(
                request,
                "This item is out of stock." if item.stock <= 0
                else f"Only {item.stock} of this item {'is' if item.stock == 1 else 'are'} left in stock.",
            )
            return redirect("shop")

        if profile.layers < total_cost:
            messages.error(request, "You do not have enough layers to purchase this item.")
            return redirect("shop")

        # Asked again inside the lock. The shelf greys out a saver with nothing
        # to apply to, but a week can close (or a rescue can land from another
        # tab) between the page rendering and this POST, and paying for an hour
        # that has nowhere to go would be taking pearls for nothing.
        try:
            challenge.target_week(request.user, item.kind)
        except SaverError as err:
            messages.error(request, str(err))
            return redirect("shop")

        # Buying more missed-week savers than there are missed hours would run
        # out of weeks part way through and roll the whole order back. Saying
        # the number up front is friendlier than refusing the lot afterwards.
        if item.kind == Item.Kind.SAVER_PAST:
            outstanding = challenge.standing(request.user).saver_hours_outstanding
            if quantity > outstanding:
                messages.error(
                    request,
                    f"You only need {outstanding} more saver hour"
                    f"{'s' if outstanding != 1 else ''} to clear every week you've "
                    "missed, so that many is all you can buy.",
                )
                return redirect("shop")

        profile.layers -= total_cost
        profile.save(update_fields=["layers"])

        if not item.unlimited_stock:
            item.stock -= quantity
            item.save(update_fields=["stock"])

        order = Order.objects.create(
            owner=request.user,
            item=item,
            quantity=quantity,
            user_notes=user_notes,
            status=Order.OrderStatus.FULFILLED,
            fulfilled_at=timezone.now(),
        )

        try:
            applied = challenge.apply_saver(
                request.user, item.kind, hours=quantity, order=order
            )
        except SaverError as err:
            # Nothing has been posted and nothing left the site, so the whole
            # purchase comes back rather than leaving paid-for hours unapplied.
            transaction.set_rollback(True)
            messages.error(request, str(err))
            return redirect("shop")

    weeks_touched = sorted(set(applied))
    named = ", ".join(f"week {index}" for index in weeks_touched)
    record_audit(request, "buy_streak_saver", target=f"Item #{item.id} ({item.name})", metadata={
        "item_id": item.id,
        "kind": item.kind,
        "order_id": order.id,
        "hours": quantity,
        "weeks": weeks_touched,
        "cost": total_cost,
    })

    state = challenge.standing(request.user)
    if state.eliminated:
        remaining = state.earliest_missed
        messages.success(
            request,
            f"{quantity} saver hour{'s' if quantity != 1 else ''} applied to {named}. "
            f"You're still out until {remaining.label} is covered: "
            f"{remaining.shortfall_hours} more to go.",
        )
    else:
        messages.success(
            request,
            f"{quantity} saver hour{'s' if quantity != 1 else ''} applied to {named}. "
            "Your streak is safe.",
        )

    profile = request.user.hackclub_profile
    if profile.slack_id:
        send_slack_dm(
            f"{quantity} streak saver hour{'s' if quantity != 1 else ''} applied to "
            f"{named}. " + (
                "You're back in the program!" if not state.eliminated
                else f"You still need {state.saver_hours_needed} more to get back in."
            ),
            profile.slack_id,
        )

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
