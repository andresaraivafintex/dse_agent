"""The reaper decides whether a Pod that nothing owns is safe to delete. Getting
it wrong in one direction leaks 250m of CPU request; in the other it kills an
agent mid-turn. These pin both edges."""

from datetime import datetime, timedelta, timezone

from sandbox_runtime.reaper import DELETE, EXPIRES_AT, KEEP, WORK_ITEM_ID, decide

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def _pod(*, phase="Running", annotations=None, finished_at=None, deletion=False, created=None):
    meta = {"name": "dse-sbx-wi-abc", "annotations": annotations or {}}
    if deletion:
        meta["deletionTimestamp"] = NOW.isoformat()
    if created:
        meta["creationTimestamp"] = created
    status = {"phase": phase}
    if finished_at:
        status["containerStatuses"] = [{"state": {"terminated": {"finishedAt": finished_at}}}]
    return {"metadata": meta, "status": status}


def test_running_pod_past_its_expiry_is_collected():
    pod = _pod(annotations={EXPIRES_AT: (NOW - timedelta(minutes=1)).isoformat()})
    verdict, why = decide(pod, NOW)
    assert verdict == DELETE
    assert "expired" in why


def test_running_pod_before_its_expiry_is_kept():
    pod = _pod(annotations={EXPIRES_AT: (NOW + timedelta(hours=5)).isoformat()})
    assert decide(pod, NOW)[0] == KEEP


def test_pod_without_the_expiry_annotation_is_never_guessed_at():
    """A Pod from a build that predates the TTL stamp must not be collected:
    leaking one sandbox is cheaper than destroying live work."""
    verdict, why = decide(_pod(annotations={WORK_ITEM_ID: "wi_abc"}), NOW)
    assert verdict == KEEP
    assert EXPIRES_AT in why


def test_terminated_pod_is_held_for_the_inspection_grace_then_collected():
    just_died = _pod(phase="Failed", finished_at=(NOW - timedelta(seconds=60)).isoformat())
    assert decide(just_died, NOW, terminated_grace_seconds=900)[0] == KEEP

    long_dead = _pod(phase="Failed", finished_at=(NOW - timedelta(hours=3)).isoformat())
    assert decide(long_dead, NOW, terminated_grace_seconds=900)[0] == DELETE


def test_succeeded_pod_is_collected_even_without_an_expiry_annotation():
    """restartPolicy=Never makes Succeeded final — it can never do work again,
    so the missing-annotation caution does not apply."""
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


def test_a_malformed_expiry_is_treated_as_absent_not_as_expired():
    pod = _pod(annotations={EXPIRES_AT: "not-a-timestamp"})
    assert decide(pod, NOW)[0] == KEEP
