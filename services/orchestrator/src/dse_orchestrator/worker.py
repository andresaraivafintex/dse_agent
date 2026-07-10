"""WSB-E1-T1/T2/T4 — worker Temporal do Fintex DSE.

Conecta em `localhost:7233` (ou `DSE_TEMPORAL_ADDRESS`), registra
`WorkItemLifecycleWorkflow` e as Activities locais do WS-B (audit,
persistencia de status, checklist de clarificacao), tenta importar
(defensivamente) as Activities cross-workstream de WS-C (sandbox-runtime) e
WS-E (validation) quando existirem, e expoe um health endpoint HTTP em
`:8900` (`DSE_ORCHESTRATOR_HEALTH_PORT`).

Worker Versioning (WSB-E1-T2): `--build-id`/`DSE_WORKER_BUILD_ID` fixam o
build id deste processo. Ver `RUNBOOK.md` para o procedimento de
drain-and-cutover ao trocar de build id em producao.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import signal
import threading
from typing import Any

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from dse_contracts.constants import TASK_QUEUE

from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.otel_interceptor import setup_tracing
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

logging.basicConfig(level=os.environ.get("DSE_LOG_LEVEL", "INFO"))
logger = logging.getLogger("dse_orchestrator.worker")


def _load_cross_workstream_activities() -> list[Any]:
    """Import defensivo: enquanto WS-C/WS-E ainda nao publicaram suas
    Activities reais, o worker sobe mesmo assim (so com as Activities
    locais); quando existirem, sao carregadas e registradas automaticamente.

    Modulo/funcao esperados por cada workstream (ver README.md, secao
    "Integracao com WS-C e WS-E" para o contrato completo assumido):
      - WS-C: `sandbox_runtime.activities` — deve expor uma lista
        `ACTIVITIES` (ou funcoes individuais) implementando
        `provision_sandbox`, `run_coder_turn`, `checkpoint_sandbox`,
        `rebuild_sandbox`, `teardown_sandbox` (nomes de
        `dse_contracts.activities`).
      - WS-E: `validation.activities` — idem para `run_l1_pipeline`,
        `finalize_pr`, `post_tracking_comment`, `consume_ci_status`.
    """
    found: list[Any] = []
    for module_name in ("sandbox_runtime.activities", "dse_validation.activities"):
        try:
            mod = importlib.import_module(module_name)
        except ImportError as exc:
            logger.warning(
                "Activities cross-workstream de '%s' ainda nao disponiveis (%s); "
                "worker sobe sem elas — workflows que as chamarem ficarao "
                "pendentes na Activity ate o worker ser reiniciado com o modulo presente.",
                module_name, exc,
            )
            continue
        activities = getattr(mod, "ACTIVITIES", None)
        if activities:
            found.extend(activities)
            logger.info("Registradas %d activities de '%s'", len(activities), module_name)
        else:
            logger.warning(
                "'%s' importado mas nao expoe `ACTIVITIES` (lista); nada registrado.",
                module_name,
            )
    return found


def _start_health_server(port: int, build_id: str) -> None:
    """Servidor HTTP minimo (sem depender do worker rodar dentro do mesmo
    loop asyncio) — usa `http.server` em thread daemon para nao competir com
    o event loop do worker Temporal por I/O."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                body = f'{{"status":"ok","build_id":"{build_id}","task_queue":"{TASK_QUEUE}"}}'.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            logger.debug("health: " + format, *args)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("Health endpoint em http://0.0.0.0:%d/health", port)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fintex DSE — orchestrator worker (WS-B)")
    parser.add_argument(
        "--build-id",
        default=os.environ.get("DSE_WORKER_BUILD_ID", "dev"),
        help="Build id fixo deste deploy (Worker Versioning — ver RUNBOOK.md).",
    )
    parser.add_argument(
        "--temporal-address",
        default=os.environ.get("DSE_TEMPORAL_ADDRESS", "localhost:7233"),
        help="Endereco do Temporal frontend.",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=int(os.environ.get("DSE_ORCHESTRATOR_HEALTH_PORT", "8900")),
    )
    parser.add_argument(
        "--use-worker-versioning",
        action="store_true",
        default=os.environ.get("DSE_WORKER_USE_VERSIONING", "false").lower() == "true",
        help="Ativa Worker Versioning classico (build_id compat set). Ver RUNBOOK.md.",
    )
    return parser.parse_args(argv)


async def run_worker(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logger.info("Conectando ao Temporal em %s (build_id=%s, task_queue=%s)",
                args.temporal_address, args.build_id, TASK_QUEUE)

    tracing_interceptor = setup_tracing()

    client = await Client.connect(
        args.temporal_address,
        data_converter=pydantic_data_converter,
    )

    activities = list(LOCAL_ACTIVITIES) + _load_cross_workstream_activities()

    worker_kwargs: dict[str, Any] = dict(
        client=client,
        task_queue=TASK_QUEUE,
        workflows=[WorkItemLifecycleWorkflow],
        activities=activities,
        interceptors=[tracing_interceptor],
        build_id=args.build_id,
    )
    if args.use_worker_versioning:
        worker_kwargs["use_worker_versioning"] = True

    worker = Worker(**worker_kwargs)

    _start_health_server(args.health_port, args.build_id)

    logger.info("Worker no ar. %d activities registradas.", len(activities))

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Sinal de shutdown recebido — drenando worker (graceful).")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig_name):
            try:
                loop.add_signal_handler(getattr(signal, sig_name), _handle_signal)
            except (NotImplementedError, RuntimeError):
                pass  # plataformas sem suporte (ex.: Windows) — segue sem handler

    async with worker:
        await stop_event.wait()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
