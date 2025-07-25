# Generated by Django 4.2.22 on 2025-06-13 18:06

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "0770_alter_hogflow_conversion_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="teamrevenueanalyticsconfig",
            name="filter_test_accounts",
            field=models.BooleanField(default=False),
        ),
        # These hack around the fact Django removes the default for no reason
        # so let's force it back
        migrations.RunSQL(
            sql="ALTER TABLE posthog_teamrevenueanalyticsconfig ALTER COLUMN filter_test_accounts SET DEFAULT false;",
            reverse_sql="",  # noop
        ),
    ]
