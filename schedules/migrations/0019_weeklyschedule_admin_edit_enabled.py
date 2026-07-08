from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("schedules", "0018_scheduleline_day_0_inventory_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="weeklyschedule",
            name="admin_edit_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
