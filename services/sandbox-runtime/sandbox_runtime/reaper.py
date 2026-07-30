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
orchestrator is down. It has no other source to read: the CronJob's NetworkPolicy
allows egress to the API server and nothing else, so every judgement here is made
out of the Pod object itself.

Which is why the annotation being absent cannot mean "keep forever". Pods built
before the TTL stamp existed carry no `expires-at` and no `work-item-id`, and two
of them sat Running for days with nothing in the cluster able to judge them. Such
a Pod is aged against its own `creationTimestamp` instead, and identified by
inverting `pod_name_for` — the name is deterministic, so it doubles as a checksum
over the identity the Pod records elsewhere.

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

from .k8s_driver import DEFAULT_SANDBOX_TTL_SECONDS, pod_name_for

KEEP = "keep"
DELETE = "delete"

SANDBOX_SELECTOR = "app.kubernetes.io/managed-by=dse-sandbox"
EXPIRES_AT = "dse.fintex/expires-at"
WORK_ITEM_ID = "dse.fintex/work-item-id"
# Every build so far, including the ones that predate the annotations, passes the
# work item id to the runner in this variable. Unlike the name it is the FULL id.
WORK_ITEM_ENV = "DSE_WORK_ITEM_ID"

# A terminated sandbox is held this long before collection. A crashed sandbox is
# the one an operator most wants to `kubectl describe`, and deleting it the
# instant it dies destroys the only forensic surface there is (the worker's RBAC
# carries no pods/log).
DEFAULT_TERMINATED_GRACE_SECONDS = 900

# Lifetime given to a Pod with no readable `expires-at`, counted from its own
# creationTimestamp. Deliberately the SAME number the driver stamps rather than a
# second, tighter one: the fallback covers both a Pod from a build that predates
# the stamp and a live Pod whose annotation was stripped, and only the second
# case is dangerous. Anything shorter would collect a running sandbox before the
# deadline its own annotation would have promised, and a reaped sandbox is not
# rebuilt — the next turn fails to exec and the work item ends `failed`. It is
# imported rather than copied so the floor and the stamp cannot drift apart in
# the source; --fallback-ttl-seconds is there for a deployment that raises
# DSE_SANDBOX_TTL_SECONDS, which this CronJob does not receive.
DEFAULT_FALLBACK_TTL_SECONDS = DEFAULT_SANDBOX_TTL_SECONDS


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


def _work_item_id_from_name(name: str) -> str | None:
    """Invert pod_name_for, or None if it could not have produced `name`.

    Lossy: the real id is `wi_` + 64 hex = 67 chars and the name is capped at 63,
    so this recovers a PREFIX — enough to attribute the Pod, not enough to query
    with. `_` is the only character of a real id that pod_name_for rewrites.
    """
    prefix = "dse-sbx-"
    if not name.startswith(prefix):
        return None
    candidate = name[len(prefix):].replace("-", "_", 1)
    return candidate if candidate and pod_name_for(candidate) == name else None


def _work_item_id_from_env(pod: dict[str, Any]) -> str | None:
    for container in (pod.get("spec") or {}).get("containers") or []:
        for var in container.get("env") or []:
            if var.get("name") == WORK_ITEM_ENV and var.get("value"):
                return str(var["value"])
    return None


def reconcile_work_item_id(pod: dict[str, Any]) -> str | None:
    """The work item this Pod belongs to, or None if it cannot be established.

    Because pod_name_for is deterministic, the name is a checksum over the
    identity: a recorded id that does not rebuild the name is not this Pod's
    owner, and that disagreement is the one real doubt in this module — it says
    the object was not made by this driver. A missing annotation is not doubt of
    that kind, it is evidence of an old build, so the name alone is accepted.
    """
    name = (pod.get("metadata") or {}).get("name") or ""
    from_name = _work_item_id_from_name(name)
    if from_name is None:
        return None
    recorded = ((pod.get("metadata") or {}).get("annotations") or {}).get(
        WORK_ITEM_ID
    ) or _work_item_id_from_env(pod)
    if recorded:
        return recorded if pod_name_for(recorded) == name else None
    return from_name


def decide(
    pod: dict[str, Any],
    now: datetime,
    terminated_grace_seconds: int = DEFAULT_TERMINATED_GRACE_SECONDS,
    fallback_ttl_seconds: int = DEFAULT_FALLBACK_TTL_SECONDS,
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
        # No readable expiry — a Pod from a build that predates the TTL stamp, or
        # one whose annotation was stripped. Age it against its own
        # creationTimestamp: keeping it forever is not caution, it is the leak.
        work_item_id = reconcile_work_item_id(pod)
        if work_item_id is None:
            return KEEP, f"no {EXPIRES_AT} and no work item this Pod reconciles to"
        created = _parse_ts(meta.get("creationTimestamp"))
        if created is None:
            return KEEP, f"no {EXPIRES_AT} and no creationTimestamp to age {work_item_id} against"
        age = (now - created).total_seconds()
        if age >= fallback_ttl_seconds:
            return DELETE, (
                f"no {EXPIRES_AT}; {work_item_id} alive for {int(age)}s "
                f"(fallback TTL {fallback_ttl_seconds}s)"
            )
        return KEEP, f"no {EXPIRES_AT}; {work_item_id} within the {fallback_ttl_seconds}s fallback TTL"
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
    fallback_ttl_seconds: int = DEFAULT_FALLBACK_TTL_SECONDS,
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
        verdict, why = decide(pod, now, terminated_grace_seconds, fallback_ttl_seconds)
        entry = {
            "pod": name,
            # The annotation is blank on exactly the Pods that leak, so this
            # falls back to the identity recovered from the object. A line an
            # operator cannot correlate to a work item is not a report.
            "work_item_id": (meta.get("annotations") or {}).get(WORK_ITEM_ID)
            or reconcile_work_item_id(pod)
            or "",
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
    parser.add_argument("--fallback-ttl-seconds", type=int, default=DEFAULT_FALLBACK_TTL_SECONDS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = sweep(
            args.namespace,
            terminated_grace_seconds=args.terminated_grace_seconds,
            fallback_ttl_seconds=args.fallback_ttl_seconds,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
