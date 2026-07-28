#!/usr/bin/env bash
# Preflight for step 4 of deploy/vps/README.md — run this BEFORE any helm upgrade
# on the VPS. It refuses the upgrade if the checkout you are about to deploy from
# renders fewer resources than the live release, i.e. if the upgrade would DELETE
# something that is running.
#
# Why this exists: on 2026-07-28 the on-box checkout was 17 commits behind and its
# chart had no console/queue-board/dispatcher/jira/reaper templates. The documented
# command rendered cleanly, exited 0, reported success — and would have deleted 26 of
# the 62 live resources, including all three Ingresses, the auth Middleware and the
# console-api PVC.
#
# Usage (on the VPS):
#   bash deploy/vps/preflight-upgrade.sh                       # default POC values
#   bash deploy/vps/preflight-upgrade.sh -f a.yaml -f b.yaml   # explicit values
#
# Env: DSE_REPO (default $HOME/dse_agent), RELEASE (dse), NS (dse), REF (origin/main).
set -euo pipefail

DSE_REPO="${DSE_REPO:-$HOME/dse_agent}"
RELEASE="${RELEASE:-dse}"
NS="${NS:-dse}"
REF="${REF:-origin/main}"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

die() { printf '\n\033[31mPREFLIGHT BLOQUEADO\033[0m: %s\n\n' "$1" >&2; exit 1; }
ok()  { printf '\033[32m  ok\033[0m  %s\n' "$1"; }

command -v helm >/dev/null || die "helm não encontrado no PATH"
[ -d "$DSE_REPO/.git" ] || die "$DSE_REPO não é um checkout git (defina DSE_REPO)"
cd "$DSE_REPO"

VALUES=("$@")
if [ ${#VALUES[@]} -eq 0 ]; then
  VALUES=(-f infra/helm/dse/values-dev.yaml -f deploy/vps/values-vps-poc.yaml)
fi

echo "preflight: $DSE_REPO  →  release $RELEASE/$NS"

# 1 ─ higiene do checkout ------------------------------------------------------
[ -z "$(git status --porcelain)" ] \
  || die "working tree suja em $DSE_REPO. Commit ou descarte antes de deployar."
ok "working tree limpa"

git fetch --quiet origin || die "git fetch falhou (chave de deploy? rede?)"
LOCAL="$(git rev-parse HEAD)"
TARGET="$(git rev-parse "$REF")"
if [ "$LOCAL" != "$TARGET" ]; then
  die "checkout em $(git rev-parse --short HEAD) mas $REF está em $(git rev-parse --short "$REF") ($(git rev-list --count HEAD.."$REF") commits atrás).
  Corrija com:  git -C $DSE_REPO merge --ff-only $REF
  NÃO use 'git checkout v0.1.0-rc.N' — o values-vps-poc.yaml dentro da tag rc.N
  ainda aponta para as imagens rc.N-1. O alvo é sempre $REF."
fi
ok "checkout em $(git rev-parse --short HEAD) == $REF"

# 2 ─ inventário: o que existe vivo vs o que este checkout renderiza -----------
inventory() {  # stdin: manifesto yaml → stdout: Kind/name, um por linha
  awk 'BEGIN { RS="\n---\n" }
  {
    kind=""; name=""; inmeta=0
    n=split($0, L, "\n")
    for (i=1; i<=n; i++) {
      l=L[i]
      if (l ~ /^kind: /)                  { kind=substr(l,7) }
      else if (l ~ /^metadata:$/)         { inmeta=1 }
      else if (inmeta && l ~ /^  name: /) { name=substr(l,9); inmeta=0 }
      else if (inmeta && l ~ /^[^ ]/)     { inmeta=0 }
    }
    if (kind != "" && name != "") print kind "/" name
  }' | sort -u
}

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

helm get manifest "$RELEASE" -n "$NS" > "$TMP/live.yaml" \
  || die "release $RELEASE não existe em $NS — se for a PRIMEIRA instalação, pule o preflight."
helm template "$RELEASE" infra/helm/dse -n "$NS" "${VALUES[@]}" --is-upgrade --no-hooks > "$TMP/cand.yaml" \
  || die "o chart deste checkout não renderiza com esses values (veja o erro acima)"

inventory < "$TMP/live.yaml" > "$TMP/live.inv"
inventory < "$TMP/cand.yaml" > "$TMP/cand.inv"

GONE="$(comm -23 "$TMP/live.inv" "$TMP/cand.inv")"
NEW="$(comm -13 "$TMP/live.inv" "$TMP/cand.inv")"

if [ -n "$GONE" ]; then
  printf '\n\033[31mEste upgrade APAGARIA %s recurso(s) que estão no ar:\033[0m\n' "$(echo "$GONE" | wc -l | tr -d ' ')" >&2
  echo "$GONE" | sed 's/^/    - /' >&2
  die "abortado. O checkout/values não correspondem ao que está em produção."
fi
ok "nenhum recurso vivo desapareceria ($(wc -l < "$TMP/live.inv" | tr -d ' ') recursos conferidos)"

[ -n "$NEW" ] && { echo "  novos recursos que seriam criados:"; echo "$NEW" | sed 's/^/    + /'; }

# 3 ─ diferenças de imagem (informativo, não bloqueia) -------------------------
grep -o 'image: [^ ]*' "$TMP/live.yaml" | sort -u > "$TMP/live.img"
grep -o 'image: [^ ]*' "$TMP/cand.yaml" | sort -u > "$TMP/cand.img"
if ! diff -q "$TMP/live.img" "$TMP/cand.img" >/dev/null; then
  echo "  imagens que mudam neste upgrade:"
  diff "$TMP/live.img" "$TMP/cand.img" | grep -E '^[<>]' | sed 's/^</    - antes:/; s/^>/    + depois:/'
else
  ok "nenhuma imagem muda"
fi

printf '\n\033[32mpreflight ok\033[0m — pode rodar o helm upgrade do passo 4.\n\n'
