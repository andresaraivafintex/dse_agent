"""bandit reads Python. A repository without any is not a finding.

Real incident, 2026-08-06 on the Angular testbed: with lint, typecheck, test
and build all green, the work item still failed because `bandit -r .` found no
targets, exited 1 and printed no JSON — which the gate reported as
`bandit did not produce valid JSON (exit=1)`. The change was a CONTRIBUTING.md;
the repository is TypeScript. The gate failed the work item for the crime of
not being a Python project.
"""
from __future__ import annotations

import subprocess

from dse_contracts import GateStatus

from dse_validation.l1.sast import sast_check
from dse_validation.sandbox_exec import ExecResult


class _RealShell:
    """Runs the probe for real against `root`, and records anything else it is
    asked to run so the test can prove bandit was never invoked."""

    def __init__(self, root, bandit=None):
        self.root = str(root)
        self.bandit = bandit
        self.ran: list[list[str]] = []

    def run(self, argv, timeout=None):
        self.ran.append(argv)
        if argv and argv[0] == "bandit":
            if self.bandit is None:
                raise AssertionError("bandit ran against a repository with no Python")
            return self.bandit
        return subprocess.run(
            argv, cwd=self.root, capture_output=True, text=True, timeout=timeout
        )


def _tree(tmp_path, files: dict[str, str]):
    for rel, body in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return tmp_path


def test_a_typescript_repository_does_not_fail_a_python_scanner(tmp_path):
    root = _tree(tmp_path, {
        "src/app/fee.service.ts": "export const x = 1;\n",
        "package.json": '{"name":"app"}',
        "CONTRIBUTING.md": "# how to\n",
    })
    ex = _RealShell(root)
    finding = sast_check(ex, ".")
    assert finding.passed is True
    assert "not run" in finding.summary
    assert not [a for a in ex.ran if a and a[0] == "bandit"]


def test_a_python_file_anywhere_still_gets_scanned(tmp_path):
    root = _tree(tmp_path, {
        "src/app/fee.service.ts": "export const x = 1;\n",
        "tools/deploy.py": "import os\n",
    })
    scan = ExecResult(argv=["bandit"], returncode=0, stdout='{"results": []}', stderr="")
    ex = _RealShell(root, bandit=scan)
    finding = sast_check(ex, ".")
    assert finding.passed is True
    assert "not run" not in finding.summary
    assert [a for a in ex.ran if a and a[0] == "bandit"], "the scanner was skipped"


def test_vendored_python_does_not_count_as_the_repository_having_python(tmp_path):
    """A `.py` inside node_modules or a virtualenv is somebody else's code, and
    walking those trees to find one is what the prune exists to avoid."""
    root = _tree(tmp_path, {
        "src/app/fee.service.ts": "export const x = 1;\n",
        "node_modules/some-pkg/setup.py": "import os\n",
        ".venv/lib/python3.12/site-packages/x/y.py": "import os\n",
    })
    ex = _RealShell(root)
    finding = sast_check(ex, ".")
    assert finding.passed is True
    assert not [a for a in ex.ran if a and a[0] == "bandit"]


def test_a_real_python_finding_still_fails_the_gate(tmp_path):
    """The skip must not become an excuse: a repository WITH Python is scanned
    and a gating finding still fails."""
    root = _tree(tmp_path, {"app.py": "PASSWORD = 'hunter2'\n"})
    scan = ExecResult(
        argv=["bandit"], returncode=1,
        stdout='{"results": [{"issue_severity": "HIGH", "test_id": "B105",'
               ' "filename": "app.py", "line_number": 1, "issue_text": "hardcoded"}]}',
        stderr="",
    )
    ex = _RealShell(root, bandit=scan)
    finding = sast_check(ex, ".")
    assert finding.passed is False
    assert finding.status is GateStatus.FAIL
