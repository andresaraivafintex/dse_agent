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


def _constantes(arvore: ast.AST) -> dict[str, list[ast.expr]]:
    """Constantes do módulo que são tuple/list, por nome.

    A guarda quase nunca é escrita inline — ela mora numa constante e chega
    splatada (`_GIT` em `github/pr_finalizer.py`, `NO_CUSTOMER_HOOKS` em
    `scoped_git.py`). Sem resolver isso o scanner erra dos DOIS lados: acusa
    quem está protegido, e fica cego para `[*_GIT, "commit"]` de um `_GIT` que
    não carrega a guarda — que é justamente o jeito mais fácil de a proteção se
    perder de novo.
    """
    encontradas: dict[str, list[ast.expr]] = {}
    for no in ast.walk(arvore):
        if not isinstance(no, ast.Assign) or not isinstance(no.value, (ast.Tuple, ast.List)):
            continue
        for alvo in no.targets:
            if isinstance(alvo, ast.Name):
                encontradas[alvo.id] = list(no.value.elts)
    return encontradas


def _sequencia_tem_guarda(elementos: list[ast.expr]) -> bool:
    """`-c` seguido de `core.hooksPath=...`, em qualquer posição da sequência."""
    for i, elemento in enumerate(elementos[:-1]):
        if _literal(elemento) == "-c":
            valor = _literal(elementos[i + 1])
            if valor is not None and valor.startswith(PREFIXO_GUARDA):
                return True
    return False


_GLOBAIS: dict[str, list[list[ast.expr]]] | None = None


def _constantes_do_repo() -> dict[str, list[list[ast.expr]]]:
    """Toda constante tuple/list do repo, por nome, com TODAS as definições.

    A guarda é importada de um módulo para outro (`agent_runner.gitops` importa
    `NO_CUSTOMER_HOOKS` de `scoped_git`, e no container a mesma constante vem do
    `_scoped_git` vendorizado). Sem isto o scanner acusaria um wrapper que está
    protegido — e um gate que acusa quem está certo é um gate que as pessoas
    aprendem a contornar.
    """
    global _GLOBAIS
    if _GLOBAIS is None:
        encontradas: dict[str, list[list[ast.expr]]] = {}
        for caminho in _fontes():
            try:
                arvore = ast.parse(caminho.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for nome, elementos in _constantes(arvore).items():
                encontradas.setdefault(nome, []).append(elementos)
        _GLOBAIS = encontradas
    return _GLOBAIS


def _resolve_por_importacao(nome: str) -> bool:
    """Só vale como guarda se TODAS as definições daquele nome no repo a
    carregam. Uma definição sem a guarda e o nome volta a ser suspeito — é o
    caso em que a proteção se perderia sem ninguém notar."""
    definicoes = _constantes_do_repo().get(nome)
    return bool(definicoes) and all(_sequencia_tem_guarda(d) for d in definicoes)


def _expandir(elementos: list[ast.expr], constantes: dict[str, list[ast.expr]]) -> list[ast.expr]:
    """Inline dos `*CONSTANTE` conhecidos; o resto do splat fica como está."""
    saida: list[ast.expr] = []
    for elemento in elementos:
        if isinstance(elemento, ast.Starred) and isinstance(elemento.value, ast.Name):
            nome = elemento.value.id
            if nome in constantes:
                saida.extend(constantes[nome])
                continue
            if _resolve_por_importacao(nome):
                saida.extend(_constantes_do_repo()[nome][0])
                continue
        saida.append(elemento)
    return saida


def _classificar(elementos: list[ast.expr]) -> tuple[str | None, bool, bool]:
    """(subcomando, tem_guarda, tem_argumento_dinamico) — a partir do índice 1."""
    tem_guarda = False
    dinamico = False
    subcomando = None

    i = 1
    while i < len(elementos):
        elemento = elementos[i]
        texto = None if isinstance(elemento, ast.Starred) else _literal(elemento)
        if texto is None:
            # splat não resolvível, f-string, variável, chamada
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
        if subcomando is None:
            subcomando = texto
        i += 1

    return subcomando, tem_guarda, dinamico


def _analisar(arvore: ast.AST) -> list[tuple[int, str]]:
    """(linha, rótulo) de toda invocação git sem guarda comprovada."""
    constantes = _constantes(arvore)
    achados = []
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.List, ast.Tuple)) or not no.elts:
            continue
        elementos = _expandir(list(no.elts), constantes)
        primeiro = _literal(elementos[0]) if elementos else None

        if primeiro == "git":
            subcomando, tem_guarda, dinamico = _classificar(elementos)
            if tem_guarda:
                continue
            if dinamico and subcomando is None:
                # Wrapper que recebe subcomando de fora: pode receber `commit`
                # amanhã, então a guarda tem que estar nele, não em quem chama.
                achados.append((no.lineno, "wrapper"))
            elif subcomando in SUBCOMANDOS_QUE_RODAM_HOOK:
                achados.append((no.lineno, subcomando))
            continue

        # Lista que COMEÇA com um splat que não sei resolver (constante
        # importada, parâmetro): só me interessa se carrega, literal, um
        # subcomando que roda hook. Sem esse filtro qualquer lista do repo
        # entraria, e um gate barulhento deixa de ser lido.
        if elementos and isinstance(elementos[0], ast.Starred):
            literais = {_literal(e) for e in elementos[1:]}
            if literais & SUBCOMANDOS_QUE_RODAM_HOOK:
                achados.append((no.lineno, "wrapper"))
    return achados


