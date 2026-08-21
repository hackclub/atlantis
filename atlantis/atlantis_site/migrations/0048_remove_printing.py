from django.db import migrations, models


def advance_printing_ships(apps, schema_editor):
    """Move ships out of the two print statuses that no longer exist.

    Both mean T1 already approved the ship, so T2 is where they belong now.
    Leaving them on 'PQ'/'BP' would strand them: no queue lists those statuses
    any more and every decision view refuses a ship it can't place.
    """
    Ship = apps.get_model("atlantis_site", "Ship")
    Ship.objects.filter(status__in=("PQ", "BP")).update(status="T2")


class Migration(migrations.Migration):

    dependencies = [
        ('atlantis_site', '0047_remove_journal_text'),
    ]

    operations = [
        # Historical T2/T3 decisions of 'P' (returned to printers) are left
        # alone: they record what a reviewer actually did, and choices are only
        # validated on write, so old rows keep loading fine.
        migrations.RunPython(advance_printing_ships, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='permissions',
            options={'permissions': [('t1_review', 'T1 Project Review'), ('t2_review', 'T2 Project Review'), ('t3_review', 'T3/Fraud Project Review'), ('fulfillment', 'Fulfill shop orders'), ('organizer', 'Access to everything')], 'verbose_name': 'Permission', 'verbose_name_plural': 'Permissions'},
        ),
        migrations.RemoveField(
            model_name='item',
            name='is_print_reward',
        ),
        migrations.RemoveField(
            model_name='profile',
            name='print_reward_kg',
        ),
        migrations.RemoveField(
            model_name='t1',
            name='print',
        ),
        migrations.AlterField(
            model_name='ship',
            name='status',
            field=models.CharField(choices=[('R', 'Rejected'), ('T1', 'Under T1 Review'), ('T2', 'Under T2 Review'), ('T3', 'Under fraud review'), ('F', 'Finalized')], default='T1', max_length=2),
        ),
        migrations.AlterField(
            model_name='t2',
            name='decision',
            field=models.CharField(choices=[('T1', 'Returned to T1 Review'), ('A', 'Approved')], default='A', max_length=2),
        ),
        migrations.AlterField(
            model_name='t3',
            name='decision',
            field=models.CharField(choices=[('T1', 'Returned to T1 Review'), ('T2', 'Returned to T2 Review'), ('A', 'Approved')], default='A', max_length=2),
        ),
        migrations.DeleteModel(
            name='Print',
        ),
    ]
