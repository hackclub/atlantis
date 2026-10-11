from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('atlantis_site', '0074_snack_grants'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='permissions',
            options={'permissions': [('t1_review', 'T1 Project Review'), ('t2_review', 'T2 Project Review'), ('t3_review', 'T3/Fraud Project Review'), ('timelapse_review', 'Timelapse Review (internal)'), ('reviewer_lead', 'Reviewer Lead: roll back T1 reviews, audit reviews'), ('fulfillment', 'Fulfill shop orders'), ('metrics', 'View the metrics page'), ('organizer', 'Access to everything')], 'verbose_name': 'Permission', 'verbose_name_plural': 'Permissions'},
        ),
    ]
