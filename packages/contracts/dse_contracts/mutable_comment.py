"""Biblioteca compartilhada: exatamente 1 comentário/mensagem de status por
surface, editado in-place, crash-consistent (WSA-E3-T2/E4-T2; reutilizada por
WSE-E3-T7 no PR finalizer). Comment-per-update é explicitamente rejeitado.

Cada adapter (Slack/GitHub/Jira) implementa `CommentBackend` com as chamadas
nativas de post/edit da própria API; esta classe cuida da parte comum:
idempotência (um comment_ref por WorkItem+surface) e convergência pós-crash.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol


class CommentBackend(Protocol):
    """Implementado por adapter-slack, adapter-github, (Jira na Fase 2)."""

    def post(self, surface_ref: dict, body: str) -> str:
        """Cria o comentário/mensagem inicial. Retorna um comment_ref opaco."""
        ...

    def edit(self, surface_ref: dict, comment_ref: str, body: str) -> None:
        """Edita in-place o comentário/mensagem existente."""
        ...


class CommentStateStore(Protocol):
    """Persistência do comment_ref por (work_item_id, surface) — normalmente uma
    tabela pequena ou uma coluna JSONB em work_items; injetada pelo caller."""

    def get_ref(self, work_item_id: str, surface: str) -> str | None: ...
    def save_ref(self, work_item_id: str, surface: str, comment_ref: str) -> None: ...


class MutableCommentWriter:
    """Uso: `writer.upsert(work_item_id, surface_ref, body)` — decide sozinho
    se é o primeiro post (cria) ou uma edição (edita o ref já salvo). Convergente
    após crash: se o processo morreu entre `post()` e `save_ref()`, a próxima
    chamada de upsert detecta ausência de ref salvo e cria de novo — duplicar
    nesse caso raro é aceitável e documentado (favor de nunca perder o comentário
    sobre nunca duplicá-lo em um crash exatamente nessa janela); todas as demais
    chamadas convergem para edição do mesmo ref.
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
