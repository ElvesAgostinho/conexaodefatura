"""Sincroniza as origens do tipo base de dados (todas as ativas, ou uma empresa/origem).

Uso:  python manage.py sync_sources
      python manage.py sync_sources --company 5000000000 --source HOST
Pensado para correr periodicamente (Agendador de Tarefas do Windows, cron, ...).
"""

from django.core.management.base import BaseCommand, CommandError

from sources.models import DataSource
from sources.sync import SyncError, sync_source


class Command(BaseCommand):
    help = "Importa documentos novos das origens do tipo base de dados."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="NIF da empresa.")
        parser.add_argument("--source", help="Código da origem (com --company).")

    def handle(self, *args, **options):
        if options["source"] and not options["company"]:
            raise CommandError("--source exige --company.")
        sources = DataSource.objects.filter(kind=DataSource.Kind.DATABASE, active=True, company__active=True)
        if options["company"]:
            sources = sources.filter(company__nif=options["company"])
        if options["source"]:
            sources = sources.filter(code=options["source"])
        sources = list(sources.select_related("company"))
        if not sources:
            raise CommandError("Nenhuma origem ativa do tipo base de dados corresponde ao pedido.")

        failures = 0
        for source in sources:
            label = f"{source.company.nif} {source.code}"
            try:
                result = sync_source(source)
            except SyncError as exc:
                failures += 1
                self.stderr.write(self.style.ERROR(f"{label}: {exc}"))
                continue
            style = self.style.SUCCESS if not result.stop_reason and not result.conflicts else self.style.WARNING
            self.stdout.write(style(f"{label}: {result.summary()}"))
            for conflict in result.conflicts:
                self.stdout.write(self.style.WARNING(f"  conflito {conflict}"))
        if failures == len(sources):
            raise CommandError("Todas as sincronizações falharam.")
