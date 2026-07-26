"""Scheduler for the WS-F platform jobs (Phase 3):

  - scheduled rotation of service secrets (WSF-E2-T3b, ADR-28)
  - retention by data classification (WSF-E8-T2, §12.2)

Why a Python loop in compose and not a k3d CronJob (P7 decision, boring-first,
documented — required by the task's acceptance criteria):

  1. The CONSUMERS of the service secrets (WS-A adapters, WS-D gateway, WS-C
     broker) and the Postgres targeted by retention all live in docker-compose —
     scheduling on the k3d cluster would create a cross-runtime dependency
     (cluster down => rotation/retention stop) with nothing gained.
  2. The k3d cluster exists for PREVIEWS (Argo CD/ESO) — the correct production
     analogue is documented: on real K8s this same module runs as a CronJob
     (`python -m dse_platform.jobs_scheduler --once`) — the entrypoint already
     supports single-shot execution exactly for that.
  3. A ~100-line Python scheduler with a sleep is the most boring thing that
     works; cron inside a container and Temporal Schedules were considered and
     rejected (more moving parts for the same effect; a Temporal Schedule would
     couple secret rotation to the availability of Temporal itself, which is one
     of the indirect consumers).

Env config (never secrets in env — scheduling only):

  DSE_ROTATION_INTERVAL_SECONDS   default 86400 (24h)
  DSE_ROTATION_MANIFEST           inline JSON OR path to a JSON file:
                                  [{"path": "dse/service/queue-board-session",
                                    "tenant_id": "platform"}, ...]
  DSE_RETENTION_INTERVAL_SECONDS  default 86400 (24h)
  DSE_JOBS_RUN_AT_START           "1" runs both immediately at boot

P8: every rotation and every retention run writes an audit row (in the
respective modules) — this scheduler only schedules and logs; it decides nothing
beyond "it's time" (P1).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from .retention import run_retention_all_tenants
from .secret_rotation import RotationError, rotate_from_manifest

log = logging.getLogger("dse.platform.jobs")


def _load_manifest() -> list[dict[str, Any]]:
    raw = os.environ.get("DSE_ROTATION_MANIFEST", "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        manifest = json.loads(raw)
    else:
        manifest = json.loads(Path(raw).read_text())
    if not isinstance(manifest, list):
        raise ValueError("DSE_ROTATION_MANIFEST must be a JSON list of {path, tenant_id}")
    return manifest


def run_rotation_once() -> int:
    """Runs the manifest rotation. Returns the number of failures (0 = success)."""
    manifest = _load_manifest()
    if not manifest:
        log.info("rotation: empty manifest (DSE_ROTATION_MANIFEST) — nothing to rotate")
        return 0
    results = rotate_from_manifest(manifest)
    failures = 0
    for res in results:
        if isinstance(res, RotationError):
            failures += 1
            log.error("rotation FAILED: %s", res)
        else:
            log.info(
                "rotated: %s v%s -> v%s (keys=%s)",
                res.path, res.old_version, res.new_version, ",".join(res.rotated_keys),
            )
    return failures


def run_retention_once() -> int:
    """Runs retention for every tenant that has a policy. Returns the failure count."""
    try:
        reports = run_retention_all_tenants()
    except Exception:
        log.exception("retention FAILED")
        return 1
    for report in reports:
        log.info(
            "retention tenant=%s: %d rows affected across %d targets",
            report.tenant_id, report.total_affected, len(report.results),
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="WS-F platform jobs (secret rotation + retention)")
    parser.add_argument("--once", action="store_true", help="run both jobs once and exit (CronJob mode)")
    args = parser.parse_args(argv)

    if args.once:
        return min(run_rotation_once() + run_retention_once(), 1)

    rotation_interval = int(os.environ.get("DSE_ROTATION_INTERVAL_SECONDS", "86400"))
    retention_interval = int(os.environ.get("DSE_RETENTION_INTERVAL_SECONDS", "86400"))
    now = time.monotonic()
    if os.environ.get("DSE_JOBS_RUN_AT_START") == "1":
        next_rotation, next_retention = now, now
    else:
        next_rotation, next_retention = now + rotation_interval, now + retention_interval

    log.info(
        "scheduler up: rotation every %ds, retention every %ds", rotation_interval, retention_interval
    )
    while True:
        now = time.monotonic()
        if now >= next_rotation:
            run_rotation_once()  # failures are logged + audited; the loop goes on (P6: clean per-job failure)
            next_rotation = now + rotation_interval
        if now >= next_retention:
            run_retention_once()
            next_retention = now + retention_interval
        time.sleep(max(1.0, min(next_rotation, next_retention) - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
