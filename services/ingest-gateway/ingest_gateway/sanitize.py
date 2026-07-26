"""WSA-E2-T3 — Inbound content sanitization (defense #3 of the intake
pipeline). Pluggable pipeline: each stage function takes/returns a `str`.

IMPORTANT (documented explicitly, as requested): this is MITIGATION, not
CONTAINMENT. Stripping invisible Unicode and redacting obvious token/secret
patterns reduces the surface for prompt injection / obvious exfiltration
*before* the text can reach any model call, but a determined attacker can
obfuscate beyond what simple regexes catch. The real containment (the
guarantee that actually prevents exfiltration even if the model is fooled) is
the WS-C default-deny egress proxy (`services/egress-proxy/`) — this module is
defense in depth, not a line of defense that can be trusted on its own.

The original `content_snapshot` (frozen by the TOCTOU defense, WSA-E2-T2)
stays intact for auditing — the SANITIZED version is the one that flows down
the pipeline to clarification/Coder. Never overwrite the original snapshot.
"""
from __future__ import annotations

import re
import unicodedata

# "Invisible"/control Unicode categories to strip: Cf (format — includes
# zero-width space/joiner, RTL/LTR bidi override), Cc (control, except the
# common whitespace \t \n \r that we preserve so formatting is not broken).
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


# Obvious token/secret patterns (WSA-E2-T3). Each tuple is (name, regex).
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
    """Full pipeline applied to `content_snapshot` before it goes to any stage
    that involves a model. Order matters: unicode first (so invisible
    characters cannot break the secret regexes)."""
    return redact_secrets(strip_invisible_unicode(text))
