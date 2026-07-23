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
    for policy in policies:
        has_ip_block = any(isinstance(node, dict) and "ipBlock" in node for node in walk(policy))
        if has_ip_block:
            require(policy is external[0], f"{policy['metadata']['name']}: unexpected external CIDR")
    selector = external[0]["spec"]["podSelector"]["matchLabels"]
    require(selector.get("app.kubernetes.io/name") == "egress-proxy", "external CIDRs not scoped to proxy")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} RENDERED_YAML")
    main(sys.argv[1])
