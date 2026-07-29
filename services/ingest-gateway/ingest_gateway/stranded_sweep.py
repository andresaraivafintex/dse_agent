"""Scheduled caller for the stranded-work-item sweep.

`stranded.py` was written on 2026-07-24 for four specific rows — two in
`implementing`, one `queued`, one `new` — that had been silent for forty hours
with no workflow left in Temporal. It detects them, it can escalate them, it is
tested, and its own README says in as many words: "Not wired yet: nothing calls
stranded_work_items/escalate_stranded."

Five days later those same four rows were still sitting there, joined by six
more, and the console still showed them as work in progress. Recovery code that
nothing invokes is worse than absent: the runbook claims the failure mode is
handled, so nobody looks.

This is the caller. It is a module rather than Python embedded in the CronJob's
args so the decision it makes is unit-testable without a cluster — the same
reason build_pod_manifest and the sandbox reaper are modules.

Detection lives entirely in `stranded.py`; nothing here re-implements it. The
only judgement this file makes is the operational one: which tenant, how long
counts as silent, and whether to act or only report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .db import get_connection
from .stranded import escalate_stranded, stranded_work_items

#: 6h. Long enough that no legitimate step is mistaken for silence — a coder
#: turn runs minutes, the CI wait writes a row every 60s, and the plan-approval
#: gate is a human wait the sweep already excludes. Short enough that an
#: abandoned item is caught the same working day instead of decorating the
#: console for a week.
DEFAULT_IDLE_SECONDS = 6 * 3600

#: A sweep that finds dozens has found a systemic failure, not stragglers.
#: Escalating them all in one pass would bury the signal in the ledger; the cap
#: makes the next run pick up the rest and keeps one run's blast radius small.
DEFAULT_LIMIT = 25


def sweep(
    *,
    tenant_id: str,
    idle_seconds: int = DEFAULT_IDLE_SECONDS,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    actor: str = "system:stranded-sweep",
    conn=None,
) -> dict:
    owns = conn is None
    conn = conn or get_connection()
    try:
        found = stranded_work_items(
            conn, tenant_id=tenant_id, idle_for_seconds=idle_seconds, limit=limit
        )
        escalated: list[str] = []
        skipped: list[str] = []
        for row in found:
            # stranded_work_items returns dicts keyed work_item_id (not "id") —
            # reading the wrong key here fails on the first real sweep, which is
            # the run nobody is watching.
            wid = row["work_item_id"]
            if dry_run:
                skipped.append(wid)
                continue
            # escalate_stranded returns False when the item moved on between
            # detection and action — a live workflow that woke up, a human who
            # answered. Reporting that as escalated would be a lie in the trail.
            if escalate_stranded(
                conn, work_item_id=wid, tenant_id=tenant_id,
                idle_seconds=idle_seconds, actor=actor,
            ):
                escalated.append(wid)
            else:
                skipped.append(wid)
        return {
            "tenant_id": tenant_id,
            "idle_seconds": idle_seconds,
            "found": len(found),
            "escalated": escalated,
            "skipped": skipped,
            "dry_run": dry_run,
        }
    finally:
        if owns:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Escalate work items whose workflow is gone.")
    parser.add_argument("--tenant-id", default=os.environ.get("DSE_TENANT_ID", ""))
    parser.add_argument("--idle-seconds", type=int, default=DEFAULT_IDLE_SECONDS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.tenant_id:
        # Refuse rather than guess: the sweep writes terminal states, and the
        # wrong tenant means escalating somebody else's live work.
        print("stranded sweep: --tenant-id (or DSE_TENANT_ID) is required", file=sys.stderr)
        return 2
    try:
        result = sweep(
            tenant_id=args.tenant_id, idle_seconds=args.idle_seconds,
            limit=args.limit, dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"stranded sweep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
