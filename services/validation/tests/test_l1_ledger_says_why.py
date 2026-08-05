"""The L1 ledger has to record WHY a gate failed, not only which one did.

`l1_pipeline_run` carried `{"sast": "ERROR"}` and nothing else. Every finding
already holds a `detail` — "bandit timed out after 60s", "no lint command in the
trusted manifest", the failing assertion — and it was dropped at write time. A
gate reading ERROR with no reason anywhere in the system is a gate nobody can
debug; diagnosing one on the VPS cost two wrong guesses before anyone noticed
the reason was never persisted.
"""
from __future__ import annotations

from dse_contracts import GateStatus

from dse_validation.l1.pipeline import L1Finding


def _emitted(findings):
    """Build the details dict exactly as `run_l1_pipeline` does."""
    return {
        "checks": {f.check: f.status.value if f.status else None for f in findings},
        "failures": {
            f.check: (f.detail or "")[:600] for f in findings if not f.passed and f.detail
        },
    }


def test_a_failing_gate_carries_its_reason():
    findings = [
        L1Finding(check="sast", passed=False, status=GateStatus.ERROR,
                  detail="bandit timed out after 60s — no SAST verdict was produced"),
        L1Finding(check="lint", passed=False, status=GateStatus.NOT_CONFIGURED,
                  detail="no lint command in the trusted manifest abc123:.dse/validation.json"),
    ]

    d = _emitted(findings)

    assert d["checks"]["sast"] == "ERROR"
    assert "timed out after 60s" in d["failures"]["sast"]
    assert "no lint command" in d["failures"]["lint"]


def test_a_green_run_stays_small():
    """Only failures are carried, so a passing run does not bloat the row."""
    findings = [
        L1Finding(check="secret_scan", passed=True, status=GateStatus.PASS, detail="0 findings"),
        L1Finding(check="diff_budget", passed=True, status=GateStatus.PASS, detail="3 lines"),
    ]

    assert _emitted(findings)["failures"] == {}


def test_a_verbose_tool_cannot_bloat_the_row():
    findings = [L1Finding(check="test", passed=False, status=GateStatus.FAIL, detail="x" * 5000)]

    assert len(_emitted(findings)["failures"]["test"]) == 600
