from django.db import migrations, models


CURRENCIES = ("lapse_hours", "t1_reviews", "t2_reviews")


def single_to_per_currency(apps, schema_editor):
    """Move each reviewer item's one price onto its currency's own column.

    The currency an order was paid in used to be read off its item. An item
    can now take any of the three, so every existing reviewer order is
    stamped with the one its item was priced in when it was bought.
    """
    Item = apps.get_model("atlantis_site", "Item")
    Order = apps.get_model("atlantis_site", "Order")

    for item in Item.objects.filter(kind="reviewer"):
        if item.review_currency in CURRENCIES:
            setattr(item, f"{item.review_currency}_cost", item.cost)
            Order.objects.filter(item=item).update(review_currency=item.review_currency)
        item.cost = 0
        item.save()


def per_currency_to_single(apps, schema_editor):
    """Back to one price: whichever currency the item was first priced in."""
    Item = apps.get_model("atlantis_site", "Item")

    # Orders paid in another currency keep their cost, which the old schema
    # reads in the item's; it has no way to say otherwise.
    for item in Item.objects.filter(kind="reviewer"):
        for currency in CURRENCIES:
            cost = getattr(item, f"{currency}_cost")
            if cost is not None:
                item.review_currency, item.cost = currency, cost
                break
        item.save()


class Migration(migrations.Migration):

    dependencies = [
        ('atlantis_site', '0076_reviewer_shop'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='lapse_hours_cost',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='item',
            name='t1_reviews_cost',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='item',
            name='t2_reviews_cost',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='review_currency',
            field=models.CharField(blank=True, choices=[('lapse_hours', 'Lapse review hours'), ('t1_reviews', 'T1 reviews'), ('t2_reviews', 'T2 reviews')], default='', max_length=16),
        ),
        migrations.RunPython(single_to_per_currency, per_currency_to_single),
        migrations.RemoveField(
            model_name='item',
            name='review_currency',
        ),
    ]
