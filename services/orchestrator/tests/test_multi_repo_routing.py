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
