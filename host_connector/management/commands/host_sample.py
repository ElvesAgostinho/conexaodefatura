"""Apoio à Fase 6: mostra algumas linhas de uma tabela/vista do HOST para confirmar o mapeamento.

Uso:
    python manage.py host_sample NomeTabela
    python manage.py host_sample dbo.NomeTabela --limit 3 --order-by DataDoc --desc

O nome da tabela e das colunas é validado contra a estrutura real da BD
(nada de SQL livre). Os dados só são mostrados no terminal, não são gravados.
"""

from django.core.management.base import CommandError
from sqlalchemy import MetaData, Table, inspect, select

from ._base import HostCommand

MAX_LIMIT = 50
MAX_VALUE_LENGTH = 80


def _format(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binário {len(bytes(value))} bytes>"
    text = str(value)
    return text if len(text) <= MAX_VALUE_LENGTH else text[: MAX_VALUE_LENGTH - 3] + "..."


class Command(HostCommand):
    help = "Mostra algumas linhas de uma tabela/vista do HOST (somente leitura)."

    def add_arguments(self, parser):
        parser.add_argument("table", help="Tabela ou vista, opcionalmente com schema: schema.tabela")
        parser.add_argument("--limit", type=int, default=5, help=f"Número de linhas (máx. {MAX_LIMIT}).")
        parser.add_argument("--order-by", help="Coluna para ordenar (ex.: a data ou o ID do documento).")
        parser.add_argument("--desc", action="store_true", help="Ordem descendente (mais recentes primeiro).")

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= MAX_LIMIT:
            raise CommandError(f"--limit deve estar entre 1 e {MAX_LIMIT}.")

        schema, _, name = options["table"].rpartition(".")
        schema = schema or None

        db = self.get_host_database()
        try:
            with db.connect() as conn:
                inspector = inspect(conn)
                existing = set(inspector.get_table_names(schema=schema)) | set(inspector.get_view_names(schema=schema))
                if name not in existing:
                    raise CommandError(
                        f"'{options['table']}' não existe. Use host_inspect para ver os nomes reais."
                    )

                table = Table(name, MetaData(), schema=schema, autoload_with=conn)
                query = select(table).limit(limit)
                if options["order_by"]:
                    if options["order_by"] not in table.c:
                        raise CommandError(f"A coluna '{options['order_by']}' não existe em {options['table']}.")
                    column = table.c[options["order_by"]]
                    query = query.order_by(column.desc() if options["desc"] else column.asc())

                rows = conn.execute(query).mappings().all()
        except CommandError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CommandError(db.safe_error(exc)) from exc
        finally:
            db.dispose()

        self.stdout.write(f"{options['table']}: {len(rows)} linha(s)\n")
        for index, row in enumerate(rows, start=1):
            self.stdout.write(self.style.MIGRATE_HEADING(f"--- linha {index} ---"))
            width = max((len(key) for key in row.keys()), default=0)
            for key, value in row.items():
                self.stdout.write(f"  {key:<{width}}  {_format(value)}")
