"""Tier-2 evaluation suite harness (WSD-E5-T1). Owner: WS-D.

Runs `cases.yaml` against the models configured in the gateway. For each case:
mint a virtual key scoped to the model, make a real call via `chat_completion`
(the same production path — it goes through enforcement and observability),
evaluate the assertions, collect cost/latency. Models unavailable on the
current infra (e.g. `bedrock/*` with no AWS) become SKIPPED, not failures.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from dse_contracts.gateway_contract import GatewayCallHeaders, Stage

from .. import chat_completion, mint_virtual_key, revoke_virtual_key
from ..errors import GatewayCallError

_CASES_PATH = os.path.join(os.path.dirname(__file__), "cases.yaml")


@dataclass
class EvalCaseResult:
    case_id: str
    model: str
    status: str  # "passed" | "failed" | "skipped"
    detail: str = ""
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    failed_assertions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "status": self.status,
            "detail": self.detail,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": round(self.latency_ms, 1),
            "failed_assertions": self.failed_assertions,
        }


@dataclass
class EvalReport:
    results: list[EvalCaseResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    @property
    def ok(self) -> bool:
        """The suite "passes" if no case FAILED (skips do not fail the suite)."""
        return self.failed == 0

    def as_dict(self) -> dict:
        return {
            "summary": {"passed": self.passed, "failed": self.failed, "skipped": self.skipped, "ok": self.ok},
            "results": [r.as_dict() for r in self.results],
        }


def _load_cases(path: str) -> list[dict]:
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("cases", [])


def _content_of(result) -> str:
    return result.content


def _eval_assertions(assertions: list[dict], first, second) -> list[str]:
    """Returns the list of descriptions of assertions that FAILED (empty = all
    ok). `first`/`second` are two ChatCompletionResult (the second is the repeat
    call used to check determinism)."""
    failures: list[str] = []
    content = _content_of(first)
    for a in assertions:
        kind = a.get("type")
        if kind == "equals":
            if content != a["value"]:
                failures.append(f"equals: expected {a['value']!r}, got {content!r}")
        elif kind == "contains":
            if a["value"] not in content:
                failures.append(f"contains: {a['value']!r} is not in {content!r}")
        elif kind == "deterministic":
            if _content_of(second) != content:
                failures.append("deterministic: the second call diverged from the first")
        elif kind == "tokens_positive":
            if not (first.tokens_in > 0 and first.tokens_out > 0):
                failures.append(f"tokens_positive: in={first.tokens_in} out={first.tokens_out}")
        elif kind == "max_cost_usd":
            if first.cost_usd > float(a["value"]):
                failures.append(f"max_cost_usd: {first.cost_usd} > {a['value']}")
        else:
            failures.append(f"unknown assertion type: {kind!r}")
    return failures


def _needs_determinism(assertions: list[dict]) -> bool:
    return any(a.get("type") == "deterministic" for a in assertions)


def run_case(case: dict, *, tenant_id: str, work_item_id: str) -> EvalCaseResult:
    model = case["model"]
    messages = case["messages"]
    assertions = case.get("assertions", [])

    key = mint_virtual_key(tenant_id, work_item_id, Stage.reviewer, models=[model])
    headers = GatewayCallHeaders(
        tenant_id=tenant_id, work_item_id=work_item_id, stage=Stage.reviewer, task_class="eval"
    )
    try:
        t0 = time.perf_counter()
        try:
            first = chat_completion(headers=headers, virtual_key=key, model=model, messages=messages)
        except GatewayCallError as exc:
            # Provider 4xx/5xx (e.g. bedrock with no AWS) -> skip, not a failure.
            return EvalCaseResult(
                case_id=case["id"],
                model=model,
                status="skipped",
                detail=f"model unavailable/provider error: HTTP {exc.status_code}",
            )
        latency_ms = (time.perf_counter() - t0) * 1000.0

        second = first
        if _needs_determinism(assertions):
            second = chat_completion(headers=headers, virtual_key=key, model=model, messages=messages)

        failures = _eval_assertions(assertions, first, second)
        return EvalCaseResult(
            case_id=case["id"],
            model=model,
            status="passed" if not failures else "failed",
            detail="" if not failures else "; ".join(failures),
            cost_usd=first.cost_usd,
            latency_ms=latency_ms,
            failed_assertions=failures,
        )
    finally:
        revoke_virtual_key(key)


def run_suite(*, models: list[str] | None = None, cases_path: str | None = None) -> EvalReport:
    """Runs the suite. `models`, if passed, filters the cases down to the listed
    models. Each case uses its own tenant/work_item IDs (isolation)."""
    import uuid

    cases = _load_cases(cases_path or _CASES_PATH)
    if models is not None:
        cases = [c for c in cases if c["model"] in models]

    report = EvalReport()
    for case in cases:
        suffix = uuid.uuid4().hex[:10]
        report.results.append(
            run_case(
                case,
                tenant_id=f"eval-{suffix}",
                work_item_id=f"eval-wi-{suffix}",
            )
        )
    return report
