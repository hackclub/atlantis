"""Balances for the reviewer shop, where review work is the currency.

A reviewer earns in three currencies, one per desk: hours of lapse footage
reviewed, T1 reviews and T2 reviews. Each reviewer shop item is priced in any
of them, and the buyer picks which to pay in. A balance is everything earned
on that desk since launch, less what has been spent on it — orders paid in
that currency, unless refunded.
Nothing is stored: a refund hands the work back simply by no longer counting,
and a rolled-back review comes off the balance the same way.

"Launch" is when snack grants (which this replaced) went live, read back from
Django's own migration table. Grants already paid out were re-priced as
reviewer shop orders by the migration that removed them, so the work they
paid for stays spent and progress towards the next one carries over.
"""

from django.db.migrations.recorder import MigrationRecorder
from django.db.models import F, Sum

from .models import Item, Order, T1, T2, Timelapse

LAUNCH_MIGRATION = ("atlantis_site", "0074_snack_grants")

ITEM_CATEGORY = "Reviewer shop"

Currency = Item.ReviewCurrency

# Lapse balances are kept in seconds, so footage short of a whole hour still
# counts towards the next one; items are priced in hours.
SECONDS_PER_HOUR = 60 * 60
UNIT_SIZE = {
    Currency.LAPSE_HOURS: SECONDS_PER_HOUR,
    Currency.T1_REVIEWS: 1,
    Currency.T2_REVIEWS: 1,
}

_launched_at = None


def launched_at():
    """When reviewer earnings started counting, or None if never."""
    global _launched_at
    if _launched_at is None:
        app, name = LAUNCH_MIGRATION
        _launched_at = (
            MigrationRecorder.Migration.objects.filter(app=app, name=name)
            .values_list("applied", flat=True)
            .first()
        )
    return _launched_at


def items():
    """Reviewer shop items still on sale."""
    return Item.objects.filter(kind=Item.Kind.REVIEWER, deleted=False).order_by("id")


def _earned(reviewer, since):
    """Work done on each desk since `since`, in base units."""
    lapse_seconds = Timelapse.objects.filter(
        journal__timelapse_review__reviewer=reviewer,
        journal__timelapse_review__reviewed_at__gte=since,
    ).aggregate(total=Sum("tracked_seconds"))["total"] or 0
    return {
        Currency.LAPSE_HOURS: lapse_seconds,
        Currency.T1_REVIEWS: T1.objects.filter(reviewer=reviewer, reviewed_at__gte=since).count(),
        Currency.T2_REVIEWS: T2.objects.filter(reviewer=reviewer, reviewed_at__gte=since).count(),
    }


def _spent(reviewer):
    """What reviewer shop orders have cost, per currency, in base units."""
    rows = (
        Order.objects.filter(owner=reviewer, item__kind=Item.Kind.REVIEWER)
        .exclude(status=Order.OrderStatus.REFUNDED)
        .values_list("review_currency")
        .annotate(total=Sum(F("cost") * F("quantity")))
    )
    return {currency: (total or 0) * UNIT_SIZE.get(currency, 1) for currency, total in rows}


def balances(reviewer):
    """What `reviewer` has left to spend, per currency, in base units.

    Can be negative: a review rolled back after it was spent leaves a hole
    that new work fills before anything else can be bought.
    """
    since = launched_at()
    if since is None:
        return {currency: 0 for currency in Currency}
    earned = _earned(reviewer, since)
    spent = _spent(reviewer)
    return {currency: earned[currency] - spent.get(currency, 0) for currency in Currency}


def price(item, currency, quantity=1):
    """What `quantity` of `item` costs paid in `currency`, in its base units.

    None if `item` can't be paid in `currency`.
    """
    cost = item.review_cost(currency)
    if cost is None:
        return None
    return cost * quantity * UNIT_SIZE[currency]


def display_amount(currency, base_units):
    """A balance as people count it: hours to a tenth, reviews whole.

    Lapse hours round down, so a balance never reads as enough for an item
    it can't actually pay for.
    """
    if currency == Currency.LAPSE_HOURS:
        tenths = base_units * 10 // SECONDS_PER_HOUR
        return f"{tenths / 10:.1f}".removesuffix(".0")
    return str(base_units)
