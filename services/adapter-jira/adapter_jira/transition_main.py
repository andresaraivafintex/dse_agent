"""Entrypoint of the transition worker (`python -m adapter_jira.transition_main`).
Separate process from the FastAPI app — drains `jira_transition_queue`,
serializing per ticket (WSA-E5-T3), against the real Jira API."""
from __future__ import annotations

import time

from .backend import build_real_jira_client
from .transitions import TransitionWorker


def main() -> None:  # pragma: no cover - production loop
    worker = TransitionWorker(build_real_jira_client())
    print("[adapter-jira] transition worker running")
    while True:
        n = worker.drain_once()
        if n == 0:
            time.sleep(1.0)


if __name__ == "__main__":  # pragma: no cover
    main()
