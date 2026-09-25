"""Corre o Gateway em produção: painel/API (waitress) + tarefas automáticas.

Uso:  python manage.py run_gateway [--host *] [--port 8000]
                                   [--sync-minutes 5] [--queue-minutes 2] [--no-scheduler]

É o que o instalador arranca como tarefa do Windows. As tarefas automáticas:
  * a cada --sync-minutes: lê documentos novos das ligações de base de dados ativas;
  * a cada --queue-minutes: envia a fila à AGT.
Uma falha numa tarefa fica no registo e não pára o Gateway.
"""

from __future__ import annotations

import logging
import threading
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

logger = logging.getLogger("gateway.scheduler")


def run_sync_once() -> int:
    from sources.models import DataSource
    from sources.sync import SyncError, sync_source

    done = 0
    sources = DataSource.objects.filter(kind=DataSource.Kind.DATABASE, active=True, company__active=True)
    for source in sources.exclude(mapping={}).select_related("company"):
        try:
            result = sync_source(source)
            logger.info("sync %s/%s: %s", source.company.nif, source.code, result.summary())
            done += 1
        except SyncError as exc:
            logger.warning("sync %s/%s: %s", source.company.nif, source.code, exc)
    return done


def run_queue_once() -> None:
    from agt.queue import process_queue

    stats = process_queue(limit=200)
    if stats.processed:
        logger.info("fila AGT: %s", stats)


class Scheduler(threading.Thread):
    """Executa as tarefas periodicamente numa thread própria."""

    def __init__(self, jobs: list[tuple[str, float, callable]], tick: float = 1.0):
        super().__init__(name="gateway-scheduler", daemon=True)
        # None = ainda não correu: corre logo ao arrancar (o relógio monótono conta desde que o
        # Windows ligou, por isso comparar com 0 adiaria a primeira execução logo após o arranque).
        self.jobs = [[name, interval, fn, None] for name, interval, fn in jobs]
        self.stop_event = threading.Event()
        self.tick = tick
        self.runs: dict[str, int] = {name: 0 for name, _, _ in jobs}
        self.errors: dict[str, int] = {name: 0 for name, _, _ in jobs}

    def run(self):
        while not self.stop_event.is_set():
            now = time.monotonic()
            for job in self.jobs:
                name, interval, fn, last = job
                if last is None or now - last >= interval:
                    job[3] = now
                    close_old_connections()
                    try:
                        fn()
                        self.runs[name] += 1
                    except Exception:  # noqa: BLE001 - nunca derruba o Gateway
                        self.errors[name] += 1
                        logger.exception("tarefa %s falhou", name)
                    finally:
                        close_old_connections()
            self.stop_event.wait(self.tick)

    def stop(self):
        self.stop_event.set()


class Command(BaseCommand):
    help = "Corre o Gateway (painel, API e tarefas automáticas)."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="*", help="* = todos os endereços IPv4 e IPv6 (omissão).")
        parser.add_argument("--port", type=int, default=8000)
        parser.add_argument("--threads", type=int, default=8)
        parser.add_argument("--sync-minutes", type=float, default=5)
        parser.add_argument("--queue-minutes", type=float, default=2)
        parser.add_argument("--no-scheduler", action="store_true")

    def handle(self, *args, **options):
        if not 1 <= options["port"] <= 65535:
            raise CommandError("Porta inválida.")
        if options["sync_minutes"] <= 0 or options["queue_minutes"] <= 0:
            raise CommandError("Os intervalos têm de ser positivos.")
        from waitress import serve

        from config.wsgi import application

        scheduler = None
        if not options["no_scheduler"]:
            scheduler = Scheduler([
                ("sincronizar ligações", options["sync_minutes"] * 60, run_sync_once),
                ("enviar fila AGT", options["queue_minutes"] * 60, run_queue_once),
            ])
            scheduler.start()
        self.stdout.write(f"Gateway Fiscal a correr em http://{options['host']}:{options['port']}/")
        try:
            # "localhost" pode ser resolvido como IPv6 (::1): com "*" o Gateway escuta em IPv4 e IPv6.
            serve(application, listen=f"{options['host']}:{options['port']}", threads=options["threads"],
                  ident="GatewayFiscal")
        finally:
            if scheduler:
                scheduler.stop()
