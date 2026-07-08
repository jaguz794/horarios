from django.db import migrations, models


def create_personal_vario_site(apps, schema_editor):
    Site = apps.get_model("core", "Site")
    Site.objects.update_or_create(
        code="VARIOS",
        defaults={
            "name": "personal_vario",
            "legacy_name": "",
            "group_code": "",
            "city": "",
            "is_active": True,
            "admin_only": True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0007_operationalstaffcache_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="site",
            name="admin_only",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(create_personal_vario_site, migrations.RunPython.noop),
    ]
