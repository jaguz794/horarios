from decimal import Decimal

from django.db import migrations, models


def seed_contractacion_shift(apps, schema_editor):
    ShiftTemplate = apps.get_model("core", "ShiftTemplate")
    max_order = (
        ShiftTemplate.objects.aggregate(max_order=models.Max("display_order"))["max_order"]
        or 0
    )
    ShiftTemplate.objects.update_or_create(
        label="contratacion",
        defaults={
            "code": "N_CONTRATACION",
            "start_time": None,
            "end_time": None,
            "duration_hours": Decimal("0.00"),
            "night_bonus_hours": Decimal("0.00"),
            "display_order": max_order + 1,
            "counts_as_worked_time": False,
            "is_active": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_systemconfiguration_programming_interval_minutes"),
    ]

    operations = [
        migrations.RunPython(seed_contractacion_shift, migrations.RunPython.noop),
    ]
