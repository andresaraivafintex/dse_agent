"""Política fail-closed do perfil de execução do sandbox.

O runtime atual ainda possui atalhos deliberados de desenvolvimento: execução
do SDK no processo do worker, substratos/estágios fixture e virtual keys locais.
Esses atalhos continuam úteis para testes, mas não podem ser confundidos com um
deployment seguro. Este módulo concentra a validação, lê o ambiente em cada
chamada (sem estado import-time) e falha antes de qualquer trabalho consequencial.
"""
from __future__ import annotations

import os
from enum import Enum


PROFILE_ENV_VAR = "DSE_DEPLOYMENT_PROFILE"
SANDBOX_INPROCESS_ENV_VAR = "DSE_SANDBOX_INPROCESS"
SUBSTRATE_ENV_VAR = "DSE_CODER_SUBSTRATE"
MODEL_GATEWAY_FIXTURE_ENV_VAR = "DSE_MODEL_GATEWAY_ALLOW_FIXTURE"


class RuntimeProfile(str, Enum):
    dev = "dev"
    test = "test"
    production = "production"


class RuntimeProfileViolation(RuntimeError):
    """Configuração ou fallback incompatível com o perfil selecionado."""


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_REAL_SUBSTRATES = frozenset({"openhands", "claude-agent"})


def _flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise RuntimeProfileViolation(
        f"{name}={raw!r} inválido; use um booleano explícito "
        "(1/0, true/false, yes/no ou on/off)"
    )


def current_runtime_profile(value: str | None = None) -> RuntimeProfile:
    """Resolve o perfil sem cache para que testes e preflight sejam fiéis."""
    raw = (value if value is not None else os.environ.get(PROFILE_ENV_VAR, "dev"))
    normalized = raw.strip().lower()
    # Aceitar os aliases usuais não reduz segurança: ambos selecionam o ramo
    # mais restritivo, em vez de cair acidentalmente no default de dev.
    if normalized in {"prod", "pilot"}:
        normalized = RuntimeProfile.production.value
    try:
        return RuntimeProfile(normalized)
    except ValueError as exc:
        valid = ", ".join(profile.value for profile in RuntimeProfile)
        raise RuntimeProfileViolation(
            f"{PROFILE_ENV_VAR}={raw!r} desconhecido; valores válidos: {valid}"
        ) from exc


def model_gateway_fixture_allowed() -> bool:
    """Valor efetivo do fallback, com parsing estrito e default legado."""
    return _flag(MODEL_GATEWAY_FIXTURE_ENV_VAR, default=True)


def validate_runtime_profile(
    *,
    require_real_substrate: bool = False,
    require_real_gateway: bool = False,
    local_fallback: str | None = None,
    substrate_name: str | None = None,
) -> RuntimeProfile:
    """Valida controles aplicáveis à operação corrente.

    Em ``dev``/``test`` apenas a sintaxe das flags é validada quando elas são
    consultadas. Em ``production`` qualquer violação é agregada numa única
    exceção, sem tentar degradar para um caminho local.

    ``local_fallback`` deve descrever o atalho que a operação usaria. Enquanto
    o ``agent-runner`` isolado não estiver ligado ao ``execute_stage``, as
    Activities de agente passam esse argumento e, portanto, permanecem
    deliberadamente indisponíveis em produção.
    """
    profile = current_runtime_profile()
    if profile is not RuntimeProfile.production:
        return profile

    violations: list[str] = []
    if _flag(SANDBOX_INPROCESS_ENV_VAR, default=False):
        violations.append(f"{SANDBOX_INPROCESS_ENV_VAR} habilita execução fora do sandbox")

    if require_real_substrate:
        chosen = (substrate_name or os.environ.get(SUBSTRATE_ENV_VAR, "fake")).strip().lower()
        if chosen not in _REAL_SUBSTRATES:
            violations.append(
                f"{SUBSTRATE_ENV_VAR}={chosen or '<vazio>'!r} não é um substrato real "
                f"aprovado ({', '.join(sorted(_REAL_SUBSTRATES))})"
            )

    if require_real_gateway and model_gateway_fixture_allowed():
        violations.append(
            f"{MODEL_GATEWAY_FIXTURE_ENV_VAR} permite virtual key fixture/fallback local"
        )

    if local_fallback:
        violations.append(f"fallback local proibido: {local_fallback}")

    if violations:
        raise RuntimeProfileViolation(
            "perfil production recusado pelo sandbox-runtime: " + "; ".join(violations)
        )
    return profile


def validate_runtime_startup(
    *, isolated_stage_execution_available: bool | None = None
) -> RuntimeProfile:
    """Preflight estático para o carregamento do runtime no worker.

    O caller que carrega as Activities pode invocar esta função antes de
    iniciar o polling. As Activities de provisionamento também a invocam, de
    modo que um integrador que ainda não adotou o preflight continua protegido.
    """
    profile = validate_runtime_profile(
        require_real_substrate=True,
        require_real_gateway=True,
    )
    if (
        profile is RuntimeProfile.production
        and isolated_stage_execution_available is False
    ):
        raise RuntimeProfileViolation(
            "perfil production recusado pelo sandbox-runtime: "
            "SandboxDriver.execute_stage isolado ainda não está disponível; "
            "fallback para o worker é proibido"
        )
    return profile


def reject_local_agent_execution(stage: str) -> RuntimeProfile:
    """Bloqueia os caminhos de agente/stand-in ainda locais em produção."""
    return validate_runtime_profile(
        require_real_substrate=True,
        require_real_gateway=True,
        local_fallback=f"estágio {stage!r} ainda executa no processo/workspace do worker",
    )
