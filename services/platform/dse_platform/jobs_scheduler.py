"""Scheduler dos jobs de plataforma do WS-F (Fase 3):

  - rotação agendada de secrets de serviço (WSF-E2-T3b, ADR-28)
  - retenção por classificação de dados (WSF-E8-T2, §12.2)

Por que um loop Python no compose e não um CronJob no k3d (decisão P7,
boring-first, documentada — pedida pelo aceite da tarefa):

  1. Os CONSUMIDORES dos secrets de serviço (adapters WS-A, gateway WS-D,
     broker WS-C) e o Postgres alvo da retenção vivem no docker-compose —
     agendar no cluster k3d criaria uma dependência cruzada de runtime
     (cluster fora do ar => rotação/retenção param) sem ganhar nada.
  2. O cluster k3d existe para PREVIEWS (Argo CD/ESO) — o análogo de
     produção correto está documentado: em K8s real, este mesmo módulo roda
     como CronJob (`python -m dse_platform.jobs_scheduler --once`) — o
     entrypoint já suporta execução única exatamente para isso.
  3. Um scheduler Python de ~100 linhas com sleep é a coisa mais chata que
     funciona; cron dentro de container e Temporal Schedules foram
     considerados e rejeitados (mais partes móveis para o mesmo efeito; um
     Temporal Schedule acopla a rotação de secrets à disponibilidade do
     próprio Temporal, que é um dos consumidores indiretos).

Config por env (nunca secrets em env — só agendamento):

  DSE_ROTATION_INTERVAL_SECONDS   default 86400 (24h)
  DSE_ROTATION_MANIFEST           JSON inline OU path de arquivo JSON:
                                  [{"path": "dse/service/queue-board-session",
                                    "tenant_id": "platform"}, ...]
  DSE_RETENTION_INTERVAL_SECONDS  default 86400 (24h)
  DSE_JOBS_RUN_AT_START           "1" roda ambos imediatamente no boot

P8: cada rotação e cada execução de retenção grava audit row (nos módulos
respectivos) — este scheduler só agenda e loga; não decide nada além de
"chegou a hora" (P1).
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
        raise ValueError("DSE_ROTATION_MANIFEST deve ser uma lista JSON de {path, tenant_id}")
    return manifest


def run_rotation_once() -> int:
    """Roda a rotação do manifest. Retorna o nº de falhas (0 = sucesso)."""
    manifest = _load_manifest()
    if not manifest:
        log.info("rotação: manifest vazio (DSE_ROTATION_MANIFEST) — nada a rotacionar")
        return 0
    results = rotate_from_manifest(manifest)
    failures = 0
    for res in results:
        if isinstance(res, RotationError):
            failures += 1
            log.error("rotação FALHOU: %s", res)
        else:
            log.info(
                "rotacionado: %s v%s -> v%s (keys=%s)",
                res.path, res.old_version, res.new_version, ",".join(res.rotated_keys),
            )
    return failures


def run_retention_once() -> int:
    """Roda a retenção de todos os tenants com política. Retorna nº de falhas."""
    try:
        reports = run_retention_all_tenants()
    except Exception:
        log.exception("retenção FALHOU")
        return 1
    for report in reports:
        log.info(
            "retenção tenant=%s: %d linhas afetadas em %d alvos",
            report.tenant_id, report.total_affected, len(report.results),
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="WS-F platform jobs (rotação de secrets + retenção)")
    parser.add_argument("--once", action="store_true", help="roda ambos os jobs uma vez e sai (modo CronJob)")
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
        "scheduler no ar: rotação a cada %ds, retenção a cada %ds", rotation_interval, retention_interval
    )
    while True:
        now = time.monotonic()
        if now >= next_rotation:
            run_rotation_once()  # falhas logadas + auditadas; o loop continua (P6: falha limpa por job)
            next_rotation = now + rotation_interval
        if now >= next_retention:
            run_retention_once()
            next_retention = now + retention_interval
        time.sleep(max(1.0, min(next_rotation, next_retention) - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
