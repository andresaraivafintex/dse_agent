"""Plano 08 §F (F5) — operational (not decorative) Worker Deployment Versioning.

Proves the MODERN wiring (not the deprecated classic version-set) is assembled
correctly: version = (deployment_name, build_id), versioning enabled, and PINNED
behavior (in-flight workflows drain on the old version → safe cutover).
Live activation/cutover is an ops step (CLI) — out of scope for this test.
"""
from __future__ import annotations

from temporalio.common import VersioningBehavior

from dse_orchestrator.worker import build_deployment_config, _parse_args


def test_deployment_config_is_pinned_and_versioned():
    cfg = build_deployment_config("dse-orchestrator", "git-abc123")
    assert cfg.version.deployment_name == "dse-orchestrator"
    assert cfg.version.build_id == "git-abc123"
    assert cfg.use_worker_versioning is True
    # PINNED = workflow stays on the version it started on → safe drain-and-cutover
    assert cfg.default_versioning_behavior == VersioningBehavior.PINNED


def test_build_id_is_pinnable_via_args_and_env():
    args = _parse_args(["--build-id", "v42", "--deployment-name", "dse-x",
                        "--use-worker-versioning"])
    assert args.build_id == "v42"
    assert args.deployment_name == "dse-x"
    assert args.use_worker_versioning is True


def test_versioning_off_by_default():
    # Safe default: without the flag/env, versioning stays off (enabling it
    # requires a versioning-enabled server + an ops cutover).
    args = _parse_args([])
    assert args.use_worker_versioning is False
