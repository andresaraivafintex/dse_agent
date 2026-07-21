"""Abstração de execução de comando dentro do sandbox.

`dse_contracts.activities.SandboxHandle` (dono: WS-C) só carrega os dados do
handle (sandbox_id, container_id, branch...) — não expõe um jeito de rodar
comando. WS-E precisa disso para o pipeline L1 (WSE-E1-T1: lint/typecheck/
test/build DENTRO do sandbox). Enquanto `services/sandbox-runtime` (WS-C) não
publica sua própria interface de execução, WS-E define aqui um Protocol
mínimo (`SandboxExecutor`) e duas implementações:

  - `DockerExecSandbox`  — real: `docker exec <container_id> ...`. Funciona
    assim que WS-C provisionar o container e popular `SandboxHandle.container_id`
    — não depende de nenhum código do WS-C além desse campo, que já está no
    contrato publicado.
  - `LocalFakeSandbox`   — modo local/teste: roda o mesmo comando via
    `subprocess` num diretório local (sem Docker), para permitir testar toda
    a lógica do pipeline L1 antes do WS-C entregar o runtime real.

Se `services/sandbox-runtime` publicar mais tarde uma interface de execução
mais rica (ex.: streaming, timeout nativo), troque só a implementação de
`DockerExecSandbox` — a interface `SandboxExecutor` e o pipeline L1 que a
consome não mudam.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class ExecResult(BaseModel):
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class SandboxExecutor(Protocol):
    def run(self, argv: list[str], cwd: str | None = None, timeout: int = 300) -> ExecResult: ...


class DockerExecSandbox:
    """Executa comandos via `docker exec` no container do sandbox (WS-C)."""

    def __init__(self, container_id: str, default_cwd: str = "/workspace/repo"):
        self.container_id = container_id
        self.default_cwd = default_cwd

    def run(self, argv: list[str], cwd: str | None = None, timeout: int = 300) -> ExecResult:
        full_argv = ["docker", "exec", "-w", cwd or self.default_cwd, self.container_id, *argv]
        try:
            proc = subprocess.run(
                full_argv, capture_output=True, text=True, timeout=timeout
            )
            return ExecResult(
                argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
            )
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                argv=argv,
                returncode=-1,
                stdout=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=(e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
                timed_out=True,
            )


class LocalFakeSandbox:
    """Modo local/teste: roda o comando diretamente num diretório local, sem
    Docker. Usado pelos testes de `dse_validation` para provar a lógica do
    pipeline L1 (parsing de findings, diff-budget, forbidden-paths) contra
    execuções REAIS de bandit/ruff/pytest/git (não mocka a ferramenta, só o
    isolamento de container que o WS-C ainda não publicou)."""

    def __init__(self, repo_dir: str | Path):
        self.repo_dir = Path(repo_dir)

    def run(self, argv: list[str], cwd: str | None = None, timeout: int = 300) -> ExecResult:
        workdir = Path(cwd) if cwd else self.repo_dir
        try:
            proc = subprocess.run(
                argv, cwd=str(workdir), capture_output=True, text=True, timeout=timeout
            )
            return ExecResult(
                argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
            )
        except FileNotFoundError as e:
            return ExecResult(argv=argv, returncode=127, stdout="", stderr=str(e))
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                argv=argv,
                returncode=-1,
                stdout=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=(e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
                timed_out=True,
            )


def executor_for_handle(sandbox_handle, repo_dir: str = "/workspace/repo") -> SandboxExecutor:
    """Resolve o executor real a partir de um `SandboxHandle` (WS-C). Usado
    pelo wrapper de Activity (`activities.py`) — os testes chamam a lógica
    core diretamente com um `LocalFakeSandbox` injetado, sem passar por aqui.
    """
    # S7 (Fase 5) — modo IN-PROCESS do PoC. Quando o substrato do agente roda no
    # processo do control-plane (claude-agent-sdk no orchestrator, não DENTRO do
    # container efêmero), o código real vive no workspace LOCAL
    # (`$DSE_SANDBOX_STATE_DIR/<wi>/workspace`) — o clone, o Coder e o checkpoint
    # rodam todos ali. O `/workspace` do container fica VAZIO (docker-out-of-
    # docker: o daemon monta um path do host que não corresponde ao path interno
    # do orchestrator). Logo L1 e finalize (git push) TÊM que operar nesse
    # workspace local, via subprocesso. PRODUÇÃO (agente DENTRO do container)
    # mantém o DockerExec — este ramo é gated por env e é OFF por default, então
    # não muda o comportamento de produção nem dos testes do WS-C/WS-E.
    if os.environ.get("DSE_SANDBOX_INPROCESS") == "1":
        wi = getattr(sandbox_handle, "work_item_id", None)
        if not wi:
            raise RuntimeError("DSE_SANDBOX_INPROCESS=1 exige SandboxHandle com work_item_id")
        state_dir = os.environ.get("DSE_SANDBOX_STATE_DIR", "/tmp/dse-sandboxes")
        local_ws = Path(state_dir) / wi / "workspace"
        return LocalFakeSandbox(local_ws)
    if getattr(sandbox_handle, "container_id", None):
        return DockerExecSandbox(sandbox_handle.container_id, default_cwd=repo_dir)
    raise RuntimeError(
        "SandboxHandle sem container_id — WS-C ainda não provisionou o sandbox real; "
        "use LocalFakeSandbox explicitamente em testes/dev."
    )
