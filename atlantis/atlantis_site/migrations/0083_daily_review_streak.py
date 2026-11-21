import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('atlantis_site', '0082_finalweekextension'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='timezone',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.CreateModel(
            name='ReviewStreakReminder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day', models.DateField()),
                ('sent_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='review_streak_reminders', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-day'],
                'constraints': [models.UniqueConstraint(fields=('user', 'day'), name='one_streak_reminder_per_user_day')],
            },
        ),
    ]
