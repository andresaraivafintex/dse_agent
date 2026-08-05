"""The Tester's authoring context is READ from the sandbox, never copied out.

Real incident, 2026-08-05 on the Angular testbed: `run_tester_turn` ran
`kubectl cp <pod>:/workspace <local>` into the orchestrator's own /tmp. After
the L1 gate's `npm ci` that tree is an Angular install; /tmp is an emptyDir
with `sizeLimit: 256Mi`, so kubelet EVICTED the Pod mid-activity — five times
in five minutes, each replacement refilling the volume. Temporal reported it as
"Worker is shutting down and this activity did not complete in time", which
points nowhere near the cause.

Excluding node_modules from the copy would not have been enough: `.angular/
cache` and `dist/` are in there too, GNU tar's `--exclude=./node_modules` does
not match a nested `packages/*/node_modules`, and buffering the archive to
strip it only converts an eviction into an OOMKill.

The prompt wants about 5 kB. These tests hold the reads to that shape.
"""
from __future__ import annotations

import subprocess

from sandbox_runtime.activities import (
    _CONTEXT_READ_TIMEOUT_SECONDS,
    _EXAMPLE_TEST_CHARS,
    _PKG_JSON_CHARS,
    _TESTER_DIFF_CHARS,
    _TesterContextUnavailable,
    _pod_tester_context,
    _sh_quote,
)


def _recorder(responses: dict[str, str], *, fail: str | None = None):
    """A `_pod_sh` stand-in. Returns the first response whose key is a substring
    of the script, and records every script it was asked to run."""
    scripts: list[tuple[str, int | None]] = []

    def pod_sh(script: str, *, timeout: int | None = None, input_text=None):
        scripts.append((script, timeout))
        if fail and fail in script:
            return subprocess.CompletedProcess([script], 1, "", "boom")
        for key, out in responses.items():
            if key in script:
                return subprocess.CompletedProcess([script], 0, out, "")
        return subprocess.CompletedProcess([script], 0, "", "")

    pod_sh.scripts = scripts  # type: ignore[attr-defined]
    return pod_sh


_LISTING = "./src/app.spec.ts\n./tests/test_dse.py\n./src/util.ts\n"


def _ok_reader(**over):
    responses = {
        "cat package.json": '{"name":"app"}',
        "find .": _LISTING,
        "git show": "diff --git a/src/util.ts b/src/util.ts\n",
    }
    responses.update(over)
    return _recorder(responses)


# ---------------------------------------------------------------------------
# Nothing leaves the Pod but the bytes we asked for.
# ---------------------------------------------------------------------------
def test_every_read_is_truncated_inside_the_pod():
    """`head -c` runs in the Pod, so a gigantic tracked file costs one buffer of
    the size we asked for — not its own size. This is what makes the memory
    bound real rather than aspirational."""
    pod = _ok_reader()
    _pod_tester_context(pod)
    for script, _ in pod.scripts:
        assert "head -c" in script, f"an unbounded read: {script}"


def test_the_bounds_are_the_ones_the_prompt_actually_uses():
    pod = _ok_reader()
    _pod_tester_context(pod)
    joined = " ".join(s for s, _ in pod.scripts)
    assert f"head -c {_PKG_JSON_CHARS}" in joined
    assert f"head -c {_EXAMPLE_TEST_CHARS}" in joined
    assert f"head -c {_TESTER_DIFF_CHARS}" in joined


def test_the_listing_never_descends_into_the_trees_nobody_reads():
    pod = _ok_reader()
    _pod_tester_context(pod)
    find = next(s for s, _ in pod.scripts if "find ." in s)
    for pruned in ("node_modules", ".git", "dist", ".angular"):
        assert pruned in find, f"{pruned} is not pruned — find walks it"
    assert "-prune" in find, "find must stop BEFORE descending, not filter after"


def test_the_reads_carry_the_short_timeout_not_the_control_default():
    pod = _ok_reader()
    _pod_tester_context(pod)
    for script, timeout in pod.scripts:
        assert timeout == _CONTEXT_READ_TIMEOUT_SECONDS, script


# ---------------------------------------------------------------------------
# What the context actually says.
# ---------------------------------------------------------------------------
def test_only_real_test_paths_become_existing_tests():
    ctx = _pod_tester_context(_ok_reader())
    assert ctx.existing_tests == {"src/app.spec.ts", "tests/test_dse.py"}
    assert "src/util.ts" not in ctx.existing_tests


