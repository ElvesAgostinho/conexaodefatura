"""Sincronização de origens do tipo DATABASE (leitura incremental por cursor).

Segurança fiscal: se um documento não puder ser lido (estrutura inválida ou coluna em
falta), a sincronização PÁRA nesse documento e o cursor não o ultrapassa. Nenhum
documento é saltado em silêncio; depois de corrigido o mapeamento/origem, a próxima
sincronização continua desse ponto. Documentos com erros de NEGÓCIO são importados
(estado ERROR) e o cursor avança.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from audit.services import log_integration
from host_connector.config import HostConfigError, HostConnectionConfig
from host_connector.connection import HostDatabase
from invoices.canonical import CanonicalError
from invoices.services import DocumentConflict, import_document

from .mapping import cursor_to_text, cursor_value, row_value, rows_to_payload, validate_mapping
from .models import DataSource


class SyncError(Exception):
    pass


@dataclass
class SyncResult:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    duplicates: int = 0
    conflicts: list[str] = field(default_factory=list)
    stopped_at: str = ""
    stop_reason: str = ""
    cursor: str = ""

    @property
    def status(self) -> str:
        if self.stop_reason:
            return DataSource.SyncStatus.ERROR if not (self.created or self.updated) else DataSource.SyncStatus.PARTIAL
        return DataSource.SyncStatus.PARTIAL if self.conflicts else DataSource.SyncStatus.OK

    def summary(self) -> str:
        parts = [f"{self.fetched} lidos", f"{self.created} novos", f"{self.updated} atualizados",
                 f"{self.duplicates} já existentes"]
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} conflitos")
        text = ", ".join(parts) + "."
        if self.stop_reason:
            text += f" Parado no documento {self.stopped_at}: {self.stop_reason}"
        return text


def database_for(source: DataSource) -> HostDatabase:
    return HostDatabase(HostConnectionConfig.from_env(source.connection))


def sync_source(source: DataSource, *, db: HostDatabase | None = None) -> SyncResult:
    if source.kind != DataSource.Kind.DATABASE:
        raise SyncError("Só origens do tipo base de dados são sincronizadas; origens API enviam os documentos.")
    if not source.active or not source.company.active:
        raise SyncError("A origem ou a empresa está inativa.")
    if not source.mapping:
        raise SyncError("O mapeamento desta origem ainda não está configurado.")
    problems = validate_mapping(source.mapping)
    if problems:
        raise SyncError("Mapeamento inválido: " + " ".join(problems))
    if not source.acquire_sync_lock():
        raise SyncError("Já está a decorrer uma sincronização desta origem.")

    result = SyncResult()
    owns_db = db is None
    try:
        try:
            db = db or database_for(source)
        except HostConfigError as exc:
            raise SyncError(f"Configuração da ligação inválida: {exc}") from exc
        _run(source, db, result)
    except SyncError as exc:
        result.stop_reason = result.stop_reason or str(exc)
        _finish(source, result)
        raise
    except Exception as exc:  # noqa: BLE001 - falha de ligação/consulta: registada sem segredos
        message = db.safe_error(exc) if db is not None else str(exc)
        result.stop_reason = message
        _finish(source, result)
        raise SyncError(message) from exc
    finally:
        source.release_sync_lock()
        if owns_db and db is not None:
            db.dispose()  # fecha as ligações à BD de origem
    _finish(source, result)
    return result


def _run(source: DataSource, db: HostDatabase, result: SyncResult) -> None:
    mapping = source.mapping
    cursor = cursor_value(mapping, source.sync_cursor)
    result.cursor = cursor_to_text(cursor)
    id_column = mapping["fields"]["source_document_id"]
    rows = db.fetch_all(mapping["documents_query"], {"cursor": cursor},
                        max_rows=mapping.get("batch_size", 200))
    for row in rows:
        result.fetched += 1
        try:
            document_id = row_value(row, id_column)
            next_cursor = cursor_value(mapping, row_value(row, mapping["cursor_column"]))
            lines = db.fetch_all(mapping["lines_query"], {"document_id": document_id})
            payload = rows_to_payload(mapping, row, lines)
            outcome = import_document(source, payload)
        except KeyError as exc:
            _stop(source, result, row, id_column, f"coluna {exc.args[0]!r} não existe no resultado da consulta.")
            return
        except (CanonicalError, ValueError) as exc:
            _stop(source, result, row, id_column, str(exc))
            return
        except DocumentConflict as exc:
            result.conflicts.append(f"{document_id}: {exc}")
            log_integration("SOURCE_CONFLICT", company=source.company, invoice=exc.invoice,
                            status="CONFLICT", error_message=str(exc))
        else:
            result.created += outcome.created
            result.updated += outcome.updated
            result.duplicates += outcome.duplicate
        # O cursor só avança depois de o documento estar tratado.
        result.cursor = cursor_to_text(next_cursor)
        DataSource.objects.filter(pk=source.pk).update(sync_cursor=result.cursor)
        source.sync_cursor = result.cursor


def _stop(source, result, row, id_column, reason) -> None:
    try:
        result.stopped_at = str(row_value(row, id_column))
    except KeyError:
        result.stopped_at = "?"
    result.stop_reason = reason
    log_integration("SOURCE_READ", company=source.company, status="ERROR",
                    error_message=f"Documento {result.stopped_at}: {reason}")


def _finish(source: DataSource, result: SyncResult) -> None:
    fields = dict(last_sync_at=timezone.now(), last_sync_status=result.status, last_sync_message=result.summary())
    DataSource.objects.filter(pk=source.pk).update(**fields)
    for name, value in fields.items():
        setattr(source, name, value)
    log_integration("SOURCE_SYNC", company=source.company, status=result.status, response_body=result.summary())
