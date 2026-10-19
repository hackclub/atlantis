from django.db import migrations, models


# What each desk's snack grant cost, at the thresholds snack_grants.py paid on
# when it was removed. Kept here rather than imported: the module is gone.
SNACK_GRANT_PRICES = {
    "lapse": ("lapse_hours", 100, "100 lapse hours"),
    "t1": ("t1_reviews", 65, "65 T1 reviews"),
    "t2": ("t2_reviews", 25, "25 T2 reviews"),
}


def snack_grants_to_reviewer_orders(apps, schema_editor):
    """Re-price every snack grant order as a reviewer shop purchase.

    The reviewer shop counts work from the same moment snack grants did and
    takes off what has been spent, so a grant already paid out has to read as
    having cost what it took to earn — otherwise everyone would get that work
    back to spend a second time. Each desk's grants move onto a deleted
    reviewer item priced at that desk's old threshold, and whatever progress a
    reviewer had made towards their next grant becomes their balance.
    """
    Item = apps.get_model("atlantis_site", "Item")
    Order = apps.get_model("atlantis_site", "Order")
    SnackGrant = apps.get_model("atlantis_site", "SnackGrant")

    legacy_items = {}
    for desk, (currency, cost, label) in SNACK_GRANT_PRICES.items():
        if not SnackGrant.objects.filter(desk=desk, order__isnull=False).exists():
            continue
        legacy_items[desk] = Item.objects.create(
            name="$10 Snack Grant",
            description=f"A $10 snack grant, earned for {label}.",
            cost=cost,
            category="Reviewer shop",
            kind="reviewer",
            review_currency=currency,
            stock=-1,
            deleted=True,
        )

    for grant in SnackGrant.objects.filter(order__isnull=False):
        _currency, cost, _label = SNACK_GRANT_PRICES[grant.desk]
        Order.objects.filter(pk=grant.order_id).update(
            item=legacy_items[grant.desk], cost=cost, quantity=1,
        )

    # The old shared grant item. Anything still on it isn't tied to a grant
    # and cost nothing, so it stays at zero and spends nothing either way.
    Item.objects.filter(kind="snack_grant").update(
        kind="reviewer", review_currency="t1_reviews", deleted=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('atlantis_site', '0075_metrics_permission'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='review_currency',
            field=models.CharField(blank=True, choices=[('lapse_hours', 'Lapse review hours'), ('t1_reviews', 'T1 reviews'), ('t2_reviews', 'T2 reviews')], default='', max_length=16),
        ),
        migrations.RunPython(snack_grants_to_reviewer_orders, migrations.RunPython.noop),
        migrations.DeleteModel(
            name='SnackGrant',
        ),
        migrations.AlterField(
            model_name='item',
            name='kind',
            field=models.CharField(choices=[('regular', 'Regular item'), ('saver_current', 'Streak saver: this week'), ('saver_past', 'Streak saver: earliest missed week'), ('printer', 'Printer claim (hidden from the shop)'), ('reviewer', 'Reviewer shop item (hidden from the shop)')], default='regular', help_text='Savers are applied on purchase and never reach fulfillment.', max_length=16),
        ),
    ]
