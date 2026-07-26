"""Foundation activation guard. Documents, as an executable test, the ONLY code
blocker to turning Teams on: `Platform.teams` in the frozen enum.

While not activated (the state of this session), `build_conversation_event` raises
`TeamsNotActivated` carrying the activation steps — a clean, explanatory failure
(P6), never an enum value fabricated outside the contract. If/when the foundation
is extended, the same test validates that the typed construction starts working.
"""
from __future__ import annotations

import pytest

from adapter_teams import events
from adapter_teams.platform_compat import TeamsNotActivated, is_activated, teams_platform
from .helpers import teams_activity


def test_teams_platform_reflects_foundation_state():
    if is_activated():
        assert teams_platform().value == "teams"
    else:
        with pytest.raises(TeamsNotActivated):
            teams_platform()


def test_build_conversation_event_gated_on_activation():
    act = teams_activity()
    if is_activated():
        ev = events.build_conversation_event(act, resolved_principal="usr_test_x")
        assert ev.platform.value == "teams"
        assert ev.source_ref == {"conversation_id": "19:channel_abc@thread.tacv2"}
        assert ev.content_snapshot == "fix the login bug"
        assert ev.signature_verified is True
    else:
        with pytest.raises(TeamsNotActivated):
            events.build_conversation_event(act, resolved_principal="usr_test_x")


def test_activation_error_message_names_the_steps():
    err = TeamsNotActivated()
    msg = str(err)
    assert "Platform.teams" in msg
    assert "activation.sql" in msg
