from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atlantis_site', '0083_daily_review_streak'),
    ]

    operations = [
        migrations.AddField(
            model_name='timelapse',
            name='lddb_version',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='timelapse',
            name='lddb_marks',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='timelapse',
            name='lddb_pending',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='timelapse',
            name='lddb_synced_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='timelapse',
            name='lddb_error',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='timelapseremoval',
            name='lddb_mark_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='timelapseremoval',
            name='lddb_mark',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='LddbCursor',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('position', models.BigIntegerField(default=0)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
