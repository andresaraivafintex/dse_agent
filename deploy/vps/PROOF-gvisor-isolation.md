# gVisor isolation proof — evidence for the pilotReadiness.sandboxIsolationVerified gate

Executed on 2026-07-24 on the pilot VPS (172.172.235.228, Ubuntu 24.04, k3s
v1.31.4+k3s1, containerd v1.7.23-k3s2, gVisor/runsc via RuntimeClass `gvisor`).
Pod hardened identically to `build_pod_manifest` (runAsNonRoot 10001, cap drop ALL,
readOnlyRootFilesystem, seccomp RuntimeDefault, SA token not mounted).

## What was proved (commands run via `kubectl exec` in the real pod)

1. **The kernel IS gVisor, not the host's.**
   `uname -a` → `Linux dse-gvisor-proof 4.19.0-gvisor #1 SMP ... x86_64`
   (the host runs `6.17.0-1020-azure`). Real OS-level isolation — the agent's
   syscalls go through runsc, not the node's kernel.

2. **Full turn flow INSIDE the isolated pod** (the runner ops):
   - `--op bootstrap` → git workspace created, `created:true`, real sha.
   - `--stage coder` (fake) → wrote `src/proof.py` + `JUNK_REPORT.md`.
   - `--op post_turn` → `pruned:["JUNK_REPORT.md"]`, commit made (sha changed).

3. **Escape denied by the OPERATING SYSTEM, not by the toolset:**
   - a turn attempting to write `/pwned.txt` →
     `error_kind:"substrate_error"`, `PermissionError [Errno 13]`.
   - `touch /pwned2` → `Permission denied` (read-only rootfs).
   - `id` → `uid=10001(dse)` (non-root).

## Infra fix folded into the bootstrap

k3s's containerd runsc handler requires `config.toml.tmpl` with the
`io.containerd.grpc.v1.cri` plugin (not `config-v3.toml.tmpl` / `cri.v1.runtime`);
with the wrong path the Pod sits in ContainerCreating with "no runtime for runsc
is configured". `bootstrap-k3s.sh` is already fixed.

## Consequence for the gates

This is the external evidence the Helm chart requires for
`pilotReadiness.sandboxIsolationVerified`. Promoting the flag to `true`
remains the release pipeline's job (after attaching this proof to the release
artifact), as the chart's design dictates — never hand-edited in the values.
