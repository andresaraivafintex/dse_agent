# Opus 5: o que mudou e o que ficou obsoleto

Claude Opus 5 foi lançado em 24/07/2026. Várias práticas consolidadas em modelos anteriores viraram ruído ou contraproducentes. Esta é a lista do que mudou para trabalho de engenharia.

---

## A mudança que mais afeta um setup em loop de regressão

**O Opus 5 verifica o próprio trabalho sem ser mandado.** A doc oficial de prompting é explícita:

> Se o seu prompt contém instruções explícitas de verificação ("inclua um passo final de verificação", "use um subagente para verificar"), **remova**: instruções assim causam *over-verification* no Claude Opus 5.

E também: evite mandar refazer checagens que ele já faz ("confira sua resposta", "reverifique antes de responder").

### Por que isso NÃO significa "pare de verificar"

É fácil ler isso como contradição de tudo que a skill prega. Não é — é a confirmação do modelo de três camadas.

- *"Verifique seu trabalho"* é **instrução** (camada 3). Redundante no Opus 5, e agora ativamente cara.
- *Um comando cujo exit code o modelo não controla* é **mecanismo** (camada 1). Continua sendo a única coisa que fecha o loop.

O Opus 5 tornou a camada 3 desnecessária para essa finalidade. Ele não tornou a camada 1 dispensável. O que muda na prática: **remova a exortação, mantenha o instrumento.**

### Risco concreto de exagerar

O system card do Opus 5 documenta que, numa campanha de 24 horas, o modelo "consistentemente entrou em loops de auto-verificação em vez de produzir designs". Ou seja: instrução redundante de verificação não é só desperdício, é um modo de falha real. Por isso a skill impõe teto de tempo/turnos.

---

## Subagentes

O Opus 5 delega com mais facilidade que modelos anteriores. Duas orientações oficiais:

- Cape a delegação explicitamente em tarefas onde ela não ajuda.
- **Não use subagentes para verificar ou conferir o próprio trabalho.** Isso é dito diretamente na doc.

Combina com a delimitação geral da Anthropic: multi-agente serve para exploração paralelizável, não para "tarefas fortemente interdependentes como codificação".

---

## Expansão de escopo

O Opus 5 tende a alargar o escopo da tarefa. A doc recomenda restringir explicitamente quando a tarefa é estreita: *"Entregue o que foi pedido, no escopo pretendido... pare antes de ações que estão claramente além do pedido."*

Isso dá respaldo empírico à cláusula "não faça gold-plating" de qualquer skill de entrega autônoma. Mantenha-a.

---

## Parâmetro de effort

Escada: `low`, `medium`, `high` (padrão), `xhigh`, `max`.

- Comece no padrão `high`.
- Baixe para `low`/`medium` onde a qualidade se mantém — economia real.
- Suba para `xhigh` em trabalho agêntico e de código exigente.
- Thinking vem ligado por padrão e só pode ser desligado em `high` ou abaixo.

Se o custo de tokens explodir depois de instalar hooks, suspeite de over-verification ou de delegação excessiva antes de suspeitar do effort.

---

## Janela de contexto

1M tokens, padrão e máximo, sem variante menor. A doc afirma que instruction following, tool calling e raciocínio permanecem consistentes ao longo de toda a janela.

**Trate como afirmação do fornecedor.** A pesquisa independente sobre context rot (Chroma, jul/2025, 18 modelos de fronteira) encontra degradação não uniforme conforme o input cresce, com problemas sérios já por volta de 50k tokens numa janela de 200k. Não há benchmark independente que confirme fidelidade uniforme em 1M.

Na prática: mantenha o contexto de trabalho enxuto mesmo tendo 1M disponível. A janela grande é margem de segurança, não convite.

---

## Alinhamento e confiabilidade

O system card descreve o Opus 5 como o modelo mais alinhado até então: tentativas de contornar classificadores em menos de 0,01% das completions monitoradas, nenhuma instância de sandbagging ou evasão de supervisão detectada.

Dois contrapontos que importam para quem depende do relatório dele:

- O próprio card registra que o Opus 5 alucina fatos ligeiramente mais que o Opus 4.8, e que "afirmou com confiança respostas sobre as quais estava de fato inseguro" num número surpreendente de casos.
- A Andon Labs (Vending-Bench) disputa o enquadramento de "most aligned" com base em avaliação qualitativa.

"Most aligned" é resultado de teste automatizado, não garantia comportamental. Não substitui o gate.

---

## Ferramenta útil

`/doctor` (ou `claude doctor`) enxuga skills e CLAUDE.md segundo as novas regras da geração Claude 5. Rode na fase 3.5 da skill, depois que os mecanismos estiverem no lugar.

---

## Resumo: o que retirar do setup

| Retire | Motivo |
|---|---|
| "Inclua um passo final de verificação" | Causa over-verification |
| "Confira / reverifique sua resposta" | Ele já faz |
| "Use um subagente para verificar" | Contraindicado explicitamente |
| Andaimes de prompt herdados de modelos antigos | 80% do system prompt do Claude Code foi removido sem perda |
| "Continue trabalhando sem parar" | Aumenta a taxa de atalho e trapaça (dado da Cursor) |

## O que manter

| Mantenha | Motivo |
|---|---|
| Hooks que gatam com exit code | Camada 1. Nada substitui. |
| DoD executável, critério = comando | Elimina o julgamento subjetivo de "pronto" |
| Teste vermelho commitado antes do fix | Torna alteração de teste visível no diff |
| Cláusula anti-gold-plating | Opus 5 expande escopo |
| Teto de tempo/turnos | Risco documentado de loop de auto-verificação |
| Grounding e plano em arquivo | Sobrevive à compactação |
