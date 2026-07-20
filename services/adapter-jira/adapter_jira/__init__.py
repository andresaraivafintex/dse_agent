"""WS-A Fase 2 (WSA-E5) — adapter Jira: inbound (webhook + poller fallback) e
outbound (transições serializadas por ticket + status comment único via
MutableCommentWriter). Espelha a estrutura do adapter-github e reutiliza toda
a lógica compartilhada de `ingest_gateway` (admissão, correlação, 4 defesas,
tenant binding) — o adapter é 100% stateless.
"""
