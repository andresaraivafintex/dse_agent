
import pytest
from dse_contracts import (
    Actor,
    ConversationEvent,
    EventKind,
    Platform,
    WorkItemStatus,
    to_public_status,
)


def test_conversation_event_build_is_deterministic_for_dedup():
    kwargs = dict(
        platform=Platform.slack,
        thread_key="C123:171234.5678",
        message_id="171234.5678",
        kind=EventKind.task_request,
        source_ref={"channel": "C123", "thread_ts": "171234.5678"},
        actor=Actor(platform_user_id="U1"),
        content_snapshot="@fintex-dse fix the flaky test",
        signature_verified=True,
    )
    ev1 = ConversationEvent.build(**kwargs)
    ev2 = ConversationEvent.build(**kwargs)
    assert ev1.event_id == ev2.event_id, "the same platform+thread+message must collide (dedup)"

    ev3 = ConversationEvent.build(**{**kwargs, "message_id": "other"})
    assert ev3.event_id != ev1.event_id


def test_conversation_event_is_frozen():
    ev = ConversationEvent.build(
        platform=Platform.github,
        thread_key="acme/repo#42",
        message_id="c1",
        kind=EventKind.review_comment,
        source_ref={"repo": "acme/repo", "issue_number": 42},
        actor=Actor(platform_user_id="gh:bob"),
        content_snapshot="please fix the typo",
        signature_verified=True,
    )
    with pytest.raises(Exception):
        ev.content_snapshot = "tampered"  # type: ignore[misc]


def test_every_internal_status_has_a_public_projection():
    # Key WSA-E1-T4 regression: adding a WorkItemStatus without updating the
    # public map must break this test, not silently leak "None".
    for status in WorkItemStatus:
        projected = to_public_status(status)
        assert projected in ("running", "blocked", "done", "failed")
