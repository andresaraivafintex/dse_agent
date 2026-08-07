---
name: fable-it
description: Runs a long unattended job to a numbered Definition of Done where every criterion is a command, reporting per-criterion status backed by exit codes. Use when the user gives a goal plus numbered acceptance criteria and asks to run unattended — "work until done", "run to DoD", "I'm going to bed, finish this", "green light, take decisions". Not for attended work where the user is at the keyboard, and not for exploratory tasks with no checkable criteria — those need a plan and checkpoints instead.
---

# fable-it — entrega autônoma verificável

Governa **como** um trabalho longo não supervisionado roda: mantém decisões tomadas cedo, termina cada critério em evidência, e reporta honestamente o que não deu para confirmar.

Delegue para outras skills disponíveis (`launch`, `iterate`, `full-qa`, controle de browser) em vez de reimplementá-las. Se alguma estiver ausente, rode a fase inline e registre a ausência no relatório.

## Entradas obrigatórias

1. **Objetivo** — uma frase do que "pronto" entrega.
2. **DoD numerado** — critérios de aceitação discretos.

Se faltar qualquer um dos dois, pergunte. Nunca invente critério de aceitação.

**Teto: 5 critérios por run.** Acima disso o contexto compacta no meio e o trabalho pós-compactação passa a contradizer as decisões anteriores. Divida em runs.

**Teto de turnos: pare e reporte após 3 tentativas falhas no mesmo critério.** Modelos atuais entram em loop de auto-verificação em campanhas longas em vez de produzir resultado; o teto existe para cortar isso.

## O que conta como evidência

**Um critério é VERIFIED apenas quando um comando saiu com exit 0 e a saída está no relatório.**

Ler o diff não é evidência. Concluir que o código deveria funcionar não é evidência. Mock passando não é evidência. Sem comando executado, o critério não é VERIFIED — sem exceção, inclusive quando a mudança é obviamente correta.

Isto não é um pedido para "verificar com cuidado". É a definição do que a palavra VERIFIED significa neste relatório.

### Antes de começar

Descubra os comandos reais do repo: scripts do `package.json`, `Makefile`, config de CI, o `CLAUDE.md` mais próximo. Registre o que encontrou em `.claude/work/grounding.md`. Não presuma que `npm test` existe.

### Mapeando critérios para comandos

Cada critério mapeia para pelo menos um comando **antes** do trabalho começar. Escreva o mapeamento em `.claude/work/dod.md`.

Critério que nenhum comando prova é **não verificável**. Diga isso no início, não na hora do relatório, e limite o melhor resultado possível dele a IMPLEMENTED-NOT-VERIFIED.

```
✅ 1. `npm test -- src/__tests__/routes/users.test.ts` passa, incluindo o caso 401 sem token
❌ 1. O endpoint de usuários funciona com autenticação
```

### Correção de bug

Vermelho antes de verde, e **o vermelho vai commitado**:

1. Escreva o teste de regressão
2. Rode e confirme que falha
3. **Commite o teste falhando**
4. Implemente até passar, sem tocar no teste

O passo 3 é o que dá dentes ao resto: com o teste vermelho no histórico, qualquer alteração posterior nele aparece no diff. Fix sem vermelho commitado é IMPLEMENTED-NOT-VERIFIED.

### Formato da evidência

Comando, exit code e a linha relevante da saída. Não prosa.

## Grounding

Antes de escrever código, leia a fonte de verdade real — schema de verdade, endpoint de verdade, config existente — não a sua suposição dela.

**Escreva o que encontrar em `.claude/work/grounding.md`.** O arquivo é o ponto: o contexto compacta em runs longos, e um fato de grounding que existe só no histórico da conversa vai ser re-derivado de memória na hora 3, errado. Releia o arquivo após qualquer compactação.

## Postura

Siga adiante nas decisões de rotina em vez de parar para pedir permissão. Silêncio longo significa trabalho em andamento.

Três limites rígidos:

