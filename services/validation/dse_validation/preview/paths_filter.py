"""FR-20 — decisão UI-touching por paths-filter 100% DETERMINÍSTICO (P1: nunca
por modelo). `fnmatch` dos `files_changed` contra `ui_path_globs` (defaults no
contrato `TriggerPreviewInput`). PR backend-only => `skipped_backend_only`,
que conta como SUCESSO e nunca bloqueia.

Semântica de glob (documentada — fnmatch stdlib, P7):
  - `*` do fnmatch atravessa `/` (regex `.*`), então `ui/**` casa `ui/a/b.css`;
  - globs iniciados em `**/` também casam o path SEM prefixo de diretório
    (ex.: `**/*.css` casa `app.css` e `web/app.css`) — sem esse ajuste,
    `fnmatch("app.css", "**/*.css")` seria falso e um PR que só muda um CSS
    na raiz escaparia do preview.
"""
from __future__ import annotations

from fnmatch import fnmatch


def file_matches_glob(path: str, glob: str) -> bool:
    if fnmatch(path, glob):
        return True
    if glob.startswith("**/") and fnmatch(path, glob[3:]):
        return True
    return False


def is_ui_touching(files_changed: list[str], ui_path_globs: list[str]) -> bool:
    """True se QUALQUER arquivo mudado casa QUALQUER glob de UI."""
    return any(
        file_matches_glob(path, glob) for path in files_changed for glob in ui_path_globs
    )


def matching_files(files_changed: list[str], ui_path_globs: list[str]) -> list[str]:
    """Os arquivos que dispararam a decisão (evidência para o audit, P8)."""
    return [
        path for path in files_changed
        if any(file_matches_glob(path, glob) for glob in ui_path_globs)
    ]
