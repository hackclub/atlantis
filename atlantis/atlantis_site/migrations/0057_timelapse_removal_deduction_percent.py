from django.db import migrations, models
from django.core.validators import MaxValueValidator, MinValueValidator


class Migration(migrations.Migration):

    dependencies = [
        ("atlantis_site", "0056_profile_ysws_eligible"),
    ]

    operations = [
        migrations.AddField(
            model_name="timelapseremoval",
            name="deduction_percent",
            field=models.PositiveSmallIntegerField(
                default=100,
                validators=[MinValueValidator(0), MaxValueValidator(100)],
            ),
        ),
        migrations.AddConstraint(
            model_name="timelapseremoval",
            constraint=models.CheckConstraint(
                condition=models.Q(deduction_percent__lte=100),
                name="timelapse_removal_deduction_percent_valid",
            ),
        ),
    ]