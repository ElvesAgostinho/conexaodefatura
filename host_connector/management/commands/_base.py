from django.core.management.base import BaseCommand, CommandError

from host_connector.config import DEFAULT_CONNECTION, HostConfigError, HostConnectionConfig
from host_connector.connection import HostDatabase


class HostCommand(BaseCommand):
    """Base dos comandos que usam uma ligação de leitura a uma BD de origem.

    --connection NOME   usa as variáveis HOST_NOME_DB_* do .env
    --company NIF       usa a origem do tipo base de dados da empresa
                        (--source CODIGO quando a empresa tem mais de uma)
    Sem nenhum, usa HOST_DB_*.
    """

    def create_parser(self, prog_name, subcommand, **kwargs):
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        target = parser.add_mutually_exclusive_group()
        target.add_argument("--connection", help="Nome da ligação (variáveis HOST_<NOME>_DB_*).")
        target.add_argument("--company", help="NIF da empresa; usa a sua origem do tipo base de dados.")
        parser.add_argument("--source", help="Código da origem (com --company).")
        return parser

    def execute(self, *args, **options):
        self._host_target = (options.get("connection"), options.get("company"), options.get("source"))
        return super().execute(*args, **options)

    def get_host_config(self) -> HostConnectionConfig:
        connection, company_nif, source_code = getattr(self, "_host_target", (None, None, None))
        if source_code and not company_nif:
            raise CommandError("--source exige --company.")
        if company_nif:
            connection = self._company_connection(company_nif, source_code)
        try:
            return HostConnectionConfig.from_env(connection or DEFAULT_CONNECTION)
        except HostConfigError as exc:
            raise CommandError(f"Configuração da ligação inválida: {exc}") from exc

    @staticmethod
    def _company_connection(company_nif: str, source_code: str | None) -> str:
        from companies.models import Company
        from sources.models import DataSource

        company = Company.objects.filter(nif=company_nif).first()
        if company is None:
            raise CommandError(f"Empresa com NIF {company_nif} não existe.")
        sources = company.sources.filter(kind=DataSource.Kind.DATABASE)
        if source_code:
            sources = sources.filter(code=source_code)
        sources = list(sources)
        if not sources:
            raise CommandError(f"A empresa {company} não tem origem do tipo base de dados"
                               + (f" com o código {source_code}." if source_code else "."))
        if len(sources) > 1:
            codes = ", ".join(s.code for s in sources)
            raise CommandError(f"A empresa tem várias origens de base de dados ({codes}): indique --source.")
        return sources[0].connection

    def get_host_database(self) -> HostDatabase:
        return HostDatabase(self.get_host_config())

    def print_config(self, db: HostDatabase) -> None:
        self.stdout.write("Configuração da ligação:")
        for key, value in db.config.describe().items():
            self.stdout.write(f"  {key:<9} {value if value is not None else '-'}")
