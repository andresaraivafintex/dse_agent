"""The L1 ledger records WHY a gate failed — without recording the secret.

`l1_pipeline_run` carried `{"sast": "ERROR"}` and nothing else. Every finding
already holds a `detail`, and it was dropped when the audit row was built, so a
failing gate was undiagnosable; working one out on the VPS cost two wrong
guesses.

Carrying the detail verbatim is not the answer either. `secret_scan` renders the
matched SOURCE LINE, and bandit's B105/B106/B107 issue_text is literally
"Possible hardcoded password: '<value>'". audit_log is append-only (0028's
trigger), `retention.py` refuses audit_log targets by design, and the console
projector copies `details` verbatim into `console_rm.timeline_events.data` — a
credential written there could only be rotated, never scrubbed.

So: the summary line, never from the secret-bearing checks, NUL-stripped
(jsonb cannot represent \\x00, and a raw NUL from tool output would wedge the
ledger write and the L1 activity with it).
"""
from __future__ import annotations

from dse_validation.l1.pipeline import _SECRET_BEARING_CHECKS, _audit_safe_detail


def test_the_summary_line_survives_because_it_is_the_diagnostic():
    assert _audit_safe_detail(
        "12 type error(s)\nsrc/a.ts:4 error TS2345: Argument of type ..."
    ) == "12 type error(s)"
    assert _audit_safe_detail(
        "no lint command in the trusted manifest abc123:.dse/validation.json"
    ).startswith("no lint command")


def test_the_payload_after_it_does_not():
    """The tail is the last 40 lines of build output: absolute sandbox paths,
    assertion diffs printing the values under test, env values."""
    d = _audit_safe_detail("3 test(s) failed\nAssertionError: expected 'sk-live-xyz'")
    assert d == "3 test(s) failed"
    assert "sk-live" not in d


def test_the_two_checks_whose_findings_are_the_secret_are_excluded():
    assert _SECRET_BEARING_CHECKS == {"secret_scan", "sast"}


def test_a_nul_byte_cannot_wedge_the_ledger():
    """jsonb has no representation for \\x00; the ::jsonb cast raises, the audit
    insert fails, and the L1 activity then fails the same way on every retry.
    Tool output is captured with text=True, so a NUL arrives as a character."""
    assert "\x00" not in _audit_safe_detail("build failed\x00\nrest")


def test_it_is_bounded():
    assert len(_audit_safe_detail("x" * 5000)) == 600


def test_no_detail_is_empty_not_none():
    assert _audit_safe_detail(None) == ""
    assert _audit_safe_detail("") == ""
