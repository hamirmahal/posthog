# Generated by Django 4.2.15 on 2024-10-16 15:06

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0489_alter_integration_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="dashboard",
            name="variables",
            field=models.JSONField(blank=True, default=dict, null=True),
        ),
    ]
