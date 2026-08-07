---
description: Converte um objetivo em Definition of Done executável, descobrindo os comandos reais do repo
argument-hint: [o que você quer construído ou corrigido]
allowed-tools: Read, Grep, Glob, Bash(npm run:*), Bash(cat:*), Bash(ls:*), Write
---

Converta o objetivo abaixo em um Definition of Done onde cada critério é um comando que passa ou falha.

**Objetivo:** $ARGUMENTS

Não comece a implementar. Este comando produz apenas o DoD.

## 1. Descubra os comandos reais

Leia, nesta ordem, sem presumir:

- Scripts do `package.json` (ou `Makefile`, `pyproject.toml`, `Cargo.toml`, `go.mod` — o que este repo usar)
- O `CLAUDE.md` mais próximo, e o da raiz
- Config de CI (`.github/workflows/`) — a fonte de verdade sobre o que realmente barra um merge
- O layout do diretório de testes, para conseguir nomear arquivos específicos

## 2. Faça o grounding

Leia o código real que o objetivo toca — schema, assinaturas, config de verdade. O suficiente para escrever critérios que referenciam coisas que existem. Anote qualquer coisa que contradiga as premissas do objetivo.

## 3. Escreva o DoD

- Cada critério é um **comando com resultado esperado**. Nomeie arquivos de teste específicos quando possível.
- **Teto de 5.** Mais que isso, divida em runs e diga explicitamente.
- Correção de bug ganha critério vermelho-depois-verde: o teste falha antes do fix, passa depois, e o vermelho vai commitado.
- Critério que não vira comando entra em **Não verificável por comando**, com o motivo em uma linha e uma sugestão de checagem manual. Nunca disfarce de executável.

```
❌ O endpoint de usuários funciona com autenticação
✅ `npm test -- src/__tests__/routes/users.test.ts` passa, incluindo caso 401 sem token
```

## 4. Escreva em `.claude/work/dod.md`

O arquivo é o ponto — ele sobrevive à compactação de contexto, a conversa não.

```markdown
# DoD — [objetivo]

## Grounding
- [o que você leu, e o que contradisse as premissas do objetivo]

## Critérios
1. `comando` → resultado esperado
2. `comando` → resultado esperado

## Não verificável por comando
- [critério] — [motivo] — [checagem manual sugerida]

## Fora de escopo
- [o que isto explicitamente não cobre]
```

## 5. Reporte

Mostre o DoD no chat e pare. Destaque qualquer coisa do passo 2 que contradisse o objetivo — essa é a saída mais valiosa deste comando, porque pegar uma premissa errada agora custa 30 segundos em vez de uma noite.

Se o objetivo for vago demais para fazer grounding, diga o que falta e pergunte. Não invente critérios.
