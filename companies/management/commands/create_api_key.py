"""Cria uma chave de API para uma empresa. A chave só é mostrada uma vez.

Uso:  python manage.py create_api_key --company 5000000000 --name "ERP da empresa" [--source ERP]
Sem --source, usa (ou cria) a origem API por omissão da empresa (código API).
"""

from django.core.management.base import BaseCommand, CommandError

from companies.models import ApiKey, Company


class Command(BaseCommand):
    help = "Cria uma chave de API para uma empresa (identificada pelo NIF)."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="NIF da empresa.")
        parser.add_argument("--name", required=True, help="Nome/descrição da chave.")
        parser.add_argument("--source", help="Código da origem do tipo API (omissão: API).")

    def handle(self, *args, **options):
        company = Company.objects.filter(nif=options["company"]).first()
        if company is None:
            raise CommandError(f"Empresa com NIF {options['company']} não existe.")
        if not company.active:
            raise CommandError("A empresa está inativa.")
        source = None
        if options["source"]:
            source = company.sources.filter(code=options["source"], kind="API").first()
            if source is None:
                raise CommandError(f"A empresa não tem uma origem do tipo API com o código {options['source']}.")
        _, raw_key = ApiKey.generate(company, options["name"], source)
        self.stdout.write(self.style.SUCCESS("Chave criada. Guarde-a agora; não voltará a ser mostrada:"))
        self.stdout.write(raw_key)
