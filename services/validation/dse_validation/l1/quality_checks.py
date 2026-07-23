"""WSE-E1-T1 — gates determinísticos in-sandbox: lint, typecheck, test, build.

Cada check roda o comando configurado (dse_validation.config.L1Config) via um
`SandboxExecutor` (dentro do sandbox de verdade quando WS-C entregar o
runtime; via `LocalFakeSandbox` em teste/dev) e faz parsing ESTRUTURADO do
resultado — nunca apenas o exit code cru — porque o exit code sozinho não diz
"quantos problemas" nem dá evidência legível para o `L1Finding.detail`
(P8: evidência sobre asserção).
"""
from __future__ import annotations

import re

from dse_contracts import GateStatus, L1Finding

from dse_validation.config import L1Config
from dse_validation.sandbox_exec import ExecResult, SandboxExecutor

_MAX_DETAIL_LINES = 40


def _tail(text: str, max_lines: int = _MAX_DETAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    omitted = len(lines) - max_lines
    return "\n".join(lines[-max_lines:]) + f"\n... ({omitted} linha(s) anterior(es) omitida(s))"


def _run(executor: SandboxExecutor, argv: list[str], timeout: int) -> ExecResult:
    return executor.run(argv, timeout=timeout)


def _not_configured(check: str, cfg: L1Config) -> L1Finding:
    return L1Finding(
        check=check,
        passed=False,
        status=GateStatus.NOT_CONFIGURED,
        detail=f"comando {check} ausente no manifesto confiável {cfg.source}",
    )


def lint_check(executor: SandboxExecutor, cfg: L1Config) -> L1Finding:
    if not cfg.lint_cmd:
        return _not_configured("lint", cfg)
    result = _run(executor, cfg.lint_cmd, cfg.timeout_seconds)
    # ruff/flake8: 1 linha por issue no formato "path:line:col: CODE msg".
    issue_lines = [ln for ln in result.stdout.splitlines() if re.match(r"^\S+:\d+:\d+:\s", ln)]
    passed = result.ok and len(issue_lines) == 0
    if result.timed_out:
        detail = f"timeout após {cfg.timeout_seconds}s rodando {' '.join(cfg.lint_cmd)}"
        status = GateStatus.ERROR
    elif result.returncode == 127:
        detail = f"comando de lint não encontrado: {' '.join(cfg.lint_cmd)} ({result.stderr.strip()})"
        passed = False
        status = GateStatus.ERROR
    else:
        n = len(issue_lines)
        detail = f"{n} issue(s) de lint" if n else "sem issues de lint"
        detail += "\n" + _tail(result.stdout or result.stderr)
        status = GateStatus.PASS if passed else GateStatus.FAIL
    return L1Finding(check="lint", passed=passed, status=status, detail=detail)


def typecheck_check(executor: SandboxExecutor, cfg: L1Config) -> L1Finding:
    if not cfg.typecheck_cmd:
        return _not_configured("typecheck", cfg)
    result = _run(executor, cfg.typecheck_cmd, cfg.timeout_seconds)
    error_lines = [ln for ln in result.stdout.splitlines() if ": error:" in ln]
    if result.returncode == 127:
        return L1Finding(
            check="typecheck",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"comando de typecheck não encontrado: {' '.join(cfg.typecheck_cmd)}",
        )
    passed = result.ok
    n = len(error_lines)
    detail = f"{n} erro(s) de tipo" if n else "sem erros de tipo"
    if result.timed_out:
        detail = f"timeout após {cfg.timeout_seconds}s"
        passed = False
        status = GateStatus.ERROR
    else:
        status = GateStatus.PASS if passed else GateStatus.FAIL
    detail += "\n" + _tail(result.stdout or result.stderr)
    return L1Finding(check="typecheck", passed=passed, status=status, detail=detail)


_PYTEST_SUMMARY_RE = re.compile(
    r"(?P<passed>\d+) passed"
    r"(?:, (?P<failed>\d+) failed)?"
    r"(?:, (?P<errors>\d+) error)?",
)


def test_check(executor: SandboxExecutor, cfg: L1Config) -> L1Finding:
    if not cfg.test_cmd:
        return _not_configured("test", cfg)
    result = _run(executor, cfg.test_cmd, cfg.timeout_seconds)
    if result.returncode == 127:
        return L1Finding(
            check="test",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"comando de teste não encontrado: {' '.join(cfg.test_cmd)}",
        )
    m = _PYTEST_SUMMARY_RE.search(result.stdout) or _PYTEST_SUMMARY_RE.search(result.stderr)
    summary = m.group(0) if m else None
    passed = result.ok
    if result.timed_out:
        detail = f"timeout após {cfg.timeout_seconds}s executando testes — L1 falha limpa (P6), não trunca"
        status = GateStatus.ERROR
    elif summary:
        detail = f"resumo: {summary}"
        status = GateStatus.PASS if passed else GateStatus.FAIL
    else:
        detail = "exit code " + str(result.returncode) + " (resumo pytest não encontrado no output)"
        status = GateStatus.PASS if passed else GateStatus.FAIL
    detail += "\n" + _tail(result.stdout or result.stderr)
    return L1Finding(check="test", passed=passed, status=status, detail=detail)


def build_check(executor: SandboxExecutor, cfg: L1Config) -> L1Finding:
    if not cfg.build_cmd:
        return _not_configured("build", cfg)
    result = _run(executor, cfg.build_cmd, cfg.timeout_seconds)
    if result.returncode == 127:
        return L1Finding(
            check="build",
            passed=False,
            status=GateStatus.ERROR,
            detail=f"comando de build não encontrado: {' '.join(cfg.build_cmd)}",
        )
    passed = result.ok
    detail = "build ok" if passed else f"build falhou (exit={result.returncode})"
    if result.timed_out:
        detail = f"timeout após {cfg.timeout_seconds}s"
        status = GateStatus.ERROR
    else:
        status = GateStatus.PASS if passed else GateStatus.FAIL
    detail += "\n" + _tail(result.stdout or result.stderr)
    return L1Finding(check="build", passed=passed, status=status, detail=detail)
