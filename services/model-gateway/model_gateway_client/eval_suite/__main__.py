"""Tier-2 eval suite CLI (WSD-E5-T1).

    python -m model_gateway_client.eval_suite [--model ALIAS ...] [--json]

Exit code 0 if no case FAILED (skips do not fail the suite), 1 otherwise — for
use as a CI / model-promotion gate.
"""
from __future__ import annotations

import argparse
import json
import sys

from .runner import run_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="model_gateway_client.eval_suite")
    parser.add_argument("--model", action="append", dest="models", help="filter by model alias (repeatable)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args(argv)

    report = run_suite(models=args.models)

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        for r in report.results:
            mark = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[r.status]
            print(f"[{mark}] {r.case_id:<24} {r.model:<40} {r.latency_ms:>7.1f}ms  ${r.cost_usd:.6f}")
            if r.detail:
                print(f"        {r.detail}")
        s = report
        print(f"\n{s.passed} passed, {s.failed} failed, {s.skipped} skipped -> {'OK' if s.ok else 'FAILED'}")

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
