"""The L1 ledger records WHY a gate failed — without recording what it saw.

`l1_pipeline_run` carried `{"sast": "ERROR"}` and nothing else, so a failing
gate was undiagnosable; working one out on the VPS cost two wrong guesses.

Carrying `detail` is not the answer: it holds scanner output, compiler output
and matched source lines, and `audit_log` is append-only (0028's trigger),
refused by `retention.py` by design, and copied verbatim into
`console_rm.timeline_events.data`. A value written there can be rotated, never
scrubbed. So each BRANCH authors a value-free `summary`, and only that is
published.

These tests drive `run_l1_pipeline_core` and capture what it hands to
`audit_emit`. That is deliberate and was learned the hard way: the first version
of this file exercised only the private helper, so deleting the entire
`failures` block — the whole point of the change — left all six tests green. A
test that cannot fail when the feature is removed pins nothing.
"""
from __future__ import annotations

import pytest

from dse_contracts import GateStatus, L1Finding, PlanArtifact

from dse_validation.config import L1Config
from dse_validation.l1 import pipeline, plan_compliance, quality_checks, sast, secret_scan
from dse_validation.l1.pipeline import _NO_SUMMARY, _audit_safe_summary

#: Shaped like a real credential so a substring search for it means something.
PLANTED = "AKIAIOSFODNN7EXAMPLE-do-not-log-me"


@pytest.fixture
def audit_rows(monkeypatch):
    """Replaces only the SINK. The payload assembly under test is real code."""
    rows: list[dict] = []
    monkeypatch.setattr(pipeline, "audit_emit", lambda **kw: rows.append(kw))
    return rows


@pytest.fixture
def findings_are(monkeypatch):
    """Pins the pipeline's checks to chosen findings.

    The checks have their own tests; what is under test here is what the
    pipeline WRITES about them, so their output is this test's input."""

    def _install(*findings: L1Finding) -> None:
        by_check = {f.check: f for f in findings}

        def _for(check: str):
            return lambda *a, **k: by_check.get(
                check, L1Finding(check=check, passed=True, status=GateStatus.PASS)
            )

        for name in ("lint", "typecheck", "test", "build"):
            monkeypatch.setattr(quality_checks, f"{name}_check", _for(name))
        monkeypatch.setattr(sast, "sast_check", _for("sast"))
        monkeypatch.setattr(secret_scan, "secret_scan_check", _for("secret_scan"))
        monkeypatch.setattr(plan_compliance, "plan_compliance_findings", lambda *a, **k: [])

    return _install


def _failures(audit_rows) -> dict:
    pipeline.run_l1_pipeline_core(
        executor=None,
        work_item_id="wi_test",
        tenant_id="t_test",
        plan=PlanArtifact(work_item_id="wi_test", expected_files=[], no_code_change=True),
        base_sha="a" * 40,
        head_sha="b" * 40,
        cfg=L1Config(manifest_status=GateStatus.PASS, manifest_detail="test config"),
        persist=False,
    )
    assert audit_rows, "the pipeline emitted no audit row at all"
    return audit_rows[-1]["details"]


# ---------------------------------------------------------------------------
# The feature: a failing gate says why.
# ---------------------------------------------------------------------------
def test_a_failing_gate_carries_its_reason(audit_rows, findings_are):
    findings_are(
        L1Finding(
            check="typecheck",
            passed=False,
            status=GateStatus.FAIL,
            detail="12 type error(s)\nsrc/a.ts(3,9): error TS2322",
            summary="12 type error(s)",
        )
    )
    assert _failures(audit_rows)["failures"]["typecheck"] == "12 type error(s)"


def test_a_gate_that_errored_carries_its_reason(audit_rows, findings_are):
    """The incident this change exists for. It happened to `sast`, so excluding
    `sast` by name — as the first version did — reintroduced exactly the bug the
    change was written to fix."""
    findings_are(
        L1Finding(
            check="sast",
            passed=False,
            status=GateStatus.ERROR,
            detail="bandit timed out after 120s (scanning .); raise the timeout",
            summary="bandit timed out after 120s — no SAST verdict was produced",
        )
    )
    failures = _failures(audit_rows)["failures"]
    assert failures["sast"] == "bandit timed out after 120s — no SAST verdict was produced"


def test_a_run_with_nothing_failing_reports_no_failures(audit_rows, findings_are):
    findings_are()
    assert _failures(audit_rows)["failures"] == {}


