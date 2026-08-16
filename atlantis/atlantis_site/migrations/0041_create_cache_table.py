from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    # Creates the table backing the "default" DatabaseCache (see CACHES in
    # settings). createcachetable is idempotent — it skips tables that already
    # exist — so this is safe to (re-)run on every deploy's `migrate`.
    call_command("createcachetable", "atlantis_cache")


class Migration(migrations.Migration):

    dependencies = [
        ("atlantis_site", "0040_lookoutsession"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
