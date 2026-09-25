"""Processa a fila de envio à AGT.

Uso:  python manage.py process_agt_queue [--company NIF] [--limit 100]
Pensado para correr periodicamente (Agendador de Tarefas do Windows, cron, ...).
"""

from django.core.management.base import BaseCommand, CommandError

from agt.queue import process_queue, stuck_sending
from companies.models import Company


class Command(BaseCommand):
    help = "Envia à AGT os documentos em fila."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="NIF da empresa.")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        company = None
        if options["company"]:
            company = Company.objects.filter(nif=options["company"]).first()
            if company is None:
                raise CommandError(f"Empresa com NIF {options['company']} não existe.")
        if options["limit"] < 1:
            raise CommandError("--limit tem de ser positivo.")
        stats = process_queue(company=company, limit=options["limit"])
        self.stdout.write(
            f"{stats.processed} processados: {stats.confirmed} validados, {stats.sent} enviados, "
            f"{stats.rejected} rejeitados, {stats.retried} a repetir, {stats.failed} com erro."
        )
        stuck = stuck_sending(company).count()
        if stuck:
            self.stdout.write(self.style.WARNING(
                f"{stuck} documento(s) presos em 'A enviar' (envio interrompido): verificar no painel."))