# ---------------------------------------------------------------------------
# The safety property: `detail` never reaches the ledger, whatever the check.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "finding",
    [
        pytest.param(
            L1Finding(
                check="secret_scan",
                passed=False,
                status=GateStatus.FAIL,
                detail=f"2 possible secret(s) detected:\nsrc/cfg.py:4 {PLANTED}",
                summary="2 possible secret(s) detected",
            ),
            id="secret_scan renders the matched source line",
        ),
        pytest.param(
            L1Finding(
                check="secret_scan",
                passed=False,
                status=GateStatus.ERROR,
                detail=f'unexpected scanner output: [{{"match": "{PLANTED}"}}]',
                summary="the secret scanner produced output that could not be parsed",
            ),
            id="the scanner's json is one line, so the payload IS line one",
        ),
        pytest.param(
            L1Finding(
                check="sast",
                passed=False,
                status=GateStatus.FAIL,
                detail=f"1 SAST finding(s) >= MEDIUM:\n- B105 Possible hardcoded password: '{PLANTED}'",
                summary="1 SAST finding(s) >= MEDIUM",
            ),
            id="bandit B105 issue_text is the credential",
        ),
        pytest.param(
            L1Finding(
                check="lint",
                passed=False,
                status=GateStatus.ERROR,
                detail=f"lint command not found: ./ci/lint.sh (AWS_SECRET_ACCESS_KEY={PLANTED})",
                summary="lint command not found (exit 127)",
            ),
            id="lint exit 127 echoes stderr from a command the manifest chose",
        ),
        pytest.param(
            L1Finding(
                check="build",
                passed=False,
                status=GateStatus.FAIL,
                detail=f"build failed (exit=1)\n  at deploy(token={PLANTED})",
                summary="build failed (exit=1)",
            ),
            id="build tails the output, which prints whatever it prints",
        ),
    ],
)
def test_what_the_gate_saw_never_reaches_the_ledger(audit_rows, findings_are, finding):
    findings_are(finding)
    details = _failures(audit_rows)
    assert PLANTED not in repr(details)
    assert details["checks"][finding.check] == finding.status.value, (
        "the STATUS must still land — suppressing the value must not hide the gate"
    )


def test_a_branch_that_authors_no_summary_publishes_nothing_of_its_own(
    audit_rows, findings_are
):
    """Fail-closed. A future check that forgets to author a summary must say
    nothing rather than fall back to `detail` — that fallback is what made the
    denylist unsound."""
    findings_are(
        L1Finding(
            check="build",
            passed=False,
            status=GateStatus.FAIL,
            detail=f"build failed\n  token={PLANTED}",
        )
    )
    details = _failures(audit_rows)
    assert details["failures"]["build"] == _NO_SUMMARY
    assert PLANTED not in repr(details)


# ---------------------------------------------------------------------------
# Bounds — audit_log is append-only, so a bad write can never be amended.
# ---------------------------------------------------------------------------
def test_the_emitted_reason_is_bounded(audit_rows, findings_are):
    findings_are(
        L1Finding(check="build", passed=False, status=GateStatus.FAIL, summary="x" * 50_000)
    )
    assert len(_failures(audit_rows)["failures"]["build"]) == 600


def test_a_nul_byte_cannot_wedge_the_ledger(audit_rows, findings_are):
    """jsonb cannot represent U+0000: the ::jsonb cast raises, the insert fails,
    and the activity then fails identically on every retry."""
    findings_are(
        L1Finding(
            check="lint", passed=False, status=GateStatus.FAIL, summary="eslint crashed\x00 here"
        )
    )
    assert "\x00" not in _failures(audit_rows)["failures"]["lint"]


# ---------------------------------------------------------------------------
# The helper's own edges.
# ---------------------------------------------------------------------------
def test_a_multiline_summary_is_flattened_to_its_first_line():
    assert _audit_safe_summary("12 type error(s)\nsrc/a.ts(3,9)") == "12 type error(s)"


def test_an_absent_summary_is_the_empty_string():
    assert _audit_safe_summary(None) == ""
    assert _audit_safe_summary("") == ""


# ---------------------------------------------------------------------------
# Where the time went.
#
# `run_l1_pipeline` measured 638 seconds on the Angular testbed, of which 55
# were explainable — `npm ci` printed its own duration — and 583 were not. The
# heartbeat says which stage is RUNNING, but nothing durable said how long any
# of them took, so every proposal to make the gate faster was a guess about
# which stage to attack.
# ---------------------------------------------------------------------------
def test_every_finding_carries_what_it_cost(audit_rows, findings_are):
    findings_are(
        L1Finding(check="typecheck", passed=False, status=GateStatus.FAIL,
                  summary="12 type error(s)")
    )
    details = _failures(audit_rows)
    assert "durations" in details, "the ledger cannot say where the time went"
    assert set(details["durations"]) == set(details["checks"]), (
        "a stage reported a status but no duration"
    )
    assert all(isinstance(v, (int, float)) for v in details["durations"].values())


def test_a_slow_stage_is_visible_in_the_ledger(audit_rows, findings_are, monkeypatch):
    """The number has to come from the clock, not from a default that would
    make every stage look free."""
    import time as _t

    real = _t.monotonic
    ticks = iter([0.0, 4.0] + [0.0] * 40)

    def fake():
        try:
            return next(ticks)
        except StopIteration:
            return real()

    findings_are()
    monkeypatch.setattr(_t, "monotonic", fake)
    details = _failures(audit_rows)
    assert details["durations"]["lint"] == 4.0, details["durations"]
