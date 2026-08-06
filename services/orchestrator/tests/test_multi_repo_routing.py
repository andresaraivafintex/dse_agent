"""Routing one request to the repositories it actually needs.

Two properties matter more than accuracy, and they are the ones tested here.

The failure modes are ASYMMETRIC. An unnecessary pull request is cheap — a human
closes it. A missed repository ships half a feature that looks finished, and
nobody notices until it is in front of a customer. So the router is asked to
include when in doubt, and everything it returns is clamped to what the tenant
actually has.

And a sibling id must never alias. `pod_name_for` is `f"dse-sbx-{slug}"[:63]`
while a work_item_id is already 67 characters, so the last twelve are ALREADY
discarded — suffixing would put two sandboxes in one Pod, with certainty. Three
other call sites truncate the same way.
"""
from __future__ import annotations

import hashlib

import pytest

from dse_orchestrator.local_activities import sibling_work_item_id


@pytest.fixture(autouse=True)
def _require_postgres():
    """Overrides the suite-wide fixture. Everything here is pure arithmetic over
    a string; requiring a database to check that two hashes differ would mean
    the collision guard silently stops running wherever the infra is not up —
    which is exactly where somebody would change the id shape."""
    yield

FE = "andresaraivafintex/bmo-fee-calculator-fe-dse"
BE = "andresaraivafintex/bmo-fee-calculator-be-dse"
EVENT = "slack:C123:1700000000.0001"


def _pod_name(work_item_id: str) -> str:
    """The real rule, copied from k8s_driver.pod_name_for."""
    return f"dse-sbx-{work_item_id.lower()}"[:63].rstrip("-")


def test_two_siblings_never_share_a_pod():
    a, b = sibling_work_item_id(EVENT, FE), sibling_work_item_id(EVENT, BE)
    assert a != b
    assert _pod_name(a) != _pod_name(b), "two sandboxes would fight over one Pod"


def test_a_sibling_id_has_the_same_shape_as_a_normal_one():
    """Same prefix, same length — every downstream truncation behaves as it does
    for a single-repo item."""
    normal = "wi_" + hashlib.sha256(EVENT.encode()).hexdigest()
    sib = sibling_work_item_id(EVENT, FE)
    assert len(sib) == len(normal) == 67
    assert sib.startswith("wi_")


def test_siblings_diverge_early_enough_for_every_truncation_site():
    """The preview namespace truncates at 63 (`argocd.py:79`), its labels at 63
    (`:175`), and the preview image tag at 12 (`pr_image.py:133`)."""
    a, b = sibling_work_item_id(EVENT, FE), sibling_work_item_id(EVENT, BE)
    first_difference = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
    assert first_difference < 12, f"aliases in the image tag: diverge at {first_difference}"


def test_the_same_request_always_derives_the_same_ids():
    """This is what makes the fan-out safe to retry: the UNIQUE constraints on
    `work_items.idempotency_key` and `ingest_events.event_id` turn a replay into
    a no-op rather than a duplicate work item."""
    assert sibling_work_item_id(EVENT, FE) == sibling_work_item_id(EVENT, FE)


def test_a_different_request_to_the_same_repo_is_a_different_item():
    assert sibling_work_item_id(EVENT, FE) != sibling_work_item_id("slack:C123:1700000000.0002", FE)


# ---------------------------------------------------------------------------
# The candidate set is every repository the tenant HAS.
#
# `repo_bindings` is not that: it has one row per BINDING, so a repository
# nobody bound to a channel is invisible in it. Deleting one Slack channel
# binding took the frontend out of the candidate list entirely, the router
# answered "the tenant has a single repository", and every request went to the
# backend — including the one that was supposed to demonstrate routing to both.
# ---------------------------------------------------------------------------
def test_the_candidate_query_reads_both_tables():
    """Pins the union. A router that draws only from `repo_bindings` is one
    deleted binding away from silently routing everything to one repo."""
    import inspect

    from dse_orchestrator import local_activities

    # The SQL moved into `dse_contracts.repos` when the ingest gateway turned
    # out to carry a THIRD copy of this same question, and its copy — which runs
    # FIRST — had not been fixed. Asserting the literal in this function's body
    # pinned the spelling; what matters is the union, wherever it lives.
    src = inspect.getsource(local_activities._route_repos_sync)
    assert "TENANT_REPO_CATALOGUE_SQL" in src, (
        "the router stopped asking the shared question and grew its own copy again"
    )

    from dse_contracts.repos import TENANT_REPO_CATALOGUE_SQL

    assert "repo_profiles" in TENANT_REPO_CATALOGUE_SQL, (
        "the router cannot see a repo nobody bound"
    )
    assert "repo_bindings" in TENANT_REPO_CATALOGUE_SQL, (
        "a repo bound but unprofiled would vanish"
    )
    assert "UNION ALL" in TENANT_REPO_CATALOGUE_SQL


def test_a_single_repo_tenant_short_circuits():
    """No decision to make, and a model call would be waste — but the reason
    must say so, because 'single repository' appearing when the tenant has two
    is exactly the symptom that hid this bug."""
    import inspect

    from dse_orchestrator import local_activities

    src = inspect.getsource(local_activities._route_repos_sync)
    assert "the tenant has a single repository" in src
    assert "len(candidates) < 2" in src


# ---------------------------------------------------------------------------
# The prompt decides by "what must I EDIT", not by "what is involved".
#
# The first version told the model to include a repository when in doubt, and
# leaned on the asymmetry between a needless PR and a missed one. Measured
# against the real gateway, all three demo sentences routed to BOTH repos — the
# model simply wrote a justification for including each time. "Show a badge for
# data the API already returns" came back as "the frontend displays it and the
# backend must supply it", which is true and is not an edit.
# ---------------------------------------------------------------------------
def test_the_prompt_asks_what_must_be_edited():
    from dse_orchestrator.local_activities import _ROUTER_PROMPT

    assert "EDITING A FILE" in _ROUTER_PROMPT
    assert "cannot name the edit" in _ROUTER_PROMPT, (
        "without this the model includes a repo it merely reasoned about"
    )


def test_the_prompt_names_the_two_ways_of_over_including():
    """Both were observed, not imagined: adding the backend because the data
    comes from it, and adding the frontend because a user eventually sees the
    result."""
    from dse_orchestrator.local_activities import _ROUTER_PROMPT

    # Normalised: the prompt wraps, and a test that pins line breaks pins the
    # formatting rather than the instruction.
    flat = " ".join(_ROUTER_PROMPT.split())
    assert "Do not add the backend because the data comes from it" in flat
    assert "Do not add the frontend because a user will eventually see the result" in flat


def test_the_bias_is_a_tiebreak_and_not_a_default():
    from dse_orchestrator.local_activities import _ROUTER_PROMPT

    assert "Do not use it as a default" in _ROUTER_PROMPT
