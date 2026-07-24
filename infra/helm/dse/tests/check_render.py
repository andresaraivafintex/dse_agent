#!/usr/bin/env python3
"""Structural checks for a rendered strict-profile manifest.

This deliberately checks only properties visible in YAML. Runtime/CNI/service
semantics remain external pilotReadiness gates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def main(path: str) -> None:
    docs = [doc for doc in yaml.safe_load_all(Path(path).read_text()) if doc]
    workloads = [doc for doc in docs if doc.get("kind") in {"Deployment", "StatefulSet"}]
    require(workloads, "no workloads rendered")

    stateful_writers = {"postgres", "redis", "temporal", "model-server", "vault"}
    for workload in workloads:
        name = workload["metadata"]["name"]
        pod = workload["spec"]["template"]["spec"]
        require(pod.get("serviceAccountName"), f"{name}: missing serviceAccountName")
        # O worker do orchestrator é a ÚNICA exceção ao token não-montado: ele
        # precisa do token para o kubectl que cria os Pods de sandbox. Exigimos
        # que use a SA DEDICADA de menor privilégio (não a global) — o RBAC de
        # criar pods está ligado só a essa identidade.
        if name.endswith("-orchestrator") and pod.get("automountServiceAccountToken") is True:
            require(
                pod.get("serviceAccountName", "").endswith("-orchestrator-worker"),
                f"{name}: monta token mas não usa a SA dedicada do worker",
            )
        else:
            require(pod.get("automountServiceAccountToken") is False, f"{name}: token automount enabled")
        require(pod.get("hostNetwork") is not True, f"{name}: hostNetwork enabled")
        require(pod.get("hostPID") is not True, f"{name}: hostPID enabled")
        require(pod.get("hostIPC") is not True, f"{name}: hostIPC enabled")
        pod_sc = pod.get("securityContext", {})
        require(pod_sc.get("runAsNonRoot") is True, f"{name}: runAsNonRoot missing")
        require(
            pod_sc.get("seccompProfile", {}).get("type") == "RuntimeDefault",
            f"{name}: RuntimeDefault seccomp missing",
        )
        for container in pod.get("containers", []):
            cname = container["name"]
            require("@sha256:" in container.get("image", ""), f"{name}/{cname}: mutable image")
            resources = container.get("resources", {})
            require(resources.get("requests"), f"{name}/{cname}: requests missing")
            require(resources.get("limits"), f"{name}/{cname}: limits missing")
            sc = container.get("securityContext", {})
            require(sc.get("allowPrivilegeEscalation") is False, f"{name}/{cname}: privilege escalation")
            require(sc.get("privileged") is not True, f"{name}/{cname}: privileged")
            require("ALL" in sc.get("capabilities", {}).get("drop", []), f"{name}/{cname}: capabilities not dropped")
            if cname not in stateful_writers:
                require(sc.get("readOnlyRootFilesystem") is True, f"{name}/{cname}: writable rootfs")

        for node in walk(pod):
            if isinstance(node, dict):
                require("hostPath" not in node, f"{name}: hostPath volume present")

    require(not any(doc.get("kind") == "Secret" for doc in docs), "plaintext Secret rendered")
    require(any(doc.get("kind") == "ExternalSecret" for doc in docs), "ExternalSecret missing")

    policies = [doc for doc in docs if doc.get("kind") == "NetworkPolicy"]
    require(policies, "NetworkPolicy missing")
    external = [p for p in policies if p["metadata"]["name"].endswith("egress-proxy-external")]
    require(len(external) == 1, "expected exactly one egress-proxy external policy")
    # Dois — e só dois — donos de CIDR externo são permitidos: (1) a saída do
    # egress-proxy para a internet (allowlist L7 no proxy) e (2) a rota do
    # orchestrator até o API server do K8s (kubectl dos sandboxes), escopada ao
    # pod do orchestrator. Qualquer outra policy com ipBlock é violação.
    kube_api = [p for p in policies if p["metadata"]["name"].endswith("orchestrator-kube-api")]
    allowed_ip_owners = set(map(id, external + kube_api))
    for policy in policies:
        has_ip_block = any(isinstance(node, dict) and "ipBlock" in node for node in walk(policy))
        if has_ip_block:
            require(id(policy) in allowed_ip_owners, f"{policy['metadata']['name']}: unexpected external CIDR")
    selector = external[0]["spec"]["podSelector"]["matchLabels"]
    require(selector.get("app.kubernetes.io/name") == "egress-proxy", "external CIDRs not scoped to proxy")
    if kube_api:
        require(
            kube_api[0]["spec"]["podSelector"]["matchLabels"].get("app.kubernetes.io/name") == "orchestrator",
            "kube-api egress not scoped to the orchestrator pod",
        )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} RENDERED_YAML")
    main(sys.argv[1])
