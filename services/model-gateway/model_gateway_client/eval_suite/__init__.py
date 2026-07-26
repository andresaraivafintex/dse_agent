"""Model-gateway Tier-2 evaluation suite (WSD-E5-T1).

OWNER (named owner — gap 6 of the plan): WS-D (model-gateway). Maintenance
contact: the model-gateway team is responsible for keeping `cases.yaml` up to
date as new models land in `litellm_config.yaml`.

What it is: a small, REAL, runnable harness that fires a set of reference
prompts against the models configured in the gateway and reports pass/fail +
cost + latency per case. It is NOT the full air-gapped Tier-2 serving (Phase 4)
— it is the minimum eval structure asked for now, so that "swapping a model" or
"bumping a LiteLLM version" has an objective gate before promotion.

Run:
    python -m model_gateway_client.eval_suite            # every reachable model
    python -m model_gateway_client.eval_suite --model eco/echo-model

Unreachable models (e.g. the `bedrock/*` aliases with no AWS in this session)
are reported as SKIPPED, not as failures — the harness distinguishes "the model
got the assertion wrong" from "the model is unavailable on the current infra".
"""
from __future__ import annotations

from .runner import EvalCaseResult, EvalReport, run_suite

__all__ = ["run_suite", "EvalReport", "EvalCaseResult"]
