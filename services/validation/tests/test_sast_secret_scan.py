"""WSE-E1-T2 — SAST (real bandit) and secret-scan (our own real regex+entropy
scanner) running through LocalFakeSandbox against actual files."""
from __future__ import annotations

from dse_contracts import GateStatus

from dse_validation.config import stage_timeout_ceiling_seconds
from dse_validation.l1.sast import sast_check
from dse_validation.l1.secret_scan import secret_scan_check
from dse_validation.sandbox_exec import ExecResult


class _RecordingSandbox:
    """Captures the timeout the check hands to the executor — the only place
    where these two numbers become observable."""

    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls: list[tuple[list[str], int]] = []

    def run(self, argv, cwd=None, timeout: int = 300) -> ExecResult:
        self.calls.append((argv, timeout))
        return ExecResult(argv=argv, returncode=0, stdout=self.stdout, stderr="")

    @property
    def timeouts(self) -> list[int]:
        """The timeouts of the SCAN calls.

        `sast_check` asks a cheap question first — does this repository contain
        any Python at all — before pointing bandit at it, so the executor now
        sees a `find` ahead of the scanner. That probe carries its own short
        clock and is not what these tests are about."""
        return [t for argv, t in self.calls if argv and argv[0] != "sh"]


def test_sast_passes_on_clean_code(sandbox):
    finding = sast_check(sandbox)
    assert finding.check == "sast"
    assert finding.passed is True


def test_sast_flags_real_bandit_high_severity_issue(sandbox, git_repo):
    # B301/B302-style: eval on untrusted input -> bandit HIGH/MEDIUM.
    (git_repo / "vuln.py").write_text(
        "import subprocess\n\n"
        "def run(cmd):\n"
        "    return subprocess.call(cmd, shell=True)\n"
    )
    finding = sast_check(sandbox, severity_gate="LOW")
    assert finding.check == "sast"
    assert finding.passed is False
    assert "finding" in finding.detail.lower()


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


def test_scan_timeouts_default_to_the_historical_numbers():
    sast_box = _RecordingSandbox('{"results": []}')
    secret_box = _RecordingSandbox('{"findings": []}')

    sast_check(sast_box)
    secret_scan_check(secret_box)

    assert sast_box.timeouts == [120]
    assert secret_box.timeouts == [60]


def test_scan_timeouts_are_reachable_from_the_platform_env(monkeypatch):
    monkeypatch.setenv("DSE_L1_SAST_TIMEOUT_SECONDS", "480")
    monkeypatch.setenv("DSE_L1_SECRET_SCAN_TIMEOUT_SECONDS", "300")
    sast_box = _RecordingSandbox('{"results": []}')
    secret_box = _RecordingSandbox('{"findings": []}')

    sast_check(sast_box)
    secret_scan_check(secret_box)

    assert sast_box.timeouts == [480]
    assert secret_box.timeouts == [300]


def test_scan_timeout_from_the_env_is_still_capped_by_the_platform_ceiling(monkeypatch):
    monkeypatch.setenv("DSE_L1_SAST_TIMEOUT_SECONDS", "99999")
    sast_box = _RecordingSandbox('{"results": []}')

    sast_check(sast_box)

    assert sast_box.timeouts == [stage_timeout_ceiling_seconds()]


def test_explicit_scan_timeout_still_wins(monkeypatch):
    monkeypatch.setenv("DSE_L1_SAST_TIMEOUT_SECONDS", "480")
    sast_box = _RecordingSandbox('{"results": []}')

    sast_check(sast_box, timeout=17)

    assert sast_box.timeouts == [17]


class _TimingOutSandbox(_RecordingSandbox):
    """The executor's answer when the clock kills the command: no stdout, and
    `timed_out` set. Every real executor in `sandbox_exec.py` returns this."""

    def run(self, argv, cwd=None, timeout: int = 300) -> ExecResult:
        self.timeouts.append(timeout)
        return ExecResult(argv=argv, returncode=-1, stdout="", stderr="", timed_out=True)


def test_a_timed_out_sast_is_an_error_not_a_clean_pass():
    """`json.loads("{}")` on a killed bandit yields zero findings, i.e. a GREEN
    security gate that never ran. Unreachable while 120s was frozen in the
    signature; now the budget comes from the environment and the manifest, so a
    short clock would BUY the pass."""
    finding = sast_check(_TimingOutSandbox(""))

    assert finding.passed is False
    assert finding.status is GateStatus.ERROR
    assert "timed out" in finding.detail


def test_a_timed_out_secret_scan_stays_an_error():
    finding = secret_scan_check(_TimingOutSandbox(""))

    assert finding.passed is False
    assert finding.status is GateStatus.ERROR
