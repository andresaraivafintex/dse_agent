# Security profiles of the DSE chart

## Development

`values.yaml` and `values-dev.yaml` keep compatibility with the local environment.
They allow in-process execution, a model fixture, dev Vault and images by
tag. They are not accepted as a pilot input.

```sh
helm lint infra/helm/dse
helm template dse-dev infra/helm/dse -f infra/helm/dse/values-dev.yaml
```

## Pilot and production

`values-pilot.yaml` is deliberately locked down. The release pipeline must
supply a tenant overlay with real endpoints, Build ID and digests, and may only
promote the `pilotReadiness` gates after the corresponding external tests.
`values-production.yaml` is applied after the pilot baseline and does not relax
any validation.

The template rejects, among other things:

- in-process/local-fallback runtime, a fixture agent substrate and a missing
  RuntimeClass;
- Worker Versioning disabled, Build ID `dev` or still a placeholder;
- dev Vault, Vault without TLS, a password in values and ESO disabled;
- a `ClusterSecretStore` in the strict profile, avoiding cross-namespace access;
- absence of a model gateway, egress proxy/credential broker, ledger, Postgres, Temporal,
  NetworkPolicy or OTel;
- images by tag, a mounted ServiceAccount token, privilege escalation and a writable
  rootfs on application workloads;
- a wildcard/implicit port in the allowlist and external egress without explicit CIDRs.

## What Helm does not prove

Rendering the profile does not make the pilot ready. The gates exist precisely
so that declarative configuration is not turned into false evidence:

- `sandboxIsolationVerified`: today `runtime.inProcess=false` fails closed on
  K8s because the chart does not mount a Docker socket. A real Kubernetes
  provisioner is required, plus an adversarial test proving that the agent process only
  exists inside the sandbox.
- `modelGatewayFailClosedVerified`: requires testing the promoted digest with a
  real provider/key, the gateway unavailable and zero fixtures.
- `egressStrictPolicyVerified`: the current implementation also has internal
  defaults and does not use the mounted file as the sole source. A new digest
  must prove strict config, SSRF/DNS-rebinding blocking and auditing.
- `auditLedgerMigrationsVerified`: the chart creates/syncs credentials, but does not
  apply or verify append-only migrations/grants.
- `workerVersioningRegistrationVerified`: the chart passes the Build ID and enables the
  option on the worker; registration on the task queue and the drain remain an
  external step of the Temporal pipeline.

The egress-proxy uses a TCP probe because the current binary does not expose `/health`; that
proves only that the listener opened. Semantic validation stays in the egress
gate. The standalone `validation` Deployment is disabled in the pilot overlay
because there is no proven corresponding HTTP server.

Kubernetes NetworkPolicy does not filter by FQDN. The chart grants external CIDRs only
to the proxy and blocks private/metadata networks in the example; hostname and port are
the responsibility of the proxy's L7 policy. The real CNI must be tested before
the pilot gate.

## Local tests

```sh
infra/helm/dse/tests/test_profiles.sh
```

The fixture in `tests/values-pilot-render.yaml` contains syntactically
valid but non-existent digests. It tests template logic only and must never
be used in a deploy.
