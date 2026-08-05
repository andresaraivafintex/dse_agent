#!/usr/bin/env python
"""Propose an AGENTS.md to a repository that has none, as a pull request.

Run by a human, deliberately, once per repository — usually when onboarding it.
It is NOT wired into the work-item loop on purpose: opening an unrelated pull
request in the middle of somebody's task would be a surprise, and the decision
to propose a document to a customer's repository is theirs to take.

WHICH REPOSITORIES NEED IT is already on the ledger. Since the Planner started
reading AGENTS.md at the base ref, every turn records where the document came
from, so the repositories that curate none are a query:

    SELECT DISTINCT details->>'repo'
    FROM audit_log
    WHERE action = 'planner_turn_completed'
      AND details->>'agents_md_source' = 'absent';

Usage:
    python scripts/propose_agents_md.py --tenant <id> --repo owner/name [--base main]
    python scripts/propose_agents_md.py --tenant <id> --repo owner/name --dry-run

`--dry-run` prints the derived facts and the draft and opens nothing. Use it
first: the model call costs about two cents and the facts alone often show the
draft is not worth proposing yet.
"""
from __future__ import annotations

import argparse
import sys

from dse_contracts import GatewayCallHeaders, Stage
from dse_validation.config import GitHubConfig
from dse_validation.github.client import build_github_client
from model_gateway_client.gateway_call import chat_completion
from sandbox_runtime.model_gateway_client import mint_virtual_key
from sandbox_runtime.repo_doc import build_doc_prompt, derive_facts, propose_agents_md

# One draft per repository, once. Bounded so a pathological repo cannot turn a
# two-cent call into an open-ended one.
_MAX_TOKENS = 900


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--base", default="main")
    ap.add_argument("--model", default=None, help="defaults to DSE_PLANNER_MODEL")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = GitHubConfig()
    if not cfg.is_configured:
        print("GitHub App is not configured; nothing to propose against.", file=sys.stderr)
        return 2
    client = build_github_client(cfg)

    # Refuse if the repository already curates one. Overwriting a human's
    # document with a machine draft is the one outcome this must never produce.
    if client.get_file_text(args.repo, "AGENTS.md", args.base) is not None:
        print(f"{args.repo}@{args.base} already has an AGENTS.md — nothing to propose.")
        return 0

    tree = client.get_tree_paths(args.repo, args.base, limit=200_000)
    if not tree:
        print(f"{args.repo}@{args.base}: empty or unreadable tree.", file=sys.stderr)
        return 1
    facts = derive_facts(tree)

    print("Derived facts (checkable against the tree):")
    print(facts.as_bullets())
    print()

    import os

    model = args.model or os.environ.get("DSE_PLANNER_MODEL") or os.environ.get(
        "DSE_CODER_MODEL", "anthropic/claude"
    )
    headers = GatewayCallHeaders(
        tenant_id=args.tenant, work_item_id=f"repo-doc:{args.repo}", stage=Stage.planner
    )
    vk = mint_virtual_key(headers)
    result = chat_completion(
        headers=headers,
        virtual_key=vk.virtual_key,
        model=model,
        messages=[{"role": "user", "content": build_doc_prompt(facts)}],
        timeout=120.0,
        max_tokens=_MAX_TOKENS,
        temperature=0,
    )
    draft = (result.content or "").strip()
    if not draft.startswith("#"):
        print("the model did not return a Markdown document; refusing to propose it", file=sys.stderr)
        return 1

    if args.dry_run:
        print("--- draft (not proposed) ---")
        print(draft)
        return 0

    pr = propose_agents_md(
        client, args.repo, args.base, tree=tree, doc_body=draft + "\n", facts=facts
    )
    print(f"proposed: {pr.get('html_url') or pr.get('number')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
