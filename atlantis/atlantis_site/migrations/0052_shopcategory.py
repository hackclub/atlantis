from django.db import migrations, models


def seed_shop_categories(apps, schema_editor):
    """Give every category already in use a slot, keeping today's order.

    The shop sorted shelves alphabetically before this, so seeding in that
    order means nothing moves on the shop page until an admin drags something.
    Deleted items count too: their category comes back the moment an item is
    edited into it, and an unused slot costs nothing.
    """
    Item = apps.get_model("atlantis_site", "Item")
    ShopCategory = apps.get_model("atlantis_site", "ShopCategory")

    names = sorted(set(Item.objects.values_list("category", flat=True)) - {""})
    ShopCategory.objects.bulk_create(
        [ShopCategory(name=name, sort_order=position) for position, name in enumerate(names, start=1)]
    )


class Migration(migrations.Migration):

    dependencies = [
        ('atlantis_site', '0051_t2_payout_multiplier'),
    ]

    operations = [
        migrations.CreateModel(
            name='ShopCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=40, unique=True)),
                ('sort_order', models.PositiveIntegerField(default=0)),
            ],
            options={
                'verbose_name_plural': 'shop categories',
                'ordering': ['sort_order', 'name'],
            },
        ),
        migrations.RunPython(seed_shop_categories, migrations.RunPython.noop),
    ]
