"""WSF-E2-T3a — testes do scanner de secrets em texto plano.

Usa um repositório git temporário isolado (tmp_path) para não depender do
estado real do monorepo (que muda conforme os outros workstreams commitam).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCANNER = REPO_ROOT / "scripts" / "scan_for_plaintext_secrets.py"


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)


def _run_scanner(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCANNER), "--root", str(root)],
        capture_output=True, text=True, check=False,
    )


def test_flags_hardcoded_slack_bot_token(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "config.yaml").write_text("SLACK_BOT_TOKEN=xoxb-1234567890-abcdefghijklmnop\n")

    result = _run_scanner(tmp_path)

    assert result.returncode == 1
    assert "slack_bot_token" in result.stdout or "SLACK_" in result.stdout


def test_flags_aws_access_key(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "deploy.yml").write_text("aws_access_key_id: AKIAABCDEFGHIJKLMNOP\n")

    result = _run_scanner(tmp_path)

    assert result.returncode == 1
    assert "aws_access_key_id" in result.stdout


def test_does_not_flag_env_var_reference(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "litellm_config.yaml").write_text(
        "api_key: os.environ/OPENAI_API_KEY\nmaster_key: os.environ/LITELLM_MASTER_KEY\n"
    )

    result = _run_scanner(tmp_path)

    assert result.returncode == 0


def test_ignores_dot_env_example_file(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / ".env.example").write_text("SLACK_BOT_TOKEN=xoxb-example-placeholder-value-here\n")

    result = _run_scanner(tmp_path)

    assert result.returncode == 0


def test_ignores_known_dev_placeholder_postgres_dsn(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "compose.yml").write_text(
        "DSE_DATABASE_URL: postgresql://dse:dse_dev_only@localhost:5432/dse\n"
    )

    result = _run_scanner(tmp_path)

    assert result.returncode == 0


def test_flags_private_key_pem_block(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "app-key.yaml").write_text(
        "github_app_key: |\n  -----BEGIN RSA PRIVATE KEY-----\n  MIIEpAIBAAKCAQEA...\n"
    )

    result = _run_scanner(tmp_path)

    assert result.returncode == 1
    assert "private_key_pem" in result.stdout


def test_ignores_untracked_venv_directory(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv-fake/\n")
    venv_dir = tmp_path / ".venv-fake" / "lib"
    venv_dir.mkdir(parents=True)
    (venv_dir / "some_dep_config.yaml").write_text("api_key: AKIAABCDEFGHIJKLMNOP\n")

    result = _run_scanner(tmp_path)

    assert result.returncode == 0


def test_real_repo_scan_runs_and_returns_deterministic_exit_code():
    """Smoke test contra o monorepo real: o scanner deve rodar sem erro de
    execução (exit 0 ou 1 são ambos aceitáveis — depende do estado atual dos
    outros workstreams; o que este teste garante é que o script não quebra)."""
    result = _run_scanner(REPO_ROOT)
    assert result.returncode in (0, 1)
    assert "[scan_for_plaintext_secrets]" in result.stdout
