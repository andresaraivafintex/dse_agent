"""wrapper→core boundary (found during the real run on 2026-07-22): the tests
call the core directly; when the core signature changes, the Activity wrapper
goes stale and ONLY breaks in production (L1 got stuck in an infinite retry with
"unexpected keyword argument 'base_branch'"). This test pins the boundary."""
from __future__ import annotations

import inspect


def test_l1_wrapper_matches_core_signature():
    from dse_validation.l1.pipeline import run_l1_pipeline_core
    sig = inspect.signature(run_l1_pipeline_core)
    # exactly the kwargs that _run_l1_pipeline assembles (activities.py) —
    # if the core changes without the wrapper (or vice versa), the bind blows up HERE.
    sig.bind(
        executor=object(),
        work_item_id="wi",
        tenant_id="t",
        plan=object(),
        base_sha="a" * 8,
        head_sha="b" * 8,
        target_dir=".",
        # the heartbeat's progress hook (item 1.2) crosses the same boundary
        on_step=lambda _stage: None,
    )
