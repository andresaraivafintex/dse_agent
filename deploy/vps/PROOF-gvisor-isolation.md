# Prova de isolamento sob gVisor — evidência do gate pilotReadiness.sandboxIsolationVerified

Executada em 2026-07-24 na VPS piloto (172.172.235.228, Ubuntu 24.04, k3s
v1.31.4+k3s1, containerd v1.7.23-k3s2, gVisor/runsc via RuntimeClass `gvisor`).
Pod endurecido igual ao `build_pod_manifest` (runAsNonRoot 10001, cap drop ALL,
readOnlyRootFilesystem, seccomp RuntimeDefault, SA token não montado).

## O que foi provado (comandos via `kubectl exec` no pod real)

1. **O kernel É o gVisor, não o do host.**
   `uname -a` → `Linux dse-gvisor-proof 4.19.0-gvisor #1 SMP ... x86_64`
   (host roda `6.17.0-1020-azure`). Isolamento de SO real — as syscalls do
   agente passam pelo runsc, não pelo kernel do nó.

2. **Fluxo completo do turno DENTRO do pod isolado** (as ops do runner):
   - `--op bootstrap` → workspace git criado, `created:true`, sha real.
   - `--stage coder` (fake) → escreveu `src/proof.py` + `JUNK_REPORT.md`.
   - `--op post_turn` → `pruned:["JUNK_REPORT.md"]`, commit feito (sha mudou).

3. **Escape negado pelo SISTEMA OPERACIONAL, não por toolset:**
   - turno tentando escrever `/pwned.txt` →
     `error_kind:"substrate_error"`, `PermissionError [Errno 13]`.
   - `touch /pwned2` → `Permission denied` (rootfs read-only).
   - `id` → `uid=10001(dse)` (não-root).

## Correção de infra incorporada ao bootstrap

O handler runsc do containerd do k3s exige `config.toml.tmpl` com o plugin
`io.containerd.grpc.v1.cri` (não `config-v3.toml.tmpl` / `cri.v1.runtime`);
com o path errado o Pod fica em ContainerCreating com "no runtime for runsc
is configured". `bootstrap-k3s.sh` já corrigido.

## Consequência para os gates

Esta é a evidência externa que o chart Helm exige para
`pilotReadiness.sandboxIsolationVerified`. A promoção do flag para `true`
permanece sendo do pipeline de release (após anexar esta prova ao artefato do
release), como o desenho do chart determina — nunca editada à mão no values.