def _rotulos(arvore: ast.AST) -> list[str]:
    return [rotulo for _, rotulo in _analisar(arvore)]


def _desprotegidas() -> list[str]:
    achados = []
    for caminho in _fontes():
        try:
            arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - o gate de compile pega antes
            continue
        rel = caminho.relative_to(ROOT)
        for linha, rotulo in _analisar(arvore):
            o_que = "wrapper genérico" if rotulo == "wrapper" else f"`git {rotulo}`"
            achados.append(f"{rel}:{linha} — {o_que} sem `-c {PREFIXO_GUARDA}...`")
    return achados


def test_todo_git_que_roda_hook_desliga_os_hooks_do_cliente():
    achados = _desprotegidas()
    assert not achados, (
        "comando git capaz de executar hook do cliente sem `-c core.hooksPath=`:\n  "
        + "\n  ".join(achados)
        + "\n\nA guarda tem que estar na LINHA DE COMANDO: `git config core.hooksPath` é "
        "desarmado pelo `npm ci` do cliente (husky reaponta para .husky/)."
    )


_GUARDA_EM_CONSTANTE = 'G = ("-c", "core.hooksPath=/nonexistent")\n'
_GIT_EM_CONSTANTE = '_GIT = ("git", "-c", "core.hooksPath=/nonexistent")\n'


@pytest.mark.parametrize(
    "fonte,esperado",
    [
        # --- formas literais ---
        ('["git", "commit", "-m", "x"]', ["commit"]),
        ('["git", "-C", d, "checkout", "main"]', ["checkout"]),
        ('["git", "-c", "core.hooksPath=/nonexistent", "push", "origin", "b"]', []),
        ('["git", "status", "--porcelain"]', []),
        ('["git", "rev-parse", "HEAD"]', []),
        ('["git", *args]', ["wrapper"]),
        ('["git", "-c", "core.hooksPath=/nonexistent", *args]', []),
        ('["git", "-c", "user.name=x", "commit"]', ["commit"]),
        # --- a guarda vinda de uma constante do módulo ---
        # É a forma idiomática (o `_GIT` de pr_finalizer.py já é assim) e o
        # scanner precisa enxergar através dela, nos dois sentidos: não acusar
        # quem está protegido, e não ficar CEGO para quem não está.
        (_GUARDA_EM_CONSTANTE + '["git", *G, *args]', []),
        (_GUARDA_EM_CONSTANTE + '["git", *G, "commit", "-m", "x"]', []),
        (_GIT_EM_CONSTANTE + '[*_GIT, "push", "origin", "b"]', []),
        ('X = ("-c", "user.name=x")\n["git", *X, "commit"]', ["commit"]),
        ('[*OUTRO, "commit"]', ["wrapper"]),
    ],
)
def test_o_classificador_reconhece_cada_forma(fonte, esperado):
    """O teste acima vale o que o classificador vale — estes são os casos que
    ele precisa distinguir, inclusive o `-c` que não é a guarda e a guarda que
    chega splatada de uma constante."""
    assert _rotulos(ast.parse(fonte)) == esperado
