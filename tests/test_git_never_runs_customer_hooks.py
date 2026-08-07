"""Nenhum comando git do DSE executa código do repositório do cliente.

A regra já foi corrigida três vezes, em três call sites, cada uma do seu jeito:
o commit de checkpoint (#46), os checkouts de higiene, e o push do finalizer
(#52). Cada correção resolveu o site onde o incidente apareceu e deixou os
outros de pé, porque a regra vivia dentro de cada chamada em vez de valer sobre
todas elas. Este teste é a regra em si.

Por que `-c` na linha de comando e não `git config`:

    `ScopedGitSession` escreve `core.hooksPath` na config do workspace, e isso
    funciona até o `npm ci` do gate L1 rodar o `prepare` do cliente — que é
    `husky`, que aponta `core.hooksPath` de volta para `.husky/`. A partir daí a
    proteção escrita em config está desarmada e o próximo `git commit` roda o
    `ng lint` do cliente dentro do sandbox. Foi assim que o turno morreu em OOM
    a cada ~45s em `wi_pr21`, e assim que `finalize_pr` morreu três vezes em
    `git push failed (exit=-1): - Finding files`.

    `-c` na linha de comando vence a config do repositório, então é a única
    forma que código não confiável não consegue desligar depois.

Um `core.hooksPath` inexistente é no-op para o git — não precisa existir nada
no disco.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Subcomandos que executam hook do repositório. `status`, `log`, `show`,
#: `rev-parse`, `cat-file`, `ls-remote`, `merge-base`, `config`, `remote`,
#: `init` e `diff` não executam nada e ficam de fora de propósito: um gate que
#: grita em chamada inofensiva é um gate que as pessoas aprendem a ignorar.
SUBCOMANDOS_QUE_RODAM_HOOK = frozenset(
    {"commit", "push", "checkout", "switch", "merge", "rebase", "am", "cherry-pick", "revert", "clone"}
)

PREFIXO_GUARDA = "core.hooksPath="

#: Flags de `git` que carregam um valor antes do subcomando.
FLAGS_COM_VALOR = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})


def _fontes() -> list[pathlib.Path]:
    arquivos = []
    for base in ("services", "packages"):
        for caminho in (ROOT / base).rglob("*.py"):
            partes = set(caminho.parts)
            if partes & {"tests", ".venv", "preview_repo", "node_modules", "__pycache__"}:
                continue
            arquivos.append(caminho)
    return sorted(arquivos)


def _literal(no: ast.AST) -> str | None:
    """O valor da string se o nó for uma string literal, senão None."""
    return no.value if isinstance(no, ast.Constant) and isinstance(no.value, str) else None


def _invocacoes_git(arvore: ast.AST):
    """Toda list/tuple literal cujo primeiro elemento é a string "git".

    Pega `["git", ...]`, `("git", ...)` e `[*_GIT, "ls-remote", ...]` não —
    esse último é o próprio `_GIT` já guardado, e aparece na varredura pela sua
    definição.
    """
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.List, ast.Tuple)) or not no.elts:
            continue
        if _literal(no.elts[0]) != "git":
            continue
        yield no


def _classificar(no: ast.List | ast.Tuple) -> tuple[str | None, bool, bool]:
    """(subcomando, tem_guarda, tem_argumento_dinamico)"""
    tem_guarda = False
    dinamico = False
    subcomando = None

    i = 1
    elementos = no.elts
    while i < len(elementos):
        elemento = elementos[i]
        if isinstance(elemento, ast.Starred):
            dinamico = True
            i += 1
            continue
        texto = _literal(elemento)
        if texto is None:
            # f-string, variável, chamada — não dá para saber o subcomando
            dinamico = True
            i += 1
            continue
        if texto in FLAGS_COM_VALOR:
            if texto == "-c" and i + 1 < len(elementos):
                valor = _literal(elementos[i + 1])
                if valor is not None and valor.startswith(PREFIXO_GUARDA):
                    tem_guarda = True
            i += 2
            continue
        if texto.startswith("-"):
            i += 1
            continue
        subcomando = texto
        break

    return subcomando, tem_guarda, dinamico


def _desprotegidas() -> list[str]:
    achados = []
    for caminho in _fontes():
        try:
            arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - o gate de compile pega antes
            continue
        for no in _invocacoes_git(arvore):
            subcomando, tem_guarda, dinamico = _classificar(no)
            if tem_guarda:
                continue
            rel = caminho.relative_to(ROOT)
            if dinamico and subcomando is None:
                # Um wrapper que recebe subcomando de fora pode receber
                # `commit` amanhã. A guarda tem que estar no wrapper.
                achados.append(f"{rel}:{no.lineno} — wrapper genérico sem `-c {PREFIXO_GUARDA}...`")
            elif subcomando in SUBCOMANDOS_QUE_RODAM_HOOK:
                achados.append(f"{rel}:{no.lineno} — `git {subcomando}` sem `-c {PREFIXO_GUARDA}...`")
    return achados


def test_todo_git_que_roda_hook_desliga_os_hooks_do_cliente():
    achados = _desprotegidas()
    assert not achados, (
        "comando git capaz de executar hook do cliente sem `-c core.hooksPath=`:\n  "
        + "\n  ".join(achados)
        + "\n\nA guarda tem que estar na LINHA DE COMANDO: `git config core.hooksPath` é "
        "desarmado pelo `npm ci` do cliente (husky reaponta para .husky/)."
    )


@pytest.mark.parametrize(
    "linha,esperado",
    [
        ('["git", "commit", "-m", "x"]', ["commit"]),
        ('["git", "-C", d, "checkout", "main"]', ["checkout"]),
        ('["git", "-c", "core.hooksPath=/nonexistent", "push", "origin", "b"]', []),
        ('["git", "status", "--porcelain"]', []),
        ('["git", "rev-parse", "HEAD"]', []),
        ('["git", *args]', ["wrapper"]),
        ('["git", "-c", "core.hooksPath=/nonexistent", *args]', []),
        ('["git", "-c", "user.name=x", "commit"]', ["commit"]),
    ],
)
def test_o_classificador_reconhece_cada_forma(linha, esperado):
    """O teste acima vale o que o classificador vale — estes são os casos que
    ele precisa distinguir, inclusive o `-c` que não é a guarda."""
    no = ast.parse(linha).body[0].value
    subcomando, tem_guarda, dinamico = _classificar(no)
    if tem_guarda:
        obtido = []
    elif dinamico and subcomando is None:
        obtido = ["wrapper"]
    elif subcomando in SUBCOMANDOS_QUE_RODAM_HOOK:
        obtido = [subcomando]
    else:
        obtido = []
    assert obtido == esperado
