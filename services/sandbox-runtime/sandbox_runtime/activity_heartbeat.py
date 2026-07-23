"""Execução de chamadas síncronas longas com heartbeat Temporal real.

Os SDKs de agente expõem hoje uma superfície síncrona. Chamá-los diretamente
dentro de uma Activity ``async`` bloqueia o event loop e impede heartbeats.
Este helper desloca somente a chamada bloqueante para uma thread e mantém o
loop da Activity livre para emitir progresso periódico.

Fora de uma Activity Temporal o mesmo helper continua funcionando e nunca
tenta acessar o contexto inexistente de ``temporalio.activity``.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any, TypeVar

from temporalio import activity


HEARTBEAT_INTERVAL_ENV_VAR = "DSE_ACTIVITY_HEARTBEAT_INTERVAL_SECONDS"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 20.0
_MIN_HEARTBEAT_INTERVAL_SECONDS = 0.01

_T = TypeVar("_T")


def _configured_interval(override: float | None = None) -> float:
    raw: str | float = (
        override
        if override is not None
        else os.environ.get(HEARTBEAT_INTERVAL_ENV_VAR, str(DEFAULT_HEARTBEAT_INTERVAL_SECONDS))
    )
    try:
        interval = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{HEARTBEAT_INTERVAL_ENV_VAR}={raw!r} precisa ser um número positivo"
        ) from exc
    if interval < _MIN_HEARTBEAT_INTERVAL_SECONDS:
        raise ValueError(
            f"{HEARTBEAT_INTERVAL_ENV_VAR} precisa ser >= {_MIN_HEARTBEAT_INTERVAL_SECONDS}s"
        )
    return interval


def _effective_interval(configured: float) -> float:
    """Mantém pelo menos três oportunidades dentro do heartbeat timeout."""
    if not activity.in_activity():
        return configured
    timeout = activity.info().heartbeat_timeout
    if timeout is None:
        return configured
    timeout_seconds = timeout.total_seconds()
    if timeout_seconds <= 0:
        return configured
    return max(
        _MIN_HEARTBEAT_INTERVAL_SECONDS,
        min(configured, timeout_seconds / 3.0),
    )


def _details(
    *,
    stage: str,
    work_item_id: str,
    operation: str,
    state: str,
    started_at: float,
    sequence: int,
) -> dict[str, Any]:
    # Deliberadamente sem instruction, prompt, virtual key ou output do
    # modelo: heartbeat vai para a história operacional do Temporal.
    return {
        "schema_version": 1,
        "component": "sandbox-runtime",
        "stage": stage,
        "work_item_id": work_item_id,
        "operation": operation,
        "state": state,
        "sequence": sequence,
        "elapsed_seconds": round(max(0.0, time.monotonic() - started_at), 3),
    }


async def run_sync_with_heartbeat(
    fn: Callable[..., _T],
    /,
    *args: Any,
    stage: str,
    work_item_id: str,
    operation: str,
    interval_seconds: float | None = None,
    **kwargs: Any,
) -> _T:
    """Executa ``fn`` sem bloquear heartbeats da Activity.

    A primeira batida é emitida antes da chamada, as seguintes em intervalo
    regular e uma última após retorno. Exceções da função ou do heartbeat não
    são escondidas. Quando chamado fora do Temporal, apenas executa ``fn`` na
    thread de trabalho e devolve/propaga o mesmo resultado.
    """
    configured_interval = _configured_interval(interval_seconds)
    in_activity = activity.in_activity()
    interval = _effective_interval(configured_interval) if in_activity else configured_interval
    started_at = time.monotonic()
    sequence = 0

    if in_activity:
        activity.heartbeat(
            _details(
                stage=stage,
                work_item_id=work_item_id,
                operation=operation,
                state="started",
                started_at=started_at,
                sequence=sequence,
            )
        )

    call = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    while not call.done():
        done, _ = await asyncio.wait({call}, timeout=interval)
        if call in done:
            break
        if in_activity:
            sequence += 1
            activity.heartbeat(
                _details(
                    stage=stage,
                    work_item_id=work_item_id,
                    operation=operation,
                    state="running",
                    started_at=started_at,
                    sequence=sequence,
                )
            )

    result = await call
    if in_activity:
        sequence += 1
        activity.heartbeat(
            _details(
                stage=stage,
                work_item_id=work_item_id,
                operation=operation,
                state="completed",
                started_at=started_at,
                sequence=sequence,
            )
        )
    return result

