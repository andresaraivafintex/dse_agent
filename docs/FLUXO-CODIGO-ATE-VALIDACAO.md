# Do pedido ao veredito: como o DSE escreve e valida código hoje, e por que ele não converge

**Status:** diagnóstico, não proposta de implementação
**Base de evidência:** ledger de produção das últimas 24 h (31 execuções de L1, 12 work items) e leitura do código em `main` (`d3b3dc1`)
**Escopo:** o caminho `Planner → Coder → Tester → L1 → PR`, e apenas ele

---

## 1. O número que define o problema

Nas últimas 24 horas, na VPS:

| | |
|---|---|
| work items criados | **12** |
| que chegaram a `review_ready` | **1** |
| execuções do gate L1 | **31** |
| execuções do L1 que passaram | **3** |

Vinte e oito execuções de um gate que leva ~7 minutos cada, para reprovar. Isso é
aproximadamente **3 h 20 min de relógio** gastas exclusivamente em rodadas que
não avançaram.

O que reprovou, agrupado por gate:

| gate | rodadas em que reprovou |
|---|---|
| `build` | **20** |
| `test` | 18 |
| `lint` | 8 |
| `typecheck` | 7 |
| `secret_scan` | 5 |
| `l1_manifest` | 1 |

E agrupado pela mensagem exata:

| mensagem | rodadas |
|---|---|
| `build failed (exit=1)` | **19** |
| `summary: 403 errors` | 8 |
| `secret scanner failed (exit=1)` | 5 |
| `4 type error(s) in the files this change touched` | 4 |
| `lint could not run: the process was killed (exit=134)` | 4 |
| `lint could not run: the process was killed (exit=137)` | 3 |
| `summary: 275 passed` | 3 |

Uma única mensagem — `build failed (exit=1)` — respondeu por **19 das 31
rodadas**. Não são dezenove problemas. Nas execuções em que eu li o detalhe, era
sempre a mesma classe de erro de tipagem de template, e três vezes seguidas o
**mesmo** erro, literalmente na mesma linha.

---

## 2. Como o fluxo funciona hoje

### 2.1 A sequência

```
intake (Slack/Jira/GitHub)
   └─ ingest gateway  →  resolve tenant, resolve repo (cascata determinística)
        └─ WorkItemLifecycleWorkflow  (Temporal)
             ├─ route_repos            ← modelo, se a cascata não decidiu
             ├─ planner_turn           ← lê o repositório, produz PlanArtifact
             ├─ provision_sandbox      ← 1 Pod por work item, clone real
             ├─ run_coder_turn         ← escreve código de produção
             ├─ run_tester_turn        ← escreve testes, roda os que escreveu
             ├─ run_l1_pipeline        ← 7 estágios, em série
             └─ finalize_pr
```

Se o L1 reprova, o fluxo **volta ao Coder** com `fix_context` e a rodada
recomeça. É esse laço que não fecha.

### 2.2 Custo medido de uma rodada

Da própria VPS (4 vCPU), média de 31 execuções:

| etapa | tempo |
|---|---|
| `provision_sandbox` | ~48 s |
| `run_coder_turn` | ~3 min |
| `run_tester_turn` | ~2 min |
| `run_l1_pipeline` | **~7 min** |
| **rodada completa** | **~12 min** + um turno pago de modelo |

Dentro do L1, também medido:

| estágio | ordem | tempo médio |
|---|---|---|
| `lint` | 1º | 79 s |
| `typecheck` | 2º | 14 s |
| `test` | 3º | **297 s** |
| `build` | 4º | 42 s |
| `sast`, `secret_scan`, `plan_compliance` | 5º–7º | < 15 s |

### 2.3 O que cada agente pode fazer

Isto está em `services/sandbox-runtime/sandbox_runtime/toolsets.py`, e é
deliberado — o módulo explica cada restrição:

| agente | ferramentas | onde |
|---|---|---|
| **Planner** | só leitura; qualquer escrita levanta `ToolPermissionError` | `PlannerToolset` |
| **Tester** | leitura + `run_tests` + escrita **apenas** em caminhos de teste | `TesterToolset` |
| **Reviewer** | contexto novo: só `read_plan` / `read_diff` | `ReviewerToolset` |
| **Coder** | *(não existe `CoderToolset`)* | — |

`activities.py:806` cria a sessão do Coder com
`agent.run_turn(inp.instruction, ...)` — **sem argumento de toolset**, ao
contrário do Planner (`toolset=PlannerToolset()`, linha 1725) e do Tester
(`toolset=TesterToolset(...)`, linha 3265). O Coder herda as ferramentas padrão
do substrato.

### 2.4 Por onde o conhecimento do repositório chega

