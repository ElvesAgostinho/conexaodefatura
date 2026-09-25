"""Move a ligação HOST e a configuração AGT da Company para os modelos próprios.

  Company.host_connection   -> sources.DataSource (código HOST, tipo DATABASE)
  Company.agt_configuration -> agt.AgtConfiguration (só chaves conhecidas)
"""

from django.db import migrations

AGT_FIELDS = ("environment", "base_url", "software_certificate_number", "software_name",
              "software_version", "client_id", "credentials_prefix", "auto_send")


def forwards(apps, schema_editor):
    Company = apps.get_model("companies", "Company")
    DataSource = apps.get_model("sources", "DataSource")
    AgtConfiguration = apps.get_model("agt", "AgtConfiguration")
    for company in Company.objects.all():
        if company.host_connection:
            DataSource.objects.get_or_create(
                company=company, code="HOST",
                defaults={"name": "HOST", "system": "HOST Hotel Systems", "kind": "DATABASE",
                          "connection": company.host_connection},
            )
        values = {k: v for k, v in (company.agt_configuration or {}).items() if k in AGT_FIELDS}
        AgtConfiguration.objects.get_or_create(company=company, defaults=values)


def backwards(apps, schema_editor):
    Company = apps.get_model("companies", "Company")
    DataSource = apps.get_model("sources", "DataSource")
    for source in DataSource.objects.filter(code="HOST", kind="DATABASE"):
        Company.objects.filter(pk=source.company_id).update(host_connection=source.connection)


class Migration(migrations.Migration):

    dependencies = [
        ("companies", "0002_host_connection_validator"),
        ("sources", "0001_initial"),
        ("agt", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(model_name="company", name="agt_configuration"),
        migrations.RemoveField(model_name="company", name="host_connection"),
    ]