def test_the_diff_is_read_in_the_pod_where_the_repository_is():
    """Running `git show` against a local copy is what broke when the copy
    stopped carrying .git: it exits 128 and the prompt silently reads
    "(diff unavailable)"."""
    ctx = _pod_tester_context(_ok_reader())
    assert ctx.diff.startswith("diff --git")
    assert ctx.workspace_dir is None, "there is no local copy on the K8s path"


def test_a_repo_with_no_tests_still_produces_a_usable_context():
    ctx = _pod_tester_context(_ok_reader(**{"find .": "\n"}))
    assert ctx.existing_tests == set()
    assert "no existing tests" in ctx.example_test


def test_a_repo_with_no_package_json_says_so():
    pod = _recorder({"find .": _LISTING})
    ctx = _pod_tester_context(pod)
    assert "no package.json" in ctx.package_json


# ---------------------------------------------------------------------------
# Failure is loud where it has to be.
# ---------------------------------------------------------------------------
def test_an_unreadable_workspace_raises_instead_of_pretending_it_is_empty():
    """Degrading quietly would send the model an empty repository and bill a
    turn for tests written against nothing."""
    pod = _recorder({}, fail="find .")
    try:
        _pod_tester_context(pod)
    except _TesterContextUnavailable as exc:
        assert "rc=1" in str(exc)
    else:
        raise AssertionError("an unreadable workspace was treated as an empty one")


def test_a_missing_diff_only_degrades():
    ctx = _pod_tester_context(_recorder({"find .": _LISTING, "cat package.json": "{}"}))
    assert ctx.diff == ""
    assert ctx.existing_tests, "the rest of the context still stands"


# ---------------------------------------------------------------------------
# The example test's path comes out of the customer's repository.
# ---------------------------------------------------------------------------
def test_a_hostile_filename_cannot_break_out_of_the_shell_command():
    evil = "src/a'; rm -rf /; echo '.spec.ts"
    pod = _ok_reader(**{"find .": f"./{evil}\n"})
    _pod_tester_context(pod)
    cat = next(s for s, _ in pod.scripts if "cat " in s and "package.json" not in s)
    assert "; rm -rf /" not in cat.replace(_sh_quote(evil), ""), (
        "the filename escaped its quoting"
    )
    assert _sh_quote(evil) in cat


def test_sh_quote_survives_a_single_quote():
    assert _sh_quote("a'b") == "'a'\\''b'"


# ---------------------------------------------------------------------------
# The two paths must say the SAME thing to the model.
# ---------------------------------------------------------------------------
def test_the_skills_note_is_identical_on_both_paths(tmp_path):
    """A divergence here does not fail anything — it quietly weakens the
    prompt, which is the hardest kind of bug to notice. The first version of
    the pod-side reader looked in `.dse/skills` (the directory is
    `.claude/skills`), dropped each skill's `description:` and lost the header
    that tells the model the guidance is mandatory. It would simply have found
    nothing, forever, and no test would have said so.

    This drives a REAL `sh -c` against a real directory, because the whole
    point is whether the shell loop and the Python reader agree."""
    from sandbox_runtime.activities import _SKILLS_NOTE_CHARS
    from sandbox_runtime.skill_files import workspace_skills_note

    for name, desc in [("testing-style", "How this repo writes tests"),
                       ("tenant-rules", "Tenant conventions")]:
        d = tmp_path / ".claude" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\nbody\n"
        )

    def pod_sh(script, *, timeout=None, input_text=None):
        return subprocess.run(
            ["sh", "-c", script.replace("cd /workspace &&", f"cd {tmp_path} &&", 1)],
            capture_output=True, text=True,
        )

    assert (
        _pod_tester_context(pod_sh).skills_note.strip()
        == workspace_skills_note(str(tmp_path))[:_SKILLS_NOTE_CHARS].strip()
    )


def test_a_repo_with_no_skills_adds_nothing_to_the_prompt(tmp_path):
    def pod_sh(script, *, timeout=None, input_text=None):
        return subprocess.run(
            ["sh", "-c", script.replace("cd /workspace &&", f"cd {tmp_path} &&", 1)],
            capture_output=True, text=True,
        )

    assert _pod_tester_context(pod_sh).skills_note == ""
