"""Closed vocabulary of failure classes (Phase 2, plan 09)."""
from __future__ import annotations

from dse_contracts import (
    FAIL_CLOSED_CLASSES,
    FAILURE_TYPE_PREFIX,
    FailureClass,
    failure_type,
    parse_failure_type,
)


def test_roundtrip_canonical_types():
    for kind in FailureClass:
        t = failure_type(kind)
        assert t == f"{FAILURE_TYPE_PREFIX}{kind.value}"
        assert parse_failure_type(t) is kind


def test_legacy_types_are_recognized_forever():
    # types already recorded in Temporal histories — removing them breaks replay
    assert parse_failure_type("ProviderBillingError") is FailureClass.provider_billing
    assert parse_failure_type("EgressFailClosed") is FailureClass.policy_fail_closed


def test_unknown_types_map_to_none():
    assert parse_failure_type(None) is None
    assert parse_failure_type("") is None
    assert parse_failure_type("ValueError") is None
    assert parse_failure_type("dse.failure.nonexistent") is None


def test_fail_closed_classes_vocabulary_is_closed():
    # infra_transient NEVER gets in: transient is a retry problem, not a refusal
    assert FAIL_CLOSED_CLASSES == {
        FailureClass.policy_fail_closed,
        FailureClass.budget_denied,
        FailureClass.provider_billing,
    }
    assert FailureClass.infra_transient not in FAIL_CLOSED_CLASSES
