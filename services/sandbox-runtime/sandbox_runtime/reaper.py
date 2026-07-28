"""TTL sweeper for leaked sandbox Pods — the cluster-side floor.

Sandbox Pods are BARE Pods: nothing owns them. A cross-namespace ownerReference
is not an option — a namespaced dependent's owner must be cluster-scoped or in
the SAME namespace, and the orchestrator lives in the control-plane namespace
while these Pods live in DSE_SANDBOX_K8S_NAMESPACE. Since 1.20 the collector
does not ignore such a reference: it raises OwnerRefInvalidNamespace and treats
the owner as absent, making the dependent eligible for IMMEDIATE deletion, so
adding one would kill every sandbox seconds after it was created.

That leaves teardown_sandbox as the only collector, and teardown cannot run if
the orchestrator crashed or Temporal purged the workflow — which is exactly how
four Pods survived three days with no workflow left to release them. This module
is that missing floor.

It reads `dse.fintex/expires-at` off the Pod rather than from the database for
the same reason the preview reaper does: the annotation travels WITH the object,
so it stays correct for a Pod whose workflow no longer exists and while the
orchestrator is down.

It lives here rather than inline in the CronJob template so the decision rule is
unit-testable without a cluster — the same reason build_pod_manifest is a pure
function.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

KEEP = "keep"
DELETE = "delete"

SANDBOX_SELECTOR = "app.kubernetes.io/managed-by=dse-sandbox"
EXPIRES_AT = "dse.fintex/expires-at"
WORK_ITEM_ID = "dse.fintex/work-item-id"

# A terminated sandbox is held this long before collection. A crashed sandbox is
# the one an operator most wants to `kubectl describe`, and deleting it the
# instant it dies destroys the only forensic surface there is (the worker's RBAC
# carries no pods/log).
DEFAULT_TERMINATED_GRACE_SECONDS = 900


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _terminated_at(pod: dict[str, Any]) -> datetime | None:
    """The latest container termination instant, or None if still running."""
    latest: datetime | None = None
    for cs in (pod.get("status") or {}).get("containerStatuses") or []:
        finished = _parse_ts(((cs.get("state") or {}).get("terminated") or {}).get("finishedAt"))
        if finished and (latest is None or finished > latest):
            latest = finished
    return latest


def decide(
    pod: dict[str, Any],
    now: datetime,
    terminated_grace_seconds: int = DEFAULT_TERMINATED_GRACE_SECONDS,
) -> tuple[str, str]:
    """Pure decision rule. Returns (KEEP|DELETE, why).

    Deliberately conservative: a Pod whose lifetime cannot be PROVEN to have
    passed is kept. Leaking one sandbox costs 250m of CPU request; deleting a
    live one destroys an agent turn mid-flight.
    """
    meta = pod.get("metadata") or {}
    ann = meta.get("annotations") or {}
    phase = (pod.get("status") or {}).get("phase")

    if meta.get("deletionTimestamp"):
        return KEEP, "already terminating"

    # restartPolicy=Never makes Succeeded/Failed final, and the runner's command
    # is a long sleep, so a live sandbox is always Running. A terminated one can
    # never be doing real work again.
    if phase in ("Succeeded", "Failed"):
        finished = _terminated_at(pod) or _parse_ts(meta.get("creationTimestamp"))
        if finished is None:
            return DELETE, f"phase={phase} with no timestamp to grace against"
        age = (now - finished).total_seconds()
        if age >= terminated_grace_seconds:
            return DELETE, f"phase={phase} for {int(age)}s (grace {terminated_grace_seconds}s)"
        return KEEP, f"phase={phase} but within the {terminated_grace_seconds}s inspection grace"

    expires = _parse_ts(ann.get(EXPIRES_AT))
    if expires is None:
        # No expiry recorded — a Pod from a build that predates the TTL stamp,
        # or one whose annotation was stripped. Never guess: leaking is cheaper
        # than killing live work.
        return KEEP, f"no {EXPIRES_AT} annotation"
    if expires <= now:
        return DELETE, f"expired at {expires.isoformat()}"
    return KEEP, f"expires at {expires.isoformat()}"


def _kubectl(args: list[str], kubectl: str = "kubectl") -> subprocess.CompletedProcess[str]:
    return subprocess.run([kubectl, *args], capture_output=True, text=True)


def sweep(
    namespace: str,
    *,
    now: datetime | None = None,
    terminated_grace_seconds: int = DEFAULT_TERMINATED_GRACE_SECONDS,
    dry_run: bool = False,
    kubectl: str = "kubectl",
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    proc = _kubectl(["get", "pods", "-n", namespace, "-l", SANDBOX_SELECTOR, "-o", "json"], kubectl)
    if proc.returncode != 0:
        # Surface WHY. A bare CalledProcessError hides whether this was RBAC,
        # DNS or a blocked route to the API server, and those have completely
        # different fixes.
        raise RuntimeError(f"kubectl get pods failed: {proc.stderr.strip()}")

    reaped: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    for pod in json.loads(proc.stdout).get("items", []):
        meta = pod.get("metadata") or {}
        name = meta.get("name", "")
        verdict, why = decide(pod, now, terminated_grace_seconds)
        entry = {
            "pod": name,
            "work_item_id": (meta.get("annotations") or {}).get(WORK_ITEM_ID, ""),
            "why": why,
        }
        if verdict == DELETE:
            if not dry_run:
                # --wait=false: a gVisor sandbox can take tens of seconds to
                # unwind, and the sweep must not hold the CronJob slot open.
                delete = _kubectl(
                    ["delete", "pod", name, "-n", namespace, "--wait=false", "--ignore-not-found"],
                    kubectl,
                )
                if delete.returncode != 0:
                    entry["error"] = delete.stderr.strip()
            reaped.append(entry)
        else:
            kept.append(entry)

    return {
        "checked_at": now.isoformat(),
        "namespace": namespace,
        "reaped": reaped,
        "kept": kept,
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect expired DSE sandbox Pods.")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--terminated-grace-seconds", type=int, default=DEFAULT_TERMINATED_GRACE_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = sweep(
            args.namespace,
            terminated_grace_seconds=args.terminated_grace_seconds,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
