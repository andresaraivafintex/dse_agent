"""WSE-E1-T2 (parte 2) — scanner de segredos self-contained (regex + entropia
de Shannon), sem dependência de serviço externo. Roda DENTRO do sandbox via o
mesmo `SandboxExecutor` dos outros checks: o scanner é um script Python puro
(stdlib apenas) embutido como string e executado com `python3 -c` — funciona
tanto no `DockerExecSandbox` (o container só precisa ter `python3`, que já é
garantido pelo runtime OpenHands) quanto no `LocalFakeSandbox` de teste.

Cobre: AWS access key id, GitHub/Slack tokens, cabeçalho de chave privada PEM,
e o caso genérico "variável com nome de segredo == literal de alta entropia".
`detect-secrets` (se instalado no sandbox) pode substituir isto no futuro sem
mudar a assinatura de `secret_scan_check` — ver README.
"""
from __future__ import annotations

import json

from dse_contracts import L1Finding

from dse_validation.sandbox_exec import SandboxExecutor

# Script stdlib-only executado dentro do sandbox. Emite uma linha JSON no
# stdout com a lista de achados — parseada pelo lado de fora.
_SCANNER_SCRIPT = r'''
import json, math, os, re, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
PLACEHOLDER_VALUES = {
    "changeme", "example", "placeholder", "xxx", "todo", "your_key_here",
    "insert_key_here", "dummy", "test", "fake", "secret", "password",
}

PATTERNS = [
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("private_key_header", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----")),
]

ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key)\b"
    r"\s*[:=]\s*[\"']([^\"']{8,})[\"']"
)


def shannon_entropy(s):
    if not s:
        return 0.0
    freq = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in freq.values())


findings = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for fname in filenames:
        if fname.endswith((".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".lock")):
            continue
        path = os.path.join(dirpath, fname)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(lines, start=1):
            for kind, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(
                        {"kind": kind, "file": path, "line": lineno, "snippet": line.strip()[:200]}
                    )
            for m in ASSIGNMENT_RE.finditer(line):
                value = m.group(2)
                if value.lower() in PLACEHOLDER_VALUES:
                    continue
                if shannon_entropy(value) >= 3.0 and len(value) >= 12:
                    findings.append(
                        {
                            "kind": "high_entropy_assignment",
                            "file": path,
                            "line": lineno,
                            "snippet": line.strip()[:200],
                        }
                    )

print(json.dumps({"findings": findings}))
'''


def secret_scan_check(executor: SandboxExecutor, target_dir: str = ".", timeout: int = 60) -> L1Finding:
    result = executor.run(["python3", "-c", _SCANNER_SCRIPT, target_dir], timeout=timeout)
    if result.returncode == 127:
        return L1Finding(check="secret_scan", passed=False, detail=f"python3 não encontrado no sandbox: {result.stderr.strip()}")
    if result.returncode != 0:
        return L1Finding(
            check="secret_scan",
            passed=False,
            detail=f"scanner de segredos falhou (exit={result.returncode}): {result.stderr[:2000]}",
        )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {"findings": []}
    except (json.JSONDecodeError, IndexError):
        return L1Finding(
            check="secret_scan", passed=False, detail=f"saída inesperada do scanner: {result.stdout[:2000]}"
        )

    findings = payload.get("findings", [])
    if not findings:
        return L1Finding(check="secret_scan", passed=True, detail="nenhum segredo/token detectado")

    lines = [f"- [{f['kind']}] {f['file']}:{f['line']} — {f['snippet']}" for f in findings[:20]]
    detail = f"{len(findings)} possível(is) segredo(s) detectado(s):\n" + "\n".join(lines)
    return L1Finding(check="secret_scan", passed=False, detail=detail)
