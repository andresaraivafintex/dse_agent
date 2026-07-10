"""WSA-E2-T3 — Sanitização de conteúdo inbound (defesa #3 do pipeline de
intake). Pipeline plugável: cada função de estágio recebe/retorna `str`.

IMPORTANTE (documentado explicitamente, conforme pedido): isto é MITIGAÇÃO,
não CONTENÇÃO. Remover Unicode invisível e redigir padrões óbvios de
token/secret reduz a superfície de prompt injection / exfiltração óbvia
*antes* do texto poder chegar a qualquer chamada de modelo, mas um atacante
determinado pode ofuscar além do que regex simples pega. A contenção real
(a garantia que efetivamente impede exfiltração mesmo se o modelo for
enganado) é o egress proxy default-deny do WS-C
(`services/egress-proxy/`) — este módulo é defesa em profundidade, não a
linha de defesa que se pode confiar sozinha.

`content_snapshot` original (congelado pela defesa TOCTOU, WSA-E2-T2)
permanece intacto para auditoria — a versão SANITIZADA é a que segue no
pipeline para clarificação/Coder. Nunca sobrescreva o snapshot original.
"""
from __future__ import annotations

import re
import unicodedata

# Categorias Unicode "invisíveis"/controle a remover: Cf (format — inclui
# zero-width space/joiner, bidi override RTL/LTR), Cc (control, exceto
# whitespace comum \t \n \r que preservamos para não quebrar formatação).
_PRESERVED_CONTROL = {"\t", "\n", "\r"}


def strip_invisible_unicode(text: str) -> str:
    out = []
    for ch in text:
        if ch in _PRESERVED_CONTROL:
            out.append(ch)
            continue
        category = unicodedata.category(ch)
        if category in ("Cf", "Cc"):
            continue  # remove formatting/control chars (zero-width, bidi override, etc.)
        out.append(ch)
    return "".join(out)


# Padrões óbvios de token/secret (WSA-E2-T3). Cada tupla é (nome, regex).
_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[bpears]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_secret_access_key", re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    ("generic_bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{20,}=*")),
]


def redact_secrets(text: str) -> str:
    redacted = text
    for name, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
    return redacted


def sanitize_content(text: str) -> str:
    """Pipeline completo aplicado ao `content_snapshot` antes de seguir para
    qualquer estágio que envolva um modelo. Ordem importa: unicode primeiro
    (para não deixar caracteres invisíveis quebrarem os regex de secret)."""
    return redact_secrets(strip_invisible_unicode(text))
