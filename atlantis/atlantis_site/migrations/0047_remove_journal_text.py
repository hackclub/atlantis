from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("atlantis_site", "0046_project_image_url"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="journal",
            name="text",
        ),
    ]
