from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atlantis_site', '0070_weekreminder'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='banned',
            field=models.BooleanField(default=False),
        ),
    ]
