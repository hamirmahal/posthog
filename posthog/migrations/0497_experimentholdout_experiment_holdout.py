# Generated by Django 4.2.15 on 2024-10-24 11:57

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    atomic = False  # Added to support concurrent index creation
    dependencies = [
        ("posthog", "0496_team_person_processing_opt_out"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExperimentHoldout",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=400)),
                ("description", models.CharField(blank=True, max_length=400, null=True)),
                ("filters", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="posthog.team")),
            ],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="experiment",
                    name="holdout",
                    field=models.ForeignKey(
                        null=True, on_delete=django.db.models.deletion.SET_NULL, to="posthog.experimentholdout"
                    ),
                ),
            ],
            database_operations=[
                # We add -- existing-table-constraint-ignore to ignore the constraint validation in CI.
                migrations.RunSQL(
                    """
                    ALTER TABLE "posthog_experiment" ADD COLUMN "holdout_id" integer NULL CONSTRAINT "posthog_experiment_holdout_id_ffd173dd_fk_posthog_e" REFERENCES "posthog_experimentholdout"("id") DEFERRABLE INITIALLY DEFERRED; -- existing-table-constraint-ignore
                    SET CONSTRAINTS "posthog_experiment_holdout_id_ffd173dd_fk_posthog_e" IMMEDIATE; -- existing-table-constraint-ignore
                    """,
                    reverse_sql="""
                        ALTER TABLE "posthog_experiment" DROP COLUMN IF EXISTS "holdout_id";
                    """,
                ),
                # We add CONCURRENTLY to the create command
                migrations.RunSQL(
                    """
                    CREATE INDEX CONCURRENTLY "posthog_experiment_holdout_id_ffd173dd_fk_posthog_e" ON "posthog_experiment" ("holdout_id");
                    """,
                    reverse_sql="""
                        DROP INDEX IF EXISTS "posthog_experiment_holdout_id_ffd173dd_fk_posthog_e";
                    """,
                ),
            ],
        ),
    ]