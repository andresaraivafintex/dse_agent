"""The sweep in stranded.py was written for four abandoned rows and never
called — its own README said so. Five days later those rows were still there,
joined by six more, all showing in the console as work in progress. These pin
the caller, with the detection and action injected so no database is needed."""

from __future__ import annotations

import pytest

from ingest_gateway import stranded_sweep


@pytest.fixture
def fake(monkeypatch):
    state = {"found": [], "escalated": [], "calls": []}

    def _found(conn, *, tenant_id, idle_for_seconds, limit):
        state["calls"].append({"tenant_id": tenant_id, "idle": idle_for_seconds, "limit": limit})
        return state["found"]

    def _escalate(conn, *, work_item_id, tenant_id, idle_seconds, actor):
        state["escalated"].append(work_item_id)
        return work_item_id not in state.get("refuse", [])

    monkeypatch.setattr(stranded_sweep, "stranded_work_items", _found)
    monkeypatch.setattr(stranded_sweep, "escalate_stranded", _escalate)
    return state


def _row(wid: str) -> dict:
    return {"work_item_id": wid, "status": "implementing", "source": "slack",
            "source_ref": {}, "last_event_at": None, "idle_seconds": 99999}


def test_it_escalates_what_the_detector_found(fake):
    fake["found"] = [_row("wi_a"), _row("wi_b")]
    out = stranded_sweep.sweep(tenant_id="fintex-poc", conn=object())
    assert out["escalated"] == ["wi_a", "wi_b"]
    assert out["found"] == 2


def test_an_item_that_moved_on_is_reported_as_skipped_not_escalated(fake):
    """escalate_stranded returns False when the item woke up between detection
    and action. Counting that as escalated would put a lie in the trail."""
    fake["found"] = [_row("wi_a"), _row("wi_b")]
    fake["refuse"] = ["wi_b"]
    out = stranded_sweep.sweep(tenant_id="fintex-poc", conn=object())
    assert out["escalated"] == ["wi_a"]
    assert out["skipped"] == ["wi_b"]


def test_dry_run_touches_nothing(fake):
    fake["found"] = [_row("wi_a")]
    out = stranded_sweep.sweep(tenant_id="fintex-poc", dry_run=True, conn=object())
    assert out["escalated"] == [] and out["skipped"] == ["wi_a"]
    assert fake["escalated"] == []


def test_it_reads_the_right_key_and_would_have_crashed_on_the_wrong_one(fake):
    """Regression: the detector returns `work_item_id`, not `id`. Guessing that
    key fails on the first real sweep — the run nobody is watching."""
    fake["found"] = [_row("wi_only_work_item_id")]
    out = stranded_sweep.sweep(tenant_id="fintex-poc", conn=object())
    assert out["escalated"] == ["wi_only_work_item_id"]


def test_a_missing_tenant_is_refused_rather_than_guessed(monkeypatch):
    """The sweep writes terminal states. The wrong tenant means escalating
    somebody else's live work."""
    monkeypatch.delenv("DSE_TENANT_ID", raising=False)
    assert stranded_sweep.main([]) == 2


def test_the_idle_window_is_long_enough_not_to_catch_real_work(fake):
    """A coder turn runs minutes and the CI wait writes a row every 60s, so the
    threshold has to sit well above both."""
    assert stranded_sweep.DEFAULT_IDLE_SECONDS >= 3600
    stranded_sweep.sweep(tenant_id="t", conn=object())
    assert fake["calls"][0]["idle"] == stranded_sweep.DEFAULT_IDLE_SECONDS
