"""WSE-E2 — WS-E's L2 layer (fresh-context Reviewer).

The Reviewer SESSION itself (the model call with fresh context) is built by
WS-C (WSC-E3-T5) and registered as the `ACTIVITY_RUN_L2_REVIEW` Activity.
This package is the ORCHESTRATION that WS-E owns (Phase 2 addendum, WSE-E2-T4/T5):

  - `session`   — L2 session Protocol + deterministic fake + defensive
                  resolution of the WS-C implementation (which lands in parallel).
  - `l2_review` — orchestrates 1 L2 turn (cheapest-first: only after L1 is green,
                  before CI — P5), records verdict + cost (P8).
  - `fix_loop`  — deterministic logic of the bounded L2->Coder loop (P1/P6):
                  when to send back to the Coder, when to escalate to an operator.
"""
