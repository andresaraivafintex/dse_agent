"""O gate que faltou na rc.39: o Dockerfile do agent-runner VENDORIZA
`scoped_git.py` como `_scoped_git.py` e `workspace_hygiene.py` como
`_workspace_hygiene.py` dentro do pacote `agent_runner`. Um import relativo
`from .scoped_git import ...` resolve no worker (pacote `sandbox_runtime`) e
NÃO resolve no Pod — foi exatamente assim que o `--op post_turn` morreu com
ModuleNotFoundError em produção e `run_coder_turn` entrou em retry infinito
(casos 1-2 de 2026-08-07, attempts 8/7, 112 erros em 25 min de log).

Este teste reproduz o vendoring do Dockerfile num pacote temporário e importa
os módulos exatamente como o Pod os vê. Vermelho na rc.39; o fix é o fallback
try/except já usado pelo `postturn.py`.
"""
from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "sandbox_runtime"


def test_workspace_hygiene_imports_in_the_vendored_layout(tmp_path):
    pkg = tmp_path / "vendored_probe_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # espelha o COPY-com-rename do Dockerfile, byte a byte
    shutil.copy(_SRC / "scoped_git.py", pkg / "_scoped_git.py")
    shutil.copy(_SRC / "workspace_hygiene.py", pkg / "_workspace_hygiene.py")

    sys.path.insert(0, str(tmp_path))
    try:
        for mod in ("vendored_probe_pkg._scoped_git", "vendored_probe_pkg._workspace_hygiene"):
            sys.modules.pop(mod, None)
        sys.modules.pop("vendored_probe_pkg", None)
        hygiene = importlib.import_module("vendored_probe_pkg._workspace_hygiene")
        # a guarda tem que chegar junto — é ela que o import quebrado derrubava
        assert hygiene.NO_CUSTOMER_HOOKS[0] == "-c"
        assert hygiene.NO_CUSTOMER_HOOKS[1].startswith("core.hooksPath=")
    finally:
        sys.path.remove(str(tmp_path))
        for mod in list(sys.modules):
            if mod.startswith("vendored_probe_pkg"):
                sys.modules.pop(mod, None)
