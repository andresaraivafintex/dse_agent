"""WSD-E5-T1: Tier-2 evaluation suite (owner: WS-D). Really runs against the
real echo model; proves the harness reports pass and treats an unavailable
model as SKIP (not a failure)."""
from __future__ import annotations

from model_gateway_client.eval_suite import run_suite

ECHO = "eco/echo-model"


def test_echo_cases_pass():
    report = run_suite(models=[ECHO])
    assert report.failed == 0
    assert report.passed >= 3  # echo-determinism, echo-known-transform, echo-nonempty
    by_id = {r.case_id: r for r in report.results}
    assert by_id["echo-known-transform"].status == "passed"
    assert by_id["echo-determinism"].status == "passed"
    # cost/latency are collected
    assert by_id["echo-known-transform"].latency_ms >= 0.0


def test_unreachable_model_is_skipped_not_failed():
    report = run_suite(models=["bedrock/anthropic.claude-3-5-sonnet"])
    # no AWS in this session -> the case is SKIPPED, not FAILED (the suite stays ok)
    assert report.failed == 0
    assert report.skipped == 1
    assert report.ok


def test_report_serializes():
    report = run_suite(models=[ECHO])
    d = report.as_dict()
    assert "summary" in d and "results" in d
    assert d["summary"]["failed"] == 0
