from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("schedules", "0017_scheduleline_money_payment_days_used_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="scheduleline",
            name="day_0_inventory",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="scheduleline",
            name="day_1_inventory",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="scheduleline",
            name="day_2_inventory",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="scheduleline",
            name="day_3_inventory",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="scheduleline",
            name="day_4_inventory",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="scheduleline",
            name="day_5_inventory",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="scheduleline",
            name="day_6_inventory",
            field=models.BooleanField(default=False),
        ),
    ]
