"""The reaper decides whether a Pod that nothing owns is safe to delete. Getting
it wrong in one direction leaks 250m of CPU request; in the other it kills an
agent mid-turn. These pin both edges."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

from sandbox_runtime import reaper
from sandbox_runtime.k8s_driver import DEFAULT_SANDBOX_TTL_SECONDS, pod_name_for
from sandbox_runtime.reaper import (
    DELETE,
    EXPIRES_AT,
    KEEP,
    WORK_ITEM_ENV,
    WORK_ITEM_ID,
    decide,
    reconcile_work_item_id,
)

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

# A Pod that really leaked: created by rc.4, before either annotation existed,
# still Running two days later. Its name is what pod_name_for produced from the
# id, truncated at the 63-char limit.
LEAKED_ID = "wi_2202bae8463202d23502d7882f9c9734a49ae67f65fe8cb7e8d9bddf8051199e"
LEAKED_NAME = "dse-sbx-wi-2202bae8463202d23502d7882f9c9734a49ae67f65fe8cb7e8d9"


def _pod(
    *,
    phase="Running",
    annotations=None,
    finished_at=None,
    deletion=False,
    created=None,
    name="dse-sbx-wi-abc",
    env_work_item_id=None,
):
    meta = {"name": name, "annotations": annotations or {}}
    if deletion:
        meta["deletionTimestamp"] = NOW.isoformat()
    if created:
        meta["creationTimestamp"] = created
    status = {"phase": phase}
    if finished_at:
        status["containerStatuses"] = [{"state": {"terminated": {"finishedAt": finished_at}}}]
    pod = {"metadata": meta, "status": status}
    if env_work_item_id:
        pod["spec"] = {
            "containers": [{"env": [{"name": WORK_ITEM_ENV, "value": env_work_item_id}]}]
        }
    return pod


def _aged(seconds, **kwargs):
    return _pod(created=(NOW - timedelta(seconds=seconds)).isoformat(), **kwargs)


def test_running_pod_past_its_expiry_is_collected():
    pod = _pod(annotations={EXPIRES_AT: (NOW - timedelta(minutes=1)).isoformat()})
    verdict, why = decide(pod, NOW)
    assert verdict == DELETE
    assert "expired" in why


def test_running_pod_before_its_expiry_is_kept():
    pod = _pod(annotations={EXPIRES_AT: (NOW + timedelta(hours=5)).isoformat()})
    assert decide(pod, NOW)[0] == KEEP


def test_pod_that_never_carried_the_expiry_annotation_is_collected_once_it_is_old():
    """The leak this exists to close: two Pods from a pre-annotation build sat
    Running for days, and the old rule could only ever answer KEEP for them."""
    pod = _aged(DEFAULT_SANDBOX_TTL_SECONDS + 60, name=LEAKED_NAME, env_work_item_id=LEAKED_ID)
    verdict, why = decide(pod, NOW)
    assert verdict == DELETE
    assert LEAKED_ID in why


def test_pod_without_the_expiry_annotation_is_kept_while_it_is_still_young():
    """The annotation can also go missing from a Pod that is working right now
    (stripped, or a stamp disabled after the fact) — age is what decides."""
    pod = _aged(3600, name=LEAKED_NAME, env_work_item_id=LEAKED_ID)
    assert decide(pod, NOW)[0] == KEEP


def test_stripping_the_annotation_does_not_change_when_a_pod_is_collected():
    """The whole point of the fallback: whether the stamp is present must stop
    being the difference between a bounded lease and an unbounded one."""
    created = NOW - timedelta(seconds=DEFAULT_SANDBOX_TTL_SECONDS - 60)
    expires_at = (created + timedelta(seconds=DEFAULT_SANDBOX_TTL_SECONDS)).isoformat()
    stamped = _pod(created=created.isoformat(), annotations={EXPIRES_AT: expires_at})
    bare = _pod(created=created.isoformat())

    assert decide(stamped, NOW)[0] == KEEP
    assert decide(bare, NOW)[0] == KEEP

    just_after = NOW + timedelta(minutes=2)
    assert decide(stamped, just_after)[0] == DELETE
    assert decide(bare, just_after)[0] == DELETE


def test_a_malformed_expiry_falls_back_to_the_creation_timestamp():
    """Unreadable is not the same as "expired now", but it must not be the same
    as "immortal" either."""
    young = _aged(3600, annotations={EXPIRES_AT: "not-a-timestamp"})
    assert decide(young, NOW)[0] == KEEP

    old = _aged(DEFAULT_SANDBOX_TTL_SECONDS + 1, annotations={EXPIRES_AT: "not-a-timestamp"})
    assert decide(old, NOW)[0] == DELETE


def test_the_fallback_ttl_is_configurable_for_a_deployment_that_moved_the_stamp():
    pod = _aged(7200, name=LEAKED_NAME, env_work_item_id=LEAKED_ID)
    assert decide(pod, NOW, fallback_ttl_seconds=3600)[0] == DELETE
    assert decide(pod, NOW, fallback_ttl_seconds=10800)[0] == KEEP


def test_the_work_item_is_recovered_from_the_pod_name_alone():
    """Nothing on a pre-annotation Pod says which work item it belongs to except
    its name — and the name is deterministic, so it can be read back."""
    recovered = reconcile_work_item_id(_pod(name=LEAKED_NAME))
    assert recovered is not None
    assert LEAKED_ID.startswith(recovered)
    assert pod_name_for(recovered) == LEAKED_NAME


def test_the_full_work_item_id_is_recovered_when_the_name_had_to_truncate_it():
    """The name caps at 63 chars and a real id is 67, so the name alone cannot
    be queried with. Every build also writes the full id into the runner's env."""
    pod = _pod(name=LEAKED_NAME, env_work_item_id=LEAKED_ID)
    assert reconcile_work_item_id(pod) == LEAKED_ID


