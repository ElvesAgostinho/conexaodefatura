"""Verifica se o utilizador configurado para o HOST tem permissões de escrita.

O Gateway nunca escreve no HOST, mas o utilizador da BD deve ser, por si só,
incapaz de o fazer. Este módulo avisa quando isso não acontece.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .connection import HostDatabase
from .diagnostic_queries import PERMISSIONS, WRITE_PERMISSIONS


@dataclass
class PermissionReport:
    checked: bool
    write_permissions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def read_only(self) -> bool:
        return self.checked and not self.write_permissions and not self.warnings


def check_permissions(db: HostDatabase) -> PermissionReport:
    queries = PERMISSIONS.get(db.config.engine)
    if not queries:
        return PermissionReport(
            checked=False,
            warnings=[
                f"Verificação automática indisponível para '{db.config.engine}'. "
                "Confirme com o DBA que o utilizador só tem permissão SELECT."
            ],
        )

    report = PermissionReport(checked=True)
    try:
        if "roles" in queries:
            roles = db.fetch_all(queries["roles"])
            for role, value in (roles[0] if roles else {}).items():
                if value in (1, True):
                    report.warnings.append(f"O utilizador tem o papel '{role}' (permite escrita).")

        for row in db.fetch_all(queries["database"]):
            permission = str(row["permission_name"]).upper()
            if permission in WRITE_PERMISSIONS:
                report.write_permissions.append(permission)
    except Exception as exc:  # noqa: BLE001
        return PermissionReport(
            checked=False,
            warnings=[f"Não foi possível verificar permissões: {db.safe_error(exc)}"],
        )

    report.write_permissions = sorted(set(report.write_permissions))
    return report
