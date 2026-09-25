from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atlantis_site', '0068_t1_request_changes'),
    ]

    operations = [
        migrations.CreateModel(
            name='MetricsSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day', models.DateField(unique=True)),
                ('taken_at', models.DateTimeField()),
                ('data', models.JSONField(default=dict)),
            ],
            options={
                'ordering': ['-day'],
            },
        ),
    ]