- **Não finja confiança.** Nunca reporte um passo como feito sem evidência conforme a definição acima.
- **Não faça gold-plating.** Exatamente o DoD. Sem escopo inventado, sem melhorias não pedidas. Pare antes de ações claramente além do que foi pedido.
- **Ação irreversível exige autorização prévia.** Destrutivo ou difícil de desfazer — apagar dados, force-push, deletar recursos, comunicação externa, gastar dinheiro — para e pergunta, mesmo em modo autônomo.

### Condições de parada

Pare e reporte, em vez de continuar queimando o run, quando:

- O mesmo critério falhou verificação 3 vezes. Reporte BLOCKED com as três tentativas.
- O grounding se revelou errado de um jeito que invalida trabalho já feito.
- Um critério exige ação irreversível não pré-autorizada.
- Você está gastando turnos verificando em vez de produzir.

## Guardas de coerência

1. **Contrato de decisão compartilhado.** Toda decisão transversal (formato de schema, nomes, interface) vai para um arquivo que todo trabalho paralelo lê e escreve. Evita um agente construir contra o schema A enquanto outro salva o B.
2. **Arquivo de interface entre sessões.** Quando este run depende de trabalho que outra sessão está construindo, escreva o contrato acordado em vez de adivinhar.
3. **Status honesto por critério.** Cada critério tem um estado explícito com evidência.

## Anti-racionalização

| Se você pensar... | Faça isto |
|---|---|
| "Esse teste está desatualizado ou errado" | Não edite. Reporte BLOCKED com o motivo. |
| "O código está obviamente certo, não preciso rodar" | Rode. Obviedade não é exit code. |
| "Vou ajustar a asserção pro comportamento novo" | Só se o DoD pediu mudança de comportamento. Senão, BLOCKED. |
| "Falha por causa do ambiente, não do meu código" | Prove: rode no commit anterior. Passava? É seu. |
| "Vou pular esse critério e reportar os outros" | Reporte como BLOCKED. Omissão não é permitida. |
| "Posso pegar o fix pronto do histórico do git" | Só se a tarefa permitir. Senão derive, e registre a fonte. |

## Relatório final

```
# Fable-it — [objetivo]
Janela: [início] → [fim]  |  Abordagem: [sessão única / paralela]
Grounding: .claude/work/grounding.md  |  DoD: .claude/work/dod.md

## Status por critério
| # | Critério    | Status                   | Comando         | Exit | Evidência    |
|---|-------------|--------------------------|-----------------|------|--------------|
| 1 | [critério]  | VERIFIED                 | `npm test -- x` | 0    | 12 passed    |
| 2 | [critério]  | IMPLEMENTED-NOT-VERIFIED | `npm run e2e`   | —    | staging fora |
| 3 | [critério]  | BLOCKED                  | —               | —    | falta chave  |

## Não verificáveis por comando
- [critérios sem comando, sinalizados no início]

## Próximas ações
- [um passo específico por item não-VERIFIED]
```

Vocabulário: **VERIFIED** (comando saiu 0, saída no relatório) · **IMPLEMENTED-NOT-VERIFIED** (feito, verificação não rodou ou não pôde rodar — diga por quê) · **BLOCKED** (não deu para prosseguir — nomeie o bloqueio).

Um relatório bagunçado e honesto vale mais que um limpo escondendo um passo não verificado. O usuário está dormindo e confiando nele.

## Invocação

```
Construa [coisa].
DoD:
1. [critério, de preferência já um comando]
2. [critério]
```

Rode `/goal` antes — ele converte critérios em prosa para critérios executáveis.

## Nota de manutenção

Esta skill deliberadamente **não** contém instruções como "verifique seu trabalho" ou "confira antes de responder". Modelos atuais já fazem isso por conta própria, e instruções redundantes de verificação produzem over-verification e loops improdutivos. O que esta skill fornece é a *definição de evidência* e o *instrumento*, não a exortação. Não readicione exortações de verificação.
