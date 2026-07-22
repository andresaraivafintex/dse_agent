"""Plano 08 §F (F5) — Worker Deployment Versioning operacional (não decorativo).

Prova que a wiring MODERNA (não a version-set clássica deprecada) é montada
corretamente: versão = (deployment_name, build_id), versioning ligado, e
comportamento PINNED (workflows em voo drenam na versão antiga → cutover seguro).
A ativação/cutover ao vivo é passo de operação (CLI) — fora do escopo do teste.
"""
from __future__ import annotations

from temporalio.common import VersioningBehavior

from dse_orchestrator.worker import build_deployment_config, _parse_args


def test_deployment_config_is_pinned_and_versioned():
    cfg = build_deployment_config("dse-orchestrator", "git-abc123")
    assert cfg.version.deployment_name == "dse-orchestrator"
    assert cfg.version.build_id == "git-abc123"
    assert cfg.use_worker_versioning is True
    # PINNED = workflow fica na versão em que começou → drain-and-cutover seguro
    assert cfg.default_versioning_behavior == VersioningBehavior.PINNED


def test_build_id_is_pinnable_via_args_and_env():
    args = _parse_args(["--build-id", "v42", "--deployment-name", "dse-x",
                        "--use-worker-versioning"])
    assert args.build_id == "v42"
    assert args.deployment_name == "dse-x"
    assert args.use_worker_versioning is True


def test_versioning_off_by_default():
    # default seguro: sem a flag/env, versioning fica desligado (ativação exige
    # server habilitado + cutover de operação).
    args = _parse_args([])
    assert args.use_worker_versioning is False
