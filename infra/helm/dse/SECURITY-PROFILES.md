# Perfis de segurança do chart DSE

## Desenvolvimento

`values.yaml` e `values-dev.yaml` mantêm compatibilidade com o ambiente local.
Eles permitem execução in-process, fixture de modelo, Vault dev e imagens por
tag. Não são aceitos como entrada de um piloto.

```sh
helm lint infra/helm/dse
helm template dse-dev infra/helm/dse -f infra/helm/dse/values-dev.yaml
```

## Piloto e produção

`values-pilot.yaml` é propositalmente bloqueado. O pipeline de release deve
fornecer um overlay do tenant com endpoints, Build ID e digests reais e só pode
promover os gates `pilotReadiness` depois dos testes externos correspondentes.
`values-production.yaml` é aplicado depois do baseline de piloto e não relaxa
nenhuma validação.

O template recusa, entre outros:

- runtime in-process/local fallback, substrate de agente fixture e RuntimeClass
  ausente;
- Worker Versioning desligado, Build ID `dev` ou ainda placeholder;
- Vault dev, Vault sem TLS, password no values e ESO desligado;
- `ClusterSecretStore` no perfil estrito, evitando acesso cross-namespace;
- ausência de model gateway, egress proxy/credential broker, ledger, Postgres, Temporal,
  NetworkPolicy ou OTel;
- imagens por tag, ServiceAccount token montado, privilege escalation e rootfs
  gravável nos workloads da aplicação;
- wildcard/porta implícita na allowlist e saída externa sem CIDRs explícitos.

## O que o Helm não prova

Renderizar o perfil não torna o piloto pronto. Os gates existem justamente
para não transformar configuração declarativa em evidência falsa:

- `sandboxIsolationVerified`: hoje `runtime.inProcess=false` falha fechado em
  K8s porque o chart não monta Docker socket. É necessário um provisionador
  Kubernetes real e um teste adversarial que prove que o processo do agente só
  existe no sandbox.
- `modelGatewayFailClosedVerified`: requer teste do digest promovido com
  provider/key real, gateway indisponível e zero fixture.
- `egressStrictPolicyVerified`: a implementação atual também possui defaults
  internos e não usa o arquivo montado como fonte exclusiva. Um digest novo
  deve provar config estrita, bloqueio de SSRF/DNS rebinding e auditoria.
- `auditLedgerMigrationsVerified`: o chart cria/sincroniza credenciais, mas não
  aplica nem verifica migrations/grants append-only.
- `workerVersioningRegistrationVerified`: o chart passa Build ID e ativa a
  opção no worker; o registro na task queue e o drain continuam sendo uma etapa
  externa do pipeline Temporal.

O egress-proxy usa probe TCP porque o binário atual não expõe `/health`; isso
prova apenas que o listener abriu. A validação semântica permanece no gate de
egress. O Deployment standalone de `validation` fica desligado no overlay de
piloto porque não há servidor HTTP correspondente comprovado.

Kubernetes NetworkPolicy não filtra FQDN. O chart concede CIDRs externos apenas
ao proxy e bloqueia redes privadas/metadata no exemplo; hostname e porta são
responsabilidade da política L7 do proxy. O CNI real precisa ser testado antes
do gate de piloto.

## Testes locais

```sh
infra/helm/dse/tests/test_profiles.sh
```

A fixture em `tests/values-pilot-render.yaml` contém digests sintaticamente
válidos, mas inexistentes. Ela testa somente a lógica do template e nunca deve
ser usada num deploy.
