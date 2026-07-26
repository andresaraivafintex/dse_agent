"""WS-A Phase 2 (WSA-E5) — Jira adapter: inbound (webhook + poller fallback) and
outbound (per-ticket serialized transitions + single status comment via
MutableCommentWriter). Mirrors the adapter-github structure and reuses all of
the shared `ingest_gateway` logic (admission, correlation, the 4 defenses,
tenant binding) — the adapter is 100% stateless.
"""
