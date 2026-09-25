"""Fase 5: testa a ligação à BD do HOST e confirma que o acesso é só de leitura.

Uso:  python manage.py host_check
"""

from django.core.management.base import CommandError

from host_connector.permissions import check_permissions

from ._base import HostCommand


class Command(HostCommand):
    help = "Testa a ligação (somente leitura) à base de dados do HOST."

    def handle(self, *args, **options):
        db = self.get_host_database()
        self.print_config(db)
        self.stdout.write("")

        try:
            result = db.test_connection()
            if not result.ok:
                self.stdout.write(self.style.ERROR("ERRO DE CONEXÃO"))
                self.stdout.write(f"  {result.message}")
                raise CommandError("Não foi possível ligar ao HOST.")

            self.stdout.write(self.style.SUCCESS("CONEXÃO OK"))
            self.stdout.write(f"  Versão do servidor: {result.server_version or '-'}")
            self.stdout.write(f"  Tempo: {result.elapsed_ms} ms")
            self.stdout.write("  Proteções de leitura ativas:")
            for measure in result.read_only_measures:
                self.stdout.write(f"    - {measure}")

            self.stdout.write("")
            permissions = check_permissions(db)
            if permissions.read_only:
                self.stdout.write(self.style.SUCCESS("PERMISSÕES OK: o utilizador não tem permissões de escrita."))
            else:
                self.stdout.write(self.style.WARNING("ATENÇÃO ÀS PERMISSÕES DO UTILIZADOR DO HOST:"))
                for permission in permissions.write_permissions:
                    self.stdout.write(f"  - Permissão de escrita: {permission}")
                for warning in permissions.warnings:
                    self.stdout.write(f"  - {warning}")
                self.stdout.write(
                    "  Recomendação: usar um utilizador dedicado só com SELECT (ex.: SQL Server db_datareader)."
                )
        finally:
            db.dispose()
