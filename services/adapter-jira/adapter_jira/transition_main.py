"""Entrypoint do worker de transições (`python -m adapter_jira.transition_main`).
Processo separado do FastAPI app — drena `jira_transition_queue` serializando
por ticket (WSA-E5-T3) contra a API real do Jira."""
from __future__ import annotations

import time

from .backend import build_real_jira_client
from .transitions import TransitionWorker


def main() -> None:  # pragma: no cover - loop de produção
    worker = TransitionWorker(build_real_jira_client())
    print("[adapter-jira] transition worker rodando")
    while True:
        n = worker.drain_once()
        if n == 0:
            time.sleep(1.0)


if __name__ == "__main__":  # pragma: no cover
    main()
