"""WSB-E1-T1/T2/T4 — Fintex DSE Temporal worker.

Connects to `localhost:7233` (or `DSE_TEMPORAL_ADDRESS`), registers
`WorkItemLifecycleWorkflow` and the WS-B local Activities (audit, status
persistence, clarification checklist), defensively tries to import the
cross-workstream Activities from WS-C (sandbox-runtime) and WS-E (validation)
when they exist, and exposes an HTTP health endpoint on `:8900`
(`DSE_ORCHESTRATOR_HEALTH_PORT`).

Worker Versioning (WSB-E1-T2): `--build-id`/`DSE_WORKER_BUILD_ID` pin this
process's build id. See `RUNBOOK.md` for the drain-and-cutover procedure when
changing build id in production.
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
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker, WorkerDeploymentConfig

from dse_contracts.constants import TASK_QUEUE

from dse_orchestrator.fairness import (
    FairnessInterceptor,
    NativeFairnessController,
    WorkerSideFairnessController,
    postgres_cap_provider,
)
from dse_orchestrator.local_activities import LOCAL_ACTIVITIES
from dse_orchestrator.otel_interceptor import setup_tracing
from dse_orchestrator.workflows import WorkItemLifecycleWorkflow

logging.basicConfig(level=os.environ.get("DSE_LOG_LEVEL", "INFO"))
logger = logging.getLogger("dse_orchestrator.worker")


def _load_cross_workstream_activities() -> list[Any]:
    """Defensive import: while WS-C/WS-E have not published their real
    Activities yet, the worker still starts (with the local Activities only);
    once they exist, they are loaded and registered automatically.

    Module/function expected from each workstream (see README.md, section
    "Integracao com WS-C e WS-E" for the full assumed contract):
      - WS-C: `sandbox_runtime.activities` — must expose an `ACTIVITIES` list
        (or individual functions) implementing `provision_sandbox`,
        `run_coder_turn`, `checkpoint_sandbox`, `rebuild_sandbox`,
        `teardown_sandbox` (names from `dse_contracts.activities`).
      - WS-E: `validation.activities` — same for `run_l1_pipeline`,
        `finalize_pr`, `post_tracking_comment`, `consume_ci_status`.
    """
    found: list[Any] = []
    for module_name in ("sandbox_runtime.activities", "dse_validation.activities"):
        try:
            mod = importlib.import_module(module_name)
        except ImportError as exc:
            logger.warning(
                "cross-workstream Activities of '%s' are not available yet (%s); "
                "worker sobe sem elas — workflows que as chamarem ficarao "
                "pending in the Activity until the worker is restarted with the module present.",
                module_name, exc,
            )
            continue
        activities = getattr(mod, "ACTIVITIES", None)
        if activities:
            found.extend(activities)
            logger.info("Registradas %d activities de '%s'", len(activities), module_name)
        else:
            logger.warning(
                "'%s' imported but does not expose `ACTIVITIES` (a list); nothing registered.",
                module_name,
            )
    return found


def _start_health_server(port: int, build_id: str) -> None:
    """Minimal HTTP server (does not depend on the worker running in the same
    asyncio loop) — uses `http.server` on a daemon thread so it does not
    compete with the Temporal worker's event loop for I/O."""
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
        help="Fixed build id of this deploy (Worker Versioning — see RUNBOOK.md).",
    )
    parser.add_argument(
        "--temporal-address",
        default=os.environ.get("DSE_TEMPORAL_ADDRESS", "localhost:7233"),
        help="Address of the Temporal frontend.",
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
        help="Enables Worker Deployment Versioning (modern, PINNED). Requires the "
             "server with deployment versioning enabled + cutover via CLI. See RUNBOOK.md.",
    )
    parser.add_argument(
        "--deployment-name",
        default=os.environ.get("DSE_WORKER_DEPLOYMENT_NAME", "dse-orchestrator"),
        help="Worker Deployment name (F5). The (deployment, build_id) pair is the version.",
    )
    parser.add_argument(
        "--fairness-mode",
        default=os.environ.get("DSE_FAIRNESS_MODE", "worker-side"),
        choices=["worker-side", "native", "off"],
        help="WSB-E1-T3: 'worker-side' (per-tenant concurrency caps read from "
             "tenant_config, the default on this Temporal version), 'native' (delegates "
             "to the server P&F — 1.31+ only), 'off' (no gating).",
    )
    return parser.parse_args(argv)


def _build_fairness_interceptor(mode: str):
    """WSB-E1-T3 — build the fairness interceptor for the given mode. Swappable
    interface: once the server supports native P&F (1.31+), use `native` (no-op
    on the worker) and add `fairness_key=tenant_id` to the ActivityOptions."""
    if mode == "off":
        return None
    if mode == "native":
        return FairnessInterceptor(NativeFairnessController())
    controller = WorkerSideFairnessController(postgres_cap_provider())
    return FairnessInterceptor(controller)


def build_deployment_config(deployment_name: str, build_id: str) -> WorkerDeploymentConfig:
    """Plan 08 §F (F5) — OPERATIONAL Worker Versioning via the MODERN API (Worker
    Deployment Versioning), NOT the classic version-set (deprecated and off by
    default on current servers).

    This worker announces itself as the deployment's `(deployment_name, build_id)`
    version. `default_versioning_behavior=PINNED`: each workflow stays STUCK to
    the version it started on — that is what gives the safe drain-and-cutover
    (in-flight workflows finish on the old version; only NEW ones go to the
    current version).

    The cutover ("make this version the current one") is a deliberate OPS step —
    `temporal worker-deployment set-current-version` (CLI) or the Temporal
    console — NEVER automatic at boot (avoids an accidental cutover on every
    restart). See RUNBOOK.md §Worker-Versioning. Build_id pinned to the image
    SHA/tag (compose)."""
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(deployment_name=deployment_name, build_id=build_id),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    )


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

    interceptors: list[Any] = [tracing_interceptor]
    fairness_interceptor = _build_fairness_interceptor(args.fairness_mode)
    if fairness_interceptor is not None:
        interceptors.append(fairness_interceptor)
        logger.info("Fairness worker-side ativa (modo=%s)", args.fairness_mode)

    worker_kwargs: dict[str, Any] = dict(
        client=client,
        task_queue=TASK_QUEUE,
        workflows=[WorkItemLifecycleWorkflow],
        activities=activities,
        interceptors=interceptors,
    )
    if args.use_worker_versioning:
        # F5 (modern): the version comes from deployment_config; do NOT also pass
        # the classic build_id (it conflicts with deployment versioning).
        worker_kwargs["deployment_config"] = build_deployment_config(
            args.deployment_name, args.build_id
        )
        logger.info(
            "Worker Deployment Versioning ATIVO: deployment=%s version=%s (PINNED). "
            "Cutover is an operations step (temporal worker-deployment set-current-version).",
            args.deployment_name, args.build_id,
        )
    else:
        # no versioning: keep the classic build_id just as a label/health field.
        worker_kwargs["build_id"] = args.build_id

    worker = Worker(**worker_kwargs)

    _start_health_server(args.health_port, args.build_id)

    logger.info("Worker no ar. %d activities registradas.", len(activities))

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received — draining the worker (graceful).")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig_name):
            try:
                loop.add_signal_handler(getattr(signal, sig_name), _handle_signal)
            except (NotImplementedError, RuntimeError):
                pass  # unsupported platforms (e.g. Windows) — continue without a handler

    async with worker:
        await stop_event.wait()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
