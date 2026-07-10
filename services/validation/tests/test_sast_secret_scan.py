"""WSE-E1-T2 — SAST (bandit real) e secret-scan (scanner regex+entropia
próprio, real) rodando via LocalFakeSandbox contra arquivos de verdade."""
from __future__ import annotations

from dse_validation.l1.sast import sast_check
from dse_validation.l1.secret_scan import secret_scan_check


def test_sast_passes_on_clean_code(sandbox):
    finding = sast_check(sandbox)
    assert finding.check == "sast"
    assert finding.passed is True


def test_sast_flags_real_bandit_high_severity_issue(sandbox, git_repo):
    # B301/B302-style: uso de eval em input não confiável -> bandit HIGH/MEDIUM.
    (git_repo / "vuln.py").write_text(
        "import subprocess\n\n"
        "def run(cmd):\n"
        "    return subprocess.call(cmd, shell=True)\n"
    )
    finding = sast_check(sandbox, severity_gate="LOW")
    assert finding.check == "sast"
    assert finding.passed is False
    assert "achado" in finding.detail.lower()


def test_secret_scan_passes_on_clean_code(sandbox):
    finding = secret_scan_check(sandbox)
    assert finding.check == "secret_scan"
    assert finding.passed is True


def test_secret_scan_flags_aws_key(sandbox, git_repo):
    (git_repo / "config.py").write_text('AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    finding = secret_scan_check(sandbox)
    assert finding.passed is False
    assert "aws_access_key_id" in finding.detail.lower() or "AKIA" in finding.detail


def test_secret_scan_flags_high_entropy_assignment(sandbox, git_repo):
    (git_repo / "settings.py").write_text('api_key = "zQ8v3nR7pL2xT9wKfM4dC6hB1sJ5"\n')
    finding = secret_scan_check(sandbox)
    assert finding.passed is False


def test_secret_scan_ignores_placeholder_values(sandbox, git_repo):
    (git_repo / "settings_placeholder.py").write_text('password = "changeme"\ntoken = "your_key_here"\n')
    finding = secret_scan_check(sandbox)
    assert finding.passed is True
