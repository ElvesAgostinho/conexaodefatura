"""Fase 4: descobre e grava a estrutura real da BD do HOST.

Uso:
    python manage.py host_inspect
    python manage.py host_inspect --schema dbo --schema hotel
    python manage.py host_inspect --no-row-counts --output C:/relatorios
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError

from host_connector.inspection import SchemaInspector
from host_connector.reports import ROLE_LABELS, write_report

from ._base import HostCommand


class Command(HostCommand):
    help = "Inspeciona a estrutura da BD do HOST (só metadados) e grava relatório JSON + Markdown."

    def add_arguments(self, parser):
        parser.add_argument("--schema", action="append", dest="schemas", help="Schema a inspecionar (repetível).")
        parser.add_argument("--no-row-counts", action="store_true", help="Não obter a estimativa de linhas.")
        parser.add_argument("--output", type=Path, default=None, help="Pasta de saída (por omissão HOST_REPORTS_DIR).")

    def handle(self, *args, **options):
        db = self.get_host_database()
        self.print_config(db)
        self.stdout.write("")

        try:
            result = db.test_connection()
            if not result.ok:
                self.stdout.write(self.style.ERROR("ERRO DE CONEXÃO"))
                raise CommandError(result.message)

            self.stdout.write("A ler a estrutura da base de dados (só metadados)...")
            report = SchemaInspector(db).run(options["schemas"], row_counts=not options["no_row_counts"])
        finally:
            db.dispose()

        json_path, md_path = write_report(report, options["output"] or settings.HOST_REPORTS_DIR)

        summary = report["summary"]
        self.stdout.write(self.style.SUCCESS(
            f"Encontrados {summary['schemas']} schema(s), {summary['tables']} tabela(s) e {summary['views']} vista(s)."
        ))
        if report["databases"]:
            self.stdout.write(f"Bases de dados no servidor: {', '.join(report['databases'])}")

        self.stdout.write("")
        self.stdout.write("Candidatos por nome (SUGESTÃO, confirmar manualmente):")
        for role, items in report["candidates"].items():
            names = ", ".join(item["object"] for item in items[:3]) or "-"
            self.stdout.write(f"  {ROLE_LABELS[role]:<30} {names}")

        for warning in report["warnings"]:
            self.stdout.write(self.style.WARNING(f"Aviso: {warning}"))

        self.stdout.write("")
        self.stdout.write(f"Relatório JSON:     {json_path}")
        self.stdout.write(f"Relatório Markdown: {md_path}")
