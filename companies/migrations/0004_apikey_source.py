"""Cada chave de API fica ligada a uma origem do tipo API (as existentes: origem API por omissão)."""

import django.db.models.deletion
from django.db import migrations, models


def assign_default_sources(apps, schema_editor):
    ApiKey = apps.get_model("companies", "ApiKey")
    DataSource = apps.get_model("sources", "DataSource")
    for key in ApiKey.objects.filter(source__isnull=True):
        source, _ = DataSource.objects.get_or_create(
            company_id=key.company_id, code="API", defaults={"name": "API", "kind": "API"}
        )
        key.source = source
        key.save(update_fields=["source"])


class Migration(migrations.Migration):

    dependencies = [
        ("companies", "0003_move_host_and_agt_settings"),
        ("sources", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="apikey",
            name="source",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE,
                                    related_name="api_keys", to="sources.datasource", verbose_name="origem"),
        ),
        migrations.RunPython(assign_default_sources, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="apikey",
            name="source",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                    related_name="api_keys", to="sources.datasource", verbose_name="origem"),
        ),
    ]