| canal | chega em |
|---|---|
| `AGENTS.md` | **só no Planner** (`_repo_docs_for_planner`, activities.py:1132-1179) |
| `.claude/skills/*/SKILL.md` | Coder (`activities.py:787`) e Tester (`activities.py:2181`) |
| árvore do repositório | só no Planner |
| um "teste de exemplo" | só no Tester — e é `sorted(existing)[:3]`, o **primeiro em ordem alfabética** do repositório inteiro |
| `.dse/validation.json` | ninguém: é lido pelo L1, do commit base |

---

## 3. Hipóteses para o loop

Em ordem de força da evidência.

### H1 — O Coder escreve sem nunca executar nada. **(provado)**

**Mecanismo.** O Coder tem as ferramentas do substrato, incluindo execução de
comandos — não há `CoderToolset` restringindo-o. Mas nada na instrução dele
manda verificar. O que `activities.py:768-787` acrescenta são apenas
proibições: não edite testes, não crie relatórios, fique nos arquivos do plano.
Não há uma linha dizendo *"rode `npm run build` antes de terminar"*.

**Consequência medida.** O erro que dominou as últimas rodadas —

```
NG2: Type 'string' is not assignable to type
     '"success" | "secondary" | "info" | "warn" | "danger" | "contrast"'
```

— é detectado por `npm run build` em **42 segundos**. O Coder o produziu e
entregou sem rodar. Foi descoberto **12 minutos depois**, três rodadas seguidas.

**Por que isso é a hipótese principal.** Ela explica os 19 `build failed` de uma
vez, e explica por que consertar o *relato* do L1 (o que fiz durante o dia) não
resolveu: melhorei a mensagem que chega ao Coder, mas o gargalo não é a
qualidade da mensagem, é **quando** ela chega.

**Como matar a hipótese.** Instruir o Coder a rodar `npm run build` antes de
declarar `done` e medir quantas rodadas passam a reprovar em `build`. Se
continuar reprovando, a hipótese está errada.

---

### H2 — O L1 não tem falha-rápida, e o gate mais decisivo é o penúltimo. **(provado)**

**Mecanismo.** `pipeline.py:120-130` executa os sete estágios
incondicionalmente:

```python
findings.append(_timed(step, "lint", ...))
findings.append(_timed(step, "typecheck", ...))
findings.append(_timed(step, "test", ...))
findings.append(_timed(step, "build", ...))
...
passed = all(f.passed for f in findings)
```

Não há `if not passed: return` em lugar nenhum. Um `build` que vai reprovar
espera os 297 s do `test` — e os testes passam, porque o erro é de compilação de
template, coisa que o jest nunca vê.

**Aritmética.** Para o erro dominante, hoje se paga `79 + 14 + 297 = 390 s`
antes de chegar ao `build`. Numa ordem barato-e-decisivo-primeiro
(`typecheck 14 → build 42 → lint 79 → test 297`), o mesmo erro apareceria em
**56 s**. São **~5,5 minutos por rodada reprovada**, e houve 28 delas em 24 h.

**Nuance importante.** Isso reduz o custo do erro, mas não o evita. É
complementar a H1, não alternativa: H1 evita a rodada, H2 barateia a que
acontecer.

---

### H3 — O conhecimento específico do repositório não tem por onde entrar no Coder até alguém escrever à mão. **(forte)**

**Mecanismo.** As três falhas de hoje foram lacunas de conhecimento sobre
*este* repositório:

1. specs instanciando componente sem o provider do NgRx `Store`;
2. `.set()` num sinal `input()`, que é somente leitura;
3. `computed` tipado como `string` onde o PrimeNG exige união de literais.

Nenhuma é defeito de plataforma. São coisas que um dev novo no projeto erraria
uma vez, aprenderia, e não repetiria.

O `AGENTS.md` — o lugar óbvio para documentá-las — **não chega ao Coder nem ao
Tester**. Só ao Planner. Descobri isso da pior maneira: escrevi a regra do
`provideMockStore` lá de manhã e o mesmo erro voltou duas vezes.

O canal que funciona é `.claude/skills/`, e nele **um humano tem que escrever a
regra depois de cada erro**. Não encontrei nenhum mecanismo pelo qual o sistema
registre o que aprendeu numa rodada reprovada.

**Como confirmar ou matar.** Procurar por qualquer escrita automática em
`.claude/skills`, `repo_profiles` ou equivalente originada de uma falha de gate.
Se não existir, a hipótese está confirmada: o aprendizado é 100% manual.

---

### H4 — O "teste de exemplo" dado ao Tester é escolhido por ordem alfabética. **(provado)**

`activities.py:2160`:

```python
for candidate in sorted(existing)[:3]:
```

O Tester recebe a instrução *"use EXATAMENTE o runner e o estilo do TESTE
EXISTENTE mostrado abaixo"* — e o teste mostrado é o primeiro em ordem
alfabética do repositório inteiro, sem relação com o que ele está testando. Ele
copiou fielmente um `TestBed` de um componente que não injeta `Store`.

O vizinho do arquivo alterado seria a escolha certa e está disponível: o diff já
é lido no mesmo contexto (`git show --stat -p HEAD`).

