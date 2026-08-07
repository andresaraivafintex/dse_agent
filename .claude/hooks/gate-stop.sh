#!/usr/bin/env bash
# Stop — impede encerrar o turno com o lint vermelho, e cobra EVIDENCIA de teste
# para as suites que a mudanca tocou.
#
# Duas metades, porque este repo tem dois tipos de suite:
#
#   1. Sem docker (contracts, tooling, packages): o hook RODA. 5,2s medidos.
#      Vermelho aqui e exit 2 — nao ha como encerrar por cima disso.
#   2. Com docker (orchestrator, sandbox-runtime, validation, ...): o hook nao
#      pode roda-las por voce, entao checa o ARTEFATO: existe
#      test-results/<suite>.xml mais novo que o arquivo editado, com
#      failures="0" errors="0"? Nao existe -> exit 2 com o comando exato.
#      Isso e uma checagem de sistema de arquivos, nao um juizo do agente: nao
#      da para satisfazer dizendo que rodou.
#
# A guarda `stop_hook_active` e OBRIGATORIA (senao: bloqueia -> agente tenta ->
# Stop dispara -> loop infinito). Ela tambem torna a metade 2 um aviso de um
# tiro: se o docker estiver fora e a suite nao puder rodar, o segundo Stop
# passa — e a saida certa nesse caso e reportar IMPLEMENTED-NOT-VERIFIED, nao
# fingir que passou.

set -uo pipefail

REPO="/Users/saraiva/Documents/DSE/fase1"
export PATH="$REPO/.venv/bin:$PATH"
cd "$REPO" || exit 0

INPUT=$(cat)
[[ "$(printf '%s' "$INPUT" | jq -r '.stop_hook_active')" == "true" ]] && exit 0

# --------------------------------------------------- o que esta sessao mexeu
# Working tree + o que a branch tem a mais que origin/main. Sem .py mexido,
# nao ha nada a gatear e o hook sai em milissegundos.
CHANGED=$(
  { git status --porcelain -- '*.py' | awk '{print $NF}'
    git diff --name-only origin/main...HEAD -- '*.py' 2>/dev/null
  } | sort -u | grep -v -e '/\.venv' -e '__pycache__' -e '/preview_repo/'
)
[[ -z "$CHANGED" ]] && exit 0

# ------------------------------------------------------------- 1. lint (1,1s)
if ! OUT=$(make lint 2>&1); then
  printf '%s\n' "$OUT" | tail -40 >&2
  printf '\n`make lint` falhou. Corrija antes de encerrar.\n' >&2
  exit 2
fi

# ------------------------------------ 2. suites sem docker (5,2s) — CI espelho
# Estes sao exatamente os jobs `quality` e `contracts` do CI, mais `packages`.
if ! OUT=$(python scripts/test_matrix.py --group contracts --group tooling --group packages 2>&1); then
  printf '%s\n' "$OUT" | tail -40 >&2
  printf '\nSuite sem docker falhou. Corrija antes de encerrar, ou reporte BLOCKED com este output.\n' >&2
  exit 2
fi

# ------------------------------------- 3. evidencia das suites com dependencia
MISSING=$(CHANGED="$CHANGED" python3 - <<'PY'
import os, pathlib, subprocess, sys, xml.etree.ElementTree as ET

repo = pathlib.Path("/Users/saraiva/Documents/DSE/fase1")
# A lista canonica de suites vem do proprio runner, nunca de uma copia aqui:
# uma suite nova aparece sozinha, e uma copia envelheceria em silencio.
suites = subprocess.run(
    [sys.executable, "scripts/test_matrix.py", "--list"],
    cwd=repo, capture_output=True, text=True,
).stdout.split()
# Ja rodadas na etapa 2.
SEM_DOCKER = {"tests", "packages/contracts", "packages/dse_audit", "packages/dse_identity"}

pendente = {}
for rel in os.environ["CHANGED"].split():
    dono = max((s for s in suites if rel.startswith(s + "/")), key=len, default=None)
    if dono is None or dono in SEM_DOCKER:
        continue
    caminho = repo / rel
    if not caminho.exists():
        continue
    pendente.setdefault(dono, 0)
    pendente[dono] = max(pendente[dono], caminho.stat().st_mtime)

for suite, mtime_fonte in sorted(pendente.items()):
    xml = repo / "test-results" / (suite.replace("/", "-") + ".xml")
    if not xml.exists():
        print(f"{suite}\tnunca rodou (sem {xml.relative_to(repo)})")
        continue
    if xml.stat().st_mtime < mtime_fonte:
        print(f"{suite}\to relatorio e mais antigo que a edicao — a suite nao viu esta mudanca")
        continue
    try:
        raiz = ET.parse(xml).getroot()
    except ET.ParseError:
        print(f"{suite}\trelatorio ilegivel")
        continue
    ts = raiz.iter("testsuite")
    ruim = sum(int(t.get("failures", 0)) + int(t.get("errors", 0)) for t in ts)
    if ruim:
        print(f"{suite}\t{ruim} teste(s) vermelho(s) no ultimo relatorio")
PY
)

if [[ -n "$MISSING" ]]; then
  {
    echo "Sem evidencia de teste para o que esta sessao mudou:"
    echo
    printf '%s\n' "$MISSING" | while IFS=$'\t' read -r suite motivo; do
      echo "  $suite — $motivo"
      # SEMPRE via with_test_database.py. Rodar o pytest direto usa o banco `dse`
      # compartilhado, com outro papel e outro search_path — e as falhas que
      # saem dali sao do ambiente, nao do codigo. Medido: services/platform deu
      # 1 vermelho direto e 123 passed / 0 failed pelo caminho certo, no mesmo
      # commit. Ler aquele vermelho como quebra real custa uma rodada de fix.
      echo "      python scripts/with_test_database.py -- python scripts/test_matrix.py --suite $suite --reports-dir test-results"
      # As dependencias que o CI sobe por grupo (ci.yml). Sem elas a suite falha
      # com connection refused, que NAO e reprovacao do codigo.
      case "$suite" in
        services/validation)               echo "      antes: docker compose up -d garage wse-gitserver" ;;
        services/model-gateway|services/egress-proxy)
          echo "      antes: docker compose up -d model-gateway-echo model-gateway-echo-b model-gateway egress-proxy" ;;
        services/platform)                 echo "      antes: docker compose up -d otel-collector" ;;
      esac
    done
    echo
    echo "Base para qualquer suite: 'make up' (postgres, temporal, redis, vault)."
    # `make up` monta o -f de cada fragmento sozinho; um `docker compose` solto
    # so enxerga o docker-compose.yml e responde "no such service: garage".
    # Medido — foi assim que este proprio aviso saiu errado da primeira vez.
    echo "Para os 'antes:', exporte antes os fragmentos:"
    echo '  export COMPOSE_FILE=$(ls docker-compose.yml docker-compose.ws*.yml | tr "\n" ":")'
    echo "Rode o comando, ou reporte o criterio como IMPLEMENTED-NOT-VERIFIED"
    echo "dizendo por que nao deu para rodar. Falha de conexao nao e reprovacao."
  } >&2
  exit 2
fi

exit 0
