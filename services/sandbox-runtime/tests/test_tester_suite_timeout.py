"""A suite whose tests pass but whose process never exits used to be
indistinguishable from a suite that was still running: the only thing that ever
ended it was the 600s activity timeout, which Temporal reports as
TimeoutExpired with no output. The Tester could not see its own defect, so each
retry authored ANOTHER non-terminating file — six of them, one every ten
minutes, observed live on 2026-07-29.

These pin the two halves of the fix: the suite is bounded inside the Pod, and a
non-terminating suite is reported as such."""

from __future__ import annotations

import re

from sandbox_runtime import activities


def test_the_suite_runs_under_a_timeout_well_inside_the_activity_budget():
    """180s is the contract: comfortably above a real suite (the live target
    repo runs in ~4s) and far below the 600s activity timeout, so the activity
    always returns a RESULT instead of dying and being retried blind."""
    assert activities._SUITE_TIMEOUT_SECONDS == 180
    assert activities._SUITE_TIMEOUT_SECONDS < 600


def test_both_runners_are_wrapped_not_just_node():
    """A Python repo hangs the same way. The command is a shell string built
    inline against a live Pod, so there is no seam to call — reading the source
    is the honest check, and it is worth having: the pytest branch is the one a
    fix like this quietly forgets."""
    import inspect

    body = inspect.getsource(activities._tester_pod_sync)
    assert re.search(r"timeout -k \d+ \{_SUITE_TIMEOUT_SECONDS\} npm test", body), \
        "the npm path must run under timeout"
    assert re.search(r"timeout -k \d+ \{_SUITE_TIMEOUT_SECONDS\} python3 -m pytest", body), \
        "the pytest path must run under timeout too — it hangs the same way"


def test_the_authoring_prompt_demands_a_process_that_exits():
    """The durable half: the runner bound stops the bleeding, but the tests
    themselves have to close what they open."""
    p = activities._TEST_AUTHOR_PROMPT
    assert "MUST EXIT" in p
    assert "after(" in p
    assert "server.close" in p


def test_a_hung_suite_is_named_as_such_not_reported_as_a_failed_assertion():
    """rc=124 is `timeout`'s verdict. The Coder cannot fix an assertion that
    never ran, so the message has to say the process did not exit."""
    import inspect

    body = inspect.getsource(activities._tester_pod_sync)
    assert "suite_hung = returncode == 124" in body
    assert "DID NOT TERMINATE" in body


# ---------------------------------------------------------------------------
# "Why did this work item fail?" has to be answerable from the ledger. On the
# 2026-07-29 run it was not: tester_turn_completed audited returncode=1 and
# nothing else, and the assertion text existed only in the orchestrator pod's
# log, truncated to 300 characters and rotating away.
# ---------------------------------------------------------------------------


def test_the_failing_output_is_persisted_on_both_runtimes():
    import inspect

    for fn in (activities._tester_pod_sync, activities._run_tester_turn_impl):
        body = inspect.getsource(fn)
        if "tester_turn_completed" not in body:
            continue
        assert '"failure_output"' in body, (
            f"{fn.__name__} audits the tester result without the reason it failed"
        )


def test_the_audit_copy_is_bounded_and_smaller_than_the_coder_copy():
    """The Coder needs the full tail to fix the test; the ledger needs enough
    for a human to see the assertion. Unbounded audit details are how a hung
    poll loop wrote 16k rows once already."""
    assert activities._FAILURE_OUTPUT_AUDIT_CHARS < activities._FAILURE_OUTPUT_CHARS
    assert activities._FAILURE_OUTPUT_AUDIT_CHARS >= 800


def test_the_log_line_no_longer_cuts_the_assertion_off():
    """At 300 chars the cut landed mid-token and printed `e: 'suite'` — the
    actual failure never reached the log."""
    import inspect

    body = inspect.getsource(activities._tester_pod_sync)
    assert "%.300s" not in body
