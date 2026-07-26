"""Activation bridge for the Teams adapter.

Intake is typed over `dse_contracts.Platform` (frozen foundation enum:
`slack|github|jira`). Activating Teams requires adding `teams` in the FOUNDATION
(packages/contracts + migration 0001):
  1. `Platform.teams` in the enum in `conversation_event.py` (additive, 1 line).
  2. relax the `work_items.source` and `identity_links.platform` CHECKs to
     include `'teams'` (see `activation.sql` — additive, apply in the activation
     migration).

This workstream (WS-A, Phase 4) does NOT edit `packages/*` nor migrations
0001-0019 (coexistence rule — 4 agents in parallel). So the adapter ships
COMPLETE and tested, yet "wired but not activated": the whole pipeline exists and
is exercised by the tests up to the exact point of the foundation blocker.
`is_activated()` detects at runtime whether the foundation has already been
extended (instead of hard-coding the result), so that turning Teams on is
literally the 1-line enum change + the activation migration — without touching
this service.
"""
from __future__ import annotations

from dse_contracts import Platform


class TeamsNotActivated(RuntimeError):
    """Raised when the pipeline tries to produce an artifact typed as
    `Platform.teams` but the foundation has not been extended yet. The message
    carries the activation steps (P6: clean, explanatory failure at the boundary,
    never a silent one)."""

    def __init__(self) -> None:
        super().__init__(
            "Teams adapter not enabled: add `Platform.teams` in "
            "packages/contracts/dse_contracts/conversation_event.py and apply "
            "services/adapter-teams/activation.sql (relaxes the CHECKs on "
            "work_items.source / identity_links.platform). Business/roadmap "
            "decision for Phase 4+ — see README."
        )


def teams_platform() -> Platform:
    """Returns `Platform.teams` if the foundation has already been extended;
    otherwise raises `TeamsNotActivated`. Never fabricates an enum value outside
    the contract."""
    try:
        return Platform("teams")
    except ValueError as exc:  # member does not exist yet in the frozen enum
        raise TeamsNotActivated() from exc


def is_activated() -> bool:
    try:
        Platform("teams")
        return True
    except ValueError:
        return False