def test_a_pod_whose_recorded_work_item_contradicts_its_name_is_never_collected():
    """This is the one real doubt: pod_name_for could not have produced this
    name from this id, so the object was not made by this driver."""
    pod = _aged(
        DEFAULT_SANDBOX_TTL_SECONDS * 10,
        name=LEAKED_NAME,
        env_work_item_id="wi_0000000000000000000000000000000000000000000000000000000000000000",
    )
    assert reconcile_work_item_id(pod) is None
    assert decide(pod, NOW)[0] == KEEP


def test_a_pod_whose_name_this_driver_could_not_have_produced_is_never_collected():
    pod = _aged(DEFAULT_SANDBOX_TTL_SECONDS * 10, name="operator-debug-pod")
    assert reconcile_work_item_id(pod) is None
    verdict, why = decide(pod, NOW)
    assert verdict == KEEP
    assert "reconciles" in why


def test_terminated_pod_is_held_for_the_inspection_grace_then_collected():
    just_died = _pod(phase="Failed", finished_at=(NOW - timedelta(seconds=60)).isoformat())
    assert decide(just_died, NOW, terminated_grace_seconds=900)[0] == KEEP

    long_dead = _pod(phase="Failed", finished_at=(NOW - timedelta(hours=3)).isoformat())
    assert decide(long_dead, NOW, terminated_grace_seconds=900)[0] == DELETE


def test_succeeded_pod_is_collected_even_without_an_expiry_annotation():
    """restartPolicy=Never makes Succeeded final — it can never do work again,
    so it is collected on the grace clock, not on the fallback TTL."""
    pod = _pod(phase="Succeeded", finished_at=(NOW - timedelta(hours=1)).isoformat())
    assert decide(pod, NOW)[0] == DELETE


def test_terminated_pod_with_no_timestamps_falls_back_to_deletion():
    pod = _pod(phase="Failed")
    verdict, why = decide(pod, NOW)
    assert verdict == DELETE
    assert "no timestamp" in why


def test_pod_already_being_deleted_is_left_alone():
    pod = _pod(annotations={EXPIRES_AT: (NOW - timedelta(days=1)).isoformat()}, deletion=True)
    assert decide(pod, NOW)[0] == KEEP


def test_naive_and_zulu_timestamps_are_both_read_as_utc():
    naive = _pod(annotations={EXPIRES_AT: (NOW - timedelta(minutes=1)).replace(tzinfo=None).isoformat()})
    assert decide(naive, NOW)[0] == DELETE

    zulu = _pod(annotations={EXPIRES_AT: (NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")})
    assert decide(zulu, NOW)[0] == DELETE


def test_the_sweep_names_the_work_item_of_a_pod_that_never_carried_the_annotation(monkeypatch):
    """A collection an operator cannot trace back to a work item is not a report,
    and the annotation is blank on exactly the Pods that leak."""
    pod = _aged(DEFAULT_SANDBOX_TTL_SECONDS + 60, name=LEAKED_NAME, env_work_item_id=LEAKED_ID)
    calls: list[list[str]] = []

    def fake_kubectl(args, kubectl="kubectl"):
        calls.append(args)
        stdout = json.dumps({"items": [pod]}) if args[0] == "get" else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(reaper, "_kubectl", fake_kubectl)
    result = reaper.sweep("dse-sandboxes", now=NOW)

    assert result["kept"] == []
    assert [e["pod"] for e in result["reaped"]] == [LEAKED_NAME]
    assert [e["work_item_id"] for e in result["reaped"]] == [LEAKED_ID]
    assert ["delete", "pod", LEAKED_NAME, "-n", "dse-sandboxes", "--wait=false", "--ignore-not-found"] in calls


def test_the_sweep_still_prefers_the_annotation_when_the_pod_carries_one(monkeypatch):
    pod = _aged(
        3600,
        name=LEAKED_NAME,
        annotations={
            WORK_ITEM_ID: LEAKED_ID,
            EXPIRES_AT: (NOW + timedelta(hours=5)).isoformat(),
        },
    )
    monkeypatch.setattr(
        reaper,
        "_kubectl",
        lambda args, kubectl="kubectl": subprocess.CompletedProcess(
            args, 0, json.dumps({"items": [pod]}), ""
        ),
    )
    result = reaper.sweep("dse-sandboxes", now=NOW)

    assert result["reaped"] == []
    assert [e["work_item_id"] for e in result["kept"]] == [LEAKED_ID]
