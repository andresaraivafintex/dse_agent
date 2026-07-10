#!/usr/bin/env python3
"""WSF-E2-T3a — scanner de secrets em texto plano no repositório versionado.

Prova o critério de aceite "nenhum secret em env-var/manifest": varre
arquivos versionados (`.env`, YAML/manifests, docker-compose fragments,
Helm values) procurando por padrões conhecidos de secret hardcoded (tokens
Slack/GitHub, chaves AWS, senhas de banco em texto puro, private keys PEM,
etc.) e falha (`exit(1)`) se encontrar alguma correspondência FORA de uma
lista de exclusão explícita (arquivos `.env.example`/`*.example`, fixtures de
teste marcadas, e os placeholders de dev local documentados no
CONVENTIONS.md como aceitáveis: `dse_dev_only`, `dse_dev_root`,
`dse_app_dev_only` — essas três são credenciais de Postgres/Vault de
desenvolvimento local, não de produção, e são conhecidas/documentadas na
fundação).

Uso:
    python3 scripts/scan_for_plaintext_secrets.py [--root PATH] [--ci]

Saída: lista de achados (arquivo:linha:padrão) em stdout; exit code 1 se
algum achado sobreviver aos filtros de exclusão, exit code 0 caso contrário.

Este script é determinístico (sem LLM) — P1: nenhuma decisão de fluxo por
LLM. A decisão "isto é um secret vazado" é 100% baseada em regex + allowlist
explícita revisável em code review humano.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Padrões de secret conhecidos. Cada entrada: (nome, regex compilado).
# Regexes são propositalmente específicos o suficiente para não gerar ruído
# em cima de nomes de variável (`SLACK_BOT_TOKEN=` sem valor não é um match;
# precisa ter o formato do token real depois do `=`).
# ---------------------------------------------------------------------------
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("slack_bot_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("slack_signing_secret_assignment", re.compile(r"SLACK_(BOT_TOKEN|SIGNING_SECRET|APP_TOKEN)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{16,}")),
    ("github_pat", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("github_app_private_key_assignment", re.compile(r"GITHUB_(APP_PRIVATE_KEY|WEBHOOK_SECRET|TOKEN)\s*[:=]\s*['\"]?[A-Za-z0-9/+_=-]{16,}")),
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws_secret_access_key_assignment", re.compile(r"AWS_SECRET_ACCESS_KEY\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{30,}")),
    ("generic_api_key_assignment", re.compile(r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=.]{20,}")),
    ("private_key_pem", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("vault_token_assignment", re.compile(r"VAULT_TOKEN\s*[:=]\s*['\"]?(hvs|hvb|s)\.[A-Za-z0-9]{10,}")),
    ("postgres_password_in_url", re.compile(r"postgresql://[^:\s]+:[^@\s]{6,}@")),
    ("database_password_assignment", re.compile(r"(?i)(db|database|postgres)_password\s*[:=]\s*['\"]?[^\s'\"]{6,}")),
]

# Valores de placeholder de dev local documentados na fundação (CONVENTIONS.md
# / docker-compose.yml) — não são segredos de produção, apenas credenciais
# fixas de ambiente local descartável. Aparecem em vários matches acima
# (ex.: `postgres_password_in_url` bate em toda DSN de dev) — são excluídos
# explicitamente para que o scanner não vire ruído permanente.
KNOWN_DEV_PLACEHOLDERS = {
    "dse_dev_only",
    "dse_dev_root",
    "dse_app_dev_only",
}

# Diretórios/arquivos nunca escaneados: dependências, VCS, caches, e os
# próprios arquivos de exemplo (que existem justamente para mostrar o
# *formato* esperado sem conter um valor real).
EXCLUDED_DIR_NAMES = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "*.egg-info",
}
EXCLUDED_FILE_SUFFIXES = (".example", ".sample", ".md")
EXCLUDED_FILE_NAMES = {".env.example", ".env.sample"}

# Extensões/arquivos relevantes para o scan (env, manifests, YAML, compose).
SCANNED_GLOBS = ["**/.env", "**/*.env", "**/*.yml", "**/*.yaml"]


def _is_excluded_path(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    if path.name in EXCLUDED_FILE_NAMES:
        return True
    if any(str(path.name).endswith(suf) for suf in EXCLUDED_FILE_SUFFIXES):
        return True
    for part in rel.parts:
        if part in EXCLUDED_DIR_NAMES or part.endswith(".egg-info"):
            return True
    return False


def _line_is_only_known_placeholder(line: str) -> bool:
    """True se a única 'credencial' presente na linha for um dos placeholders
    de dev conhecidos (ex.: DSN `postgresql://dse:dse_dev_only@...`)."""
    stripped = line.strip()
    return any(placeholder in stripped for placeholder in KNOWN_DEV_PLACEHOLDERS)


# Indicadores de que o valor é uma REFERÊNCIA a uma env var/secret manager,
# não um secret hardcoded (ex.: `api_key: os.environ/OPENAI_API_KEY` no
# formato do LiteLLM, ou `${VAULT_TOKEN}` em um manifest) — estes são
# exatamente o padrão que queremos incentivar, não flaggar como violação.
_ENV_REFERENCE_INDICATORS = (
    "os.environ",
    "os.getenv(",
    "process.env.",
    "${",
    "secretKeyRef",
    "valueFrom",
)


def _match_is_env_reference(line: str) -> bool:
    return any(indicator in line for indicator in _ENV_REFERENCE_INDICATORS)


def _git_tracked_files(root: Path) -> set[Path] | None:
    """Retorna o conjunto de arquivos "versionáveis" (relativos a `root`):
    já commitados (`git ls-files`) UNIÃO ainda não commitados mas não
    ignorados (`git ls-files --others --exclude-standard`). Ou None se
    `root` não for um repositório git / git indisponível.

    Usamos a união (não só `git ls-files`) porque este monorepo está em
    desenvolvimento paralelo ativo sem commits intermediários (o integrador
    consolida tudo no final) — rodar o scanner só contra o HEAD commitado o
    deixaria cego para secrets introduzidos em arquivos novos ainda não
    adicionados ao índice. "Versionado" aqui significa "elegível para
    versionamento" (não coberto por .gitignore), que é o que .gitignore
    define precisamente."""
    try:
        tracked = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if tracked.returncode != 0:
        return None
    lines = tracked.stdout.splitlines() + (untracked.stdout.splitlines() if untracked.returncode == 0 else [])
    return {root / line for line in lines if line.strip()}


def scan_repo(root: Path) -> list[tuple[Path, int, str, str]]:
    """Retorna lista de (arquivo, linha, nome_padrão, trecho) para cada match
    que sobrevive aos filtros de exclusão.

    Quando `root` é um repositório git, restringe o scan aos arquivos
    RASTREADOS (`git ls-files`) — é a definição exata de "versionado" pedida
    pelo critério de aceite, e evita falso-positivo em dependências
    instaladas localmente (`.venv-*/`, `node_modules/`, etc.) que nunca
    seriam commitadas. Sem git disponível, cai para um walk de diretório com
    a lista de exclusão heurística abaixo."""
    findings: list[tuple[Path, int, str, str]] = []
    seen_files: set[Path] = set()

    tracked = _git_tracked_files(root)

    def candidate_files() -> list[Path]:
        if tracked is not None:
            return [p for p in tracked if p.suffix in (".yml", ".yaml") or p.name == ".env" or p.name.endswith(".env")]
        found: list[Path] = []
        for pattern_glob in SCANNED_GLOBS:
            found.extend(p for p in root.glob(pattern_glob) if p.is_file())
        return found

    for path in candidate_files():
        if path in seen_files or not path.is_file():
            continue
        if _is_excluded_path(path, root):
            continue
        if tracked is None and _is_excluded_by_dir_heuristic(path, root):
            continue
        seen_files.add(path)

        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            if _line_is_only_known_placeholder(line):
                continue
            if _match_is_env_reference(line):
                continue
            for pattern_name, regex in SECRET_PATTERNS:
                match = regex.search(line)
                if match:
                    findings.append((path.relative_to(root), lineno, pattern_name, match.group(0)[:60]))

    return findings


def _is_excluded_by_dir_heuristic(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    return any(part in EXCLUDED_DIR_NAMES or part.startswith(".venv") for part in rel.parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Raiz do repositório a escanear (default: cwd)")
    parser.add_argument("--ci", action="store_true", help="Formato de saída compacto para CI")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    findings = scan_repo(root)

    if not findings:
        print(f"[scan_for_plaintext_secrets] OK — nenhum secret em texto plano encontrado sob {root}")
        return 0

    print(f"[scan_for_plaintext_secrets] FALHOU — {len(findings)} possível(is) secret(s) em texto plano:")
    for path, lineno, pattern_name, snippet in findings:
        print(f"  {path}:{lineno}  [{pattern_name}]  {snippet}")
    print()
    print("Se algum destes for falso-positivo (ex.: fixture de teste com valor fake),")
    print("mova o valor para uma variável nomeada claramente como fixture/mock, ou")
    print("adicione o arquivo à lista de exclusão explícita neste script com justificativa.")
    print("Se for um secret real: mova para o Vault (services/platform/dse_secrets) e")
    print("substitua o valor aqui por uma referência de env var lida em runtime.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
