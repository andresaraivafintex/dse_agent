"""Shared library: exactly 1 status comment/message per surface, edited
in-place, crash-consistent (WSA-E3-T2/E4-T2; reused by WSE-E3-T7 in the PR
finalizer). Comment-per-update is explicitly rejected.

Each adapter (Slack/GitHub/Jira) implements `CommentBackend` with the native
post/edit calls of its own API; this class handles the common part:
idempotency (one comment_ref per WorkItem+surface) and post-crash convergence.
"""
from __future__ import annotations

from typing import Protocol


class CommentBackend(Protocol):
    """Implemented by adapter-slack, adapter-github, (Jira in Phase 2)."""

    def post(self, surface_ref: dict, body: str) -> str:
        """Creates the initial comment/message. Returns an opaque comment_ref."""
        ...

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        """Edits the existing comment/message in place."""
        ...


class CommentStateStore(Protocol):
    """Persistence of the comment_ref per (work_item_id, surface) — usually a
    small table or a JSONB column on work_items; injected by the caller."""

    def get_ref(self, work_item_id: str, surface: str) -> str | None: ...
    def save_ref(self, work_item_id: str, surface: str, comment_ref: str) -> None: ...


class MutableCommentWriter:
    """Usage: `writer.upsert(work_item_id, surface_ref, body)` — it decides on
    its own whether this is the first post (create) or an edit (edits the ref
    already saved). Convergent after a crash: if the process died between
    `post()` and `save_ref()`, the next upsert call sees no saved ref and
    creates again — duplicating in that rare case is acceptable and documented
    (trading never losing the comment against never duplicating it on a crash
    inside exactly that window); every other call converges to editing the same
    ref.
    """

    def __init__(self, backend: CommentBackend, store: CommentStateStore, surface: str):
        self._backend = backend
        self._store = store
        self._surface = surface

    def upsert(self, work_item_id: str, surface_ref: dict, body: str) -> str:
        existing_ref = self._store.get_ref(work_item_id, self._surface)
        if existing_ref is None:
            comment_ref = self._backend.post(surface_ref, body)
            self._store.save_ref(work_item_id, self._surface, comment_ref)
            return comment_ref
        self._backend.edit(surface_ref, existing_ref, body)
        return existing_ref
