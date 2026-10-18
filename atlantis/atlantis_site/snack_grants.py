"""$10 snack grants for reviewers, paid out as fulfillment orders.

Every 100 hours of lapse footage reviewed, every 65 T1 reviews and every 25
T2 reviews earns one grant. The desks are counted separately — 50 hours of
lapses and 40 T1s is not a grant yet — and a grant becomes an ordinary
pending Order for a hidden zero-pearl Item, so fulfillment sends it out the
way it sends everything else.

Only reviews written after this shipped count. "Shipped" is the moment the
migration that introduced grants was applied, read back from Django's own
migration table, so nothing has to be kept in step with a deploy by hand.
"""

import logging
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Sum

from .models import Item, Order, SnackGrant, T1, T2, Timelapse
from .views.helpers import run_in_background, send_slack_dm

logger = logging.getLogger(__name__)

LAUNCH_MIGRATION = ("atlantis_site", "0074_snack_grants")

GRANT_ITEM_NAME = "$10 Snack Grant"
GRANT_ITEM_CATEGORY = "Reviewer grants"

LAPSE_SECONDS_PER_GRANT = 100 * 60 * 60
T1_REVIEWS_PER_GRANT = 65
T2_REVIEWS_PER_GRANT = 25

GRANT_AMOUNT_CENTS = 1000
HCB_CARD_GRANT_URL = "https://hcb.hackclub.com/ysws-atlantis/card-grants/new"

_launched_at = None


def launched_at():
    """When grants went live here, or None if the migration hasn't run."""
    global _launched_at
    if _launched_at is None:
        app, name = LAUNCH_MIGRATION
        _launched_at = (
            MigrationRecorder.Migration.objects.filter(app=app, name=name)
            .values_list("applied", flat=True)
            .first()
        )
    return _launched_at


def grant_item():
    """The hidden Item every grant order points at, made on first use."""
    item = Item.objects.filter(kind=Item.Kind.SNACK_GRANT, deleted=False).order_by("id").first()
    if item is None:
        item = Item.objects.create(
            name=GRANT_ITEM_NAME,
            description="A $10 snack grant, earned by reviewing.",
            cost=0,
            category=GRANT_ITEM_CATEGORY,
            kind=Item.Kind.SNACK_GRANT,
            stock=-1,
        )
    return item


def hcb_grant_url(order):
    """HCB's new-card-grant form, prefilled to pay out this grant order.

    None when the reviewer has no email on file, since HCB sends the grant
    to an email and there is nothing to prefill it with.
    """
    email = order.owner.email
    if not email:
        return None
    return HCB_CARD_GRANT_URL + "?" + urlencode({
        "email": email,
        "amount_cents": GRANT_AMOUNT_CENTS * order.quantity,
        "purpose": "Snack Grant",
        "instructions": "buy snacks!!",
    })


def _earned(reviewer, since):
    """Grants each desk's work since `since` is worth, as {desk: count}."""
    lapse_seconds = Timelapse.objects.filter(
        journal__timelapse_review__reviewer=reviewer,
        journal__timelapse_review__reviewed_at__gte=since,
    ).aggregate(total=Sum("tracked_seconds"))["total"] or 0
    t1s = T1.objects.filter(reviewer=reviewer, reviewed_at__gte=since).count()
    t2s = T2.objects.filter(reviewer=reviewer, reviewed_at__gte=since).count()
    return {
        SnackGrant.Desk.LAPSE: lapse_seconds // LAPSE_SECONDS_PER_GRANT,
        SnackGrant.Desk.T1: t1s // T1_REVIEWS_PER_GRANT,
        SnackGrant.Desk.T2: t2s // T2_REVIEWS_PER_GRANT,
    }


ORDER_NOTES = {
    SnackGrant.Desk.LAPSE: f"Snack grant: {LAPSE_SECONDS_PER_GRANT // 3600}h of lapses reviewed",
    SnackGrant.Desk.T1: f"Snack grant: {T1_REVIEWS_PER_GRANT} T1 reviews",
    SnackGrant.Desk.T2: f"Snack grant: {T2_REVIEWS_PER_GRANT} T2 reviews",
}


def award_snack_grants(reviewer):
    """Issue every grant `reviewer` has earned and not yet been given.

    Safe to call as often as you like: it only ever tops the ledger up to what
    the work since launch is worth. The reviewer's user row is locked while it
    counts, so two reviews landing at once can't both issue the same grant.
    Returns the new SnackGrant rows.
    """
    since = launched_at()
    if since is None:
        return []

    with transaction.atomic():
        get_user_model().objects.select_for_update().filter(pk=reviewer.pk).first()
        earned = _earned(reviewer, since)
        issued = {desk: 0 for desk in earned}
        for desk in SnackGrant.objects.filter(reviewer=reviewer).values_list("desk", flat=True):
            issued[desk] = issued.get(desk, 0) + 1

        owed = [
            desk
            for desk, count in earned.items()
            for _ in range(count - issued[desk])
        ]
        if not owed:
            return []

        item = grant_item()
        grants = []
        for desk in owed:
            order = Order.objects.create(
                owner=reviewer,
                item=item,
                quantity=1,
                cost=0,
                admin_notes=ORDER_NOTES[desk],
            )
            grants.append(SnackGrant.objects.create(reviewer=reviewer, desk=desk, order=order))

    slack_id = getattr(getattr(reviewer, "hackclub_profile", None), "slack_id", "")
    if slack_id:
        for grant in grants:
            run_in_background(
                send_slack_dm,
                f"Thanks for reviewing! You've earned a $10 snack grant "
                f"({SnackGrant.Desk(grant.desk).label.lower()}). It's on its way!",
                slack_id,
            )
    return grants


def award_snack_grants_quietly(reviewer):
    """award_snack_grants for a view that has already saved its review.

    The review is committed by the time this runs, so a failure here is logged
    rather than turned into an error page for a decision that did go through.
    The next review the reviewer writes tops the ledger up anyway.
    """
    try:
        return award_snack_grants(reviewer)
    except Exception:
        logger.exception("Couldn't award snack grants to user %s", reviewer.pk)
        return []
