<!-- Template de CLAUDE.md por area. Teto: ~50 linhas.
     Regra: se da para descobrir lendo o codigo, NAO entra aqui.
     Passou de 50 linhas, virou documentacao — mova para uma skill. -->

# <nome da area>

## Comandos
- test:        `<comando>`
- teste unico: `<comando> -- <caminho>`
- typecheck:   `<comando>`
- lint:        `<comando>`
- dev:         `<comando>`

## Invariantes
<!-- Regras que o codigo nao revela sozinho. Uma linha cada. -->
- <ex: nunca SQL cru em route handler; queries via camada de dados>
- <ex: toda mutacao passa pela camada de servico>

## Pegadinhas
<!-- Comportamento nao obvio que ja causou bug. -->
- <ex: o cache invalida sozinho no write; nao chame invalidate() manualmente>

## Definicao de pronto
Typecheck e suite verdes.
Bugfix: o teste de regressao e escrito, roda vermelho, e commitado ANTES do fix.
