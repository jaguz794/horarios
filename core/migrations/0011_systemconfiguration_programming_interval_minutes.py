from django.db import migrations, models
from django.core.validators import MaxValueValidator, MinValueValidator


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_jobrole_base_work_days_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemconfiguration",
            name="programming_interval_minutes",
            field=models.PositiveSmallIntegerField(
                default=30,
                validators=[MinValueValidator(1), MaxValueValidator(240)],
            ),
        ),
    ]
