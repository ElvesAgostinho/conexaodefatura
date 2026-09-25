"""Primeira configuração de uma instalação: empresa e administrador (usado pelo instalador).

Uso:  python manage.py gateway_bootstrap --empresa "Hotel X, Lda" --nif 5000000000 --utilizador admin
      (a senha vem da variável GATEWAY_ADMIN_PASSWORD, para não ficar no histórico da consola)

Idempotente: se a empresa ou o utilizador já existirem, não são duplicados nem alterados
(exceto a senha, se --redefinir-senha for indicado).
"""

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from agt.models import AgtConfiguration
from companies.models import Company, Membership


class Command(BaseCommand):
    help = "Cria a empresa e o administrador de uma instalação nova."

    def add_arguments(self, parser):
        parser.add_argument("--empresa", required=True)
        parser.add_argument("--nif", required=True)
        parser.add_argument("--utilizador", required=True)
        parser.add_argument("--redefinir-senha", action="store_true")

    def handle(self, *args, **options):
        name, nif, username = options["empresa"].strip(), options["nif"].strip(), options["utilizador"].strip()
        if not name or not nif or not username:
            raise CommandError("Empresa, NIF e utilizador são obrigatórios.")
        if not nif.isalnum() or len(nif) > 20:
            raise CommandError("NIF inválido.")
        password = os.environ.get("GATEWAY_ADMIN_PASSWORD", "")
        User = get_user_model()
        user = User.objects.filter(username=username).first()
        if user is None or options["redefinir_senha"]:
            if not password:
                raise CommandError("Defina a senha do administrador na variável GATEWAY_ADMIN_PASSWORD.")
            try:
                validate_password(password, user=user)
            except ValidationError as exc:
                raise CommandError("Senha fraca: " + " ".join(exc.messages)) from exc

        with transaction.atomic():
            company, company_created = Company.objects.get_or_create(nif=nif, defaults={"name": name})
            AgtConfiguration.for_company(company)
            if user is None:
                user = User.objects.create_superuser(username=username, password=password, email="")
                user_created = True
            else:
                user_created = False
                if options["redefinir_senha"]:
                    user.set_password(password)
                    user.save(update_fields=["password"])
            Membership.objects.get_or_create(user=user, company=company, defaults={"role": Membership.Role.ADMIN})

        self.stdout.write(self.style.SUCCESS(
            f"Empresa {'criada' if company_created else 'já existia'}: {company}. "
            f"Administrador {'criado' if user_created else 'já existia'}: {username}."))