---

### H5 — Falhas de infraestrutura contam como reprovação de código e gastam rodadas. **(forte)**

Das 31 rodadas:

- **7** foram `lint could not run: the process was killed` (exit 134/137) — o V8
  dimensiona o heap pela memória da máquina, não do contêiner, e o cgroup mata o
  processo. Corrigido hoje no manifesto do repositório, mas cada ocorrência
  custou uma rodada;
- **5** foram `secret scanner failed (exit=1)` — **não investigado**. Cinco
  rodadas reprovadas por um scanner que errou, não por código inseguro;
- **1** foi `l1_manifest` — que o rótulo do workflow reportava como "o repo
  precisa de `.dse/validation.json`" quando o arquivo estava lá e apenas tinha
  um timeout grande demais.

O `_infra_failure` já classifica alguns desses como `ERROR` em vez de `FAIL`,
mas o workflow os trata igual: `coder_retry_count += 1` e mais uma rodada. O
plano (§8.5) já prevê o correto — *"`environment` e `dependency` repetem somente
a Activity afetada; não entram automaticamente no Coder"* — e isso não está
implementado.

---

### H6 — A mesma suíte roda duas vezes por rodada. **(provado, já parcialmente corrigido)**

O Tester rodava os 4 975 testes para responder *"os 2 specs que eu escrevi
funcionam?"*, e o L1 rodava os mesmos 4 975 minutos depois. Corrigido hoje
(#62): o Tester passou a rodar só os arquivos que escreveu, e o turno dele caiu
de ~9 min para ~2 min, medido em produção.

Fica registrado porque explica parte do custo histórico das rodadas.

---

## 4. O que já foi corrigido hoje, e o que isso ensinou

| conserto | efeito medido |
|---|---|
| `detail` do L1 lia o fluxo errado (`stdout or stderr`) | o motivo real passou a aparecer; foi o que permitiu diagnosticar tudo acima |
| resumo do `test` era do pytest, e o jest inverte a ordem | `summary: 275 passed` numa reprovação virou a contagem verdadeira |
| Tester rodava a suíte inteira | turno de ~9 min → ~2 min |
| turno do Coder que não move arquivo re-armava os gates | 51 min medidos de rodadas idênticas, agora cortadas |
| Tester não podia reparar o próprio spec | acumulava cópias `-dse`; posse agora é pergunta ao git |
| roteador desistia num 502 de segundos | item parava esperando humano; agora retenta |

**A lição.** Todos esses consertos eram reais e necessários, e nenhum deles
atacou H1 ou H2. Eu estava melhorando o *diagnóstico* e o *controle do laço*
enquanto a causa do custo estava em **onde a validação acontece**.

---

## 5. O que eu não sei

Registrado explicitamente, porque presumir foi o erro recorrente destes três
dias:

1. **Quais ferramentas exatamente o substrato de produção entrega ao Coder.**
   Deduzi da ausência de `CoderToolset` e do `run_turn` sem restrição. Falta ler
   `ClaudeAgentSubstrate` e confirmar que `bash` está lá.
2. **Se rodar o build dentro do turno cabe no orçamento do Coder.** O turno tem
   `max_turns = 8` (`activities.py:802`). Um build de 42 s por turno é
   aceitável; não sei se o substrato conta isso contra algum limite.
3. **Por que o `secret_scan` reprovou 5 vezes.** Nunca olhei.
4. **Se o `test` de 297 s pode cair.** Dentro do gVisor o `nproc` devolve 3 e o
   jest cai em execução sequencial; medi 219 s em fila contra 153 s com dois
   workers, mas com três o cgroup mata a suíte.

---

## 6. Ordem de ataque sugerida

Pela razão custo-benefício, não por elegância:

1. **H1 — mandar o Coder validar o próprio trabalho.** É a única mudança que
   *elimina* rodadas em vez de baratear. Não exige contrato novo nem migração:
   é instrução mais a ferramenta que ele já tem.
2. **H2 — reordenar o L1 e falhar rápido.** ~5,5 min por rodada reprovada, sem
   perder um gate sequer: a ordem muda, o conjunto não.
3. **H5 — separar falha de infraestrutura de veredito sobre o código.** Já está
   escrito no plano §8.5 e não implementado.
4. **H4 — escolher o teste de exemplo pela proximidade com o diff.**
5. **H3 — dar ao sistema um lugar onde registrar o que aprendeu.** É o mais
   ambicioso e o que menos urge: com H1 no lugar, o Coder descobre sozinho boa
   parte do que hoje precisa de regra escrita à mão.

**A não fazer agora:** o `ChangeGroupWorkflow` do plano multi-repo. Ele coordena
entregas em dois repositórios e está correto no que propõe, mas parte da
premissa de que a entrega **de um** repositório converge. Hoje ela não converge,
e construir a coordenação por cima multiplicaria a superfície de falha por dois.
