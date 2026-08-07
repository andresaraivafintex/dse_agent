#!/bin/bash
# Uma release, ponta a ponta: CI verde -> merge -> tag -> imagem -> pin -> VPS.
#
#   bash .fable/ship.sh <numero-da-pr> <rc>       ex: bash .fable/ship.sh 60 32
#
# Cada passo VERIFICA antes de seguir. Nenhum passo presume o anterior: as
# noites perdidas nesse projeto foram todas de um passo que reportou sucesso
# sem ter acontecido.
set -u
PR="${1:?uso: ship.sh <pr> <rc>}"
RC="${2:?uso: ship.sh <pr> <rc>}"
TAG="v0.1.0-rc.${RC}"
REPO_DIR=/Users/saraiva/Documents/DSE/fase1
CTRL="$REPO_DIR/.fable/OVERNIGHT.md"
cd "$REPO_DIR" || exit 1

log(){ echo "[$(date -u +%H:%M:%S)] $*"; echo "- $(date -u +%H:%M) — $*" >> "$CTRL"; }
die(){ log "ABORTADO: $*"; exit 1; }

# ---------------------------------------------------------------- 1. CI verde
log "rc.$RC: verificando CI da #$PR"
for i in $(seq 1 80); do
  # A COLUNA de status, nunca o nome: existe um job chamado
  # "helm profiles and fail-closed policy", e um grep por "fail" sempre casa.
  PEND=$(gh pr checks "$PR" 2>&1 | awk -F'\t' '$2=="pending"' | wc -l | tr -d ' ')
  [ "$PEND" -eq 0 ] && break
  sleep 45
done
FAILED=$(gh pr checks "$PR" 2>&1 | awk -F'\t' '$2=="fail"' | wc -l | tr -d ' ')
[ "$FAILED" -eq 0 ] || die "CI da #$PR tem $FAILED job(s) vermelhos — nao vou mergear em vermelho"
log "rc.$RC: CI verde"

# ------------------------------------------------------------------ 2. merge
gh pr merge "$PR" --squash --delete-branch 2>&1 | tail -1
git fetch -q origin
git checkout -q main && git reset -q --hard origin/main
MERGED=$(git log --oneline -1)
log "rc.$RC: merge feito — $MERGED"

# -------------------------------------------------------------------- 3. tag
git tag -f "$TAG" && git push -q -f origin "$TAG" 2>&1 | tail -1
log "rc.$RC: tag $TAG empurrada"

# ------------------------------------------------------------ 4. imagem pronta
sleep 20
RUN=$(gh run list --workflow=release.yml --limit 1 --json databaseId --jq '.[0].databaseId')
log "rc.$RC: build da imagem $RUN"
for i in $(seq 1 80); do
  ST=$(gh run view "$RUN" --json status,conclusion --jq '.status+":"+(.conclusion//"")' 2>/dev/null)
  case "$ST" in
    completed:success) break;;
    completed:*) die "build da imagem terminou como $ST";;
  esac
  sleep 45
done
[ "${ST:-}" = "completed:success" ] || die "build da imagem nao terminou a tempo"
log "rc.$RC: imagem publicada"

# --------------------------------------------------------------------- 5. pin
# O pin entra no commit DEPOIS da tag: por isso a VPS sempre deploya de
# origin/main e nunca da tag (a tag rc.N ainda aponta para as imagens rc.N-1).
sed -i '' "s|_ghcrTag: &ghcrTag \".*\"|_ghcrTag: \&ghcrTag \"$TAG\"|" deploy/vps/values-vps-poc.yaml
grep -q "$TAG" deploy/vps/values-vps-poc.yaml || die "o pin nao entrou no values-vps-poc.yaml"
git add deploy/vps/values-vps-poc.yaml
git commit -q -m "Point the VPS at rc.$RC

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -q origin main
log "rc.$RC: pin empurrado ($(git log --oneline -1 | cut -c1-7))"

# ------------------------------------------------------------------ 6. deploy
log "rc.$RC: deploy na VPS"
ssh dse-vps 'bash -s' <<'REMOTE' 2>&1 | tail -25
set -u
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
cd ~/dse_agent || exit 1
git fetch -q origin && git reset -q --hard origin/main
echo "checkout: $(git log --oneline -1)"
bash deploy/vps/preflight-upgrade.sh || { echo "PREFLIGHT ABORTOU"; exit 1; }
helm upgrade --install dse infra/helm/dse \
  -f infra/helm/dse/values-dev.yaml -f deploy/vps/values-vps-poc.yaml -n dse 2>&1 | tail -6
REMOTE
[ "${PIPESTATUS[0]}" -eq 0 ] || die "o deploy falhou na VPS"

# --------------------------------------------------- 7. verificar NA MAQUINA
sleep 45
ssh dse-vps 'bash -s' <<'REMOTE'
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
echo "=== deployments nao prontos ==="
sudo k3s kubectl -n dse get deploy -o json | python3 -c "
import sys,json
d=json.load(sys.stdin)
bad=[i['metadata']['name'] for i in d['items']
     if i['status'].get('readyReplicas',0)!=i['spec']['replicas']]
print('  todos prontos' if not bad else '  NAO PRONTOS: '+', '.join(bad))"
echo "=== imagem em uso ==="
sudo k3s kubectl -n dse get deploy -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].image}{"\n"}{end}' \
  | sed 's|.*:||' | sort | uniq -c
REMOTE
log "rc.$RC NO AR"
