"""Alcançabilidade das fronteiras de posse (2026-08-07).

Para cada célula (quem quebrou, em que arquivo a falha se manifesta, modo de
falha), tem que existir PELO MENOS UM ator autorizado a corrigir — ou uma
escalada DESENHADA para humano. Morrer no teto de retentativas não é saída:
é exaustão, e foi exatamente assim que wi_5620d2c1 e wi_8edaef39 morreram
antes das portas 1/5.

As regras modeladas aqui espelham código real (referências abaixo). Isto NÃO
importa os serviços — é um mapa executável; quem mudar uma regra atualiza a
célula, e o teste força isso: célula viva sem saída falha; beco documentado
que ganhar saída vira XPASS estrito e falha também, exigindo promover a
célula.

As regras e onde vivem:
  R1 revert de teste do Coder      — sandbox_runtime/activities.py (coder_test_edits_reverted):
                                     o Coder NUNCA edita caminho de teste.
  R2 posse do Tester               — activities.py:_is_dse_authored (~2347) + rename guard
                                     (~2329): o Tester nunca destrói spec do cliente.
  R3 reuso existencial + exceção   — activities.py (~2889: reused) + porta 5
     zero-veredito                   (_zero_verdict_specs + repair in-place, rc.42):
                                     re-autoria SÓ quando a suite própria não executa
                                     asserção alguma (carga/compilação).
  R4 deferral                      — activities.py:_suite_verdict_deferred (~2034):
                                     suite própria falhando não é gate; L1 julga.
  R5 detector da porta 1           — dse_orchestrator/workflows.py:preexisting_spec_conflicts:
                                     spec FAIL não-do-Tester com SUJEITO no diff
                                     ACUMULADO (v2) → parque spec_conflict p/ humano.
  R6 forbidden_paths               — validation (plan_compliance): gate sobre o diff do
                                     Coder; o próprio Coder pode remover o que criou.
  R7 diff_budget                   — validation: idem — o Coder pode encolher o diff.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

PRODUCAO = "producao"
SPEC_TESTER = "spec_tester"
SPEC_CLIENTE = "spec_cliente"

ASSERCAO = "assercao"              # a suite EXECUTOU e reprovou — existe veredito
ZERO_VEREDITO = "zero_veredito"    # carga/compilação da spec morreu — sem veredito
COMPILE_PRODUCAO = "compile_producao"
FORBIDDEN_PATHS = "forbidden_paths"
DIFF_BUDGET = "diff_budget"


@dataclass(frozen=True)
class Celula:
    quem: str        # coder | tester | cliente (estado pré-existente do repo)
    arquivo: str     # onde a falha se manifesta
    modo: str
    sujeito_no_diff: bool = True  # só relevante para SPEC_CLIENTE (R5/v2: diff ACUMULADO)
    nota: str = ""


def saidas(c: Celula) -> set[str]:
    """Os atores/parques autorizados pelas regras R1-R7 — deny-by-default."""
    s: set[str] = set()

    # Coder: autorizado em PRODUÇÃO e nos gates sobre o próprio diff (R6/R7).
    # R1 o exclui de QUALQUER caminho de teste, sempre.
    if c.arquivo == PRODUCAO:
        s.add("coder")
    if c.modo in {FORBIDDEN_PATHS, DIFF_BUDGET}:
        s.add("coder")

    # Tester: R3 — re-autoria in-place APENAS na spec própria sem veredito
    # (posse via git do Pod; asserção falhando é veredito e fica de fora).
    if c.arquivo == SPEC_TESTER and c.modo == ZERO_VEREDITO:
        s.add("tester")

    # Humano: R5 — parque spec_conflict quando uma spec do CLIENTE está na
    # lista FAIL e o sujeito dela está no diff acumulado do item.
    if c.arquivo == SPEC_CLIENTE and c.sujeito_no_diff and c.modo in {ASSERCAO, ZERO_VEREDITO}:
        s.add("humano:spec_conflict")

    # R2: célula (tester quebrou spec do cliente) é estruturalmente impossível —
    # o rename guard desvia a escrita para um caminho -dse próprio. Modelada
    # fora da matriz (ver test_r2_torna_a_celula_impossivel).
    return s


VIVAS: list[Celula] = [
    # O laço saudável: quem quebra produção conserta produção.
    Celula("coder", PRODUCAO, COMPILE_PRODUCAO,
           nota="fix_context → Coder (medido: wi_1a5f9e3d corrigiu typecheck+build)"),
    Celula("coder", PRODUCAO, ASSERCAO,
           nota="spec do Tester reprovando código: o alvo do conserto é a produção"),
    # Porta 1 (rc.41 + v2 rc.42): spec do cliente invalidada pelo diff → humano.
    Celula("coder", SPEC_CLIENTE, ASSERCAO,
           nota="parque spec_conflict (medido ao vivo em wi_8edaef39, 2º parque via diff acumulado)"),
    Celula("coder", SPEC_CLIENTE, ZERO_VEREDITO,
           nota="carga da spec do cliente quebrada por mudança no sujeito → parque"),
    # Porta 5 (rc.42): instrumento próprio quebrado → o próprio Tester repara.
    Celula("tester", SPEC_TESTER, ZERO_VEREDITO,
           nota="@MockBean (wi_5620d2c1): testCompile sem veredito → repair in-place"),
    Celula("coder", SPEC_TESTER, ZERO_VEREDITO,
           nota="@ngx-translate herdado (wi_8edaef39): posse decide, não a autoria do defeito"),
    # Gates sobre o diff do Coder: o Coder é o ator (remove/encolhe).
    Celula("coder", PRODUCAO, FORBIDDEN_PATHS,
           nota="run 1: Dockerfile fora do plano — o Coder pode deletar o que criou"),
    Celula("coder", PRODUCAO, DIFF_BUDGET,
           nota="run 2: 451 linhas — o Coder pode encolher o próprio diff"),
]

#: BECOS CONHECIDOS — células hoje SEM ator e SEM escalada desenhada. A saída
#: real é exaustão (coder_not_converging/teto), que mata o item sem nomear a
#: causa. xfail ESTRITO: quem abrir uma saída para elas promove a célula.
BECOS: list[Celula] = [
    Celula("tester", SPEC_TESTER, ASSERCAO,
           nota="spec própria com asserção ERRADA e código certo: Coder revertido (R1), "
                "Tester não re-autora com veredito presente (R3), porta 1 exclui por posse (R5). "
                "Resíduo indecidível da porta 2 — hoje só exaustão."),
    Celula("cliente", SPEC_CLIENTE, ASSERCAO, sujeito_no_diff=False,
           nota="baseline vermelha do repo (spec já quebrada antes do item, sujeito fora do "
                "diff): nenhum ator, nenhum parque — L1 vermelho até o teto. Não há "
                "comparação com o estado base em lugar nenhum."),
    Celula("cliente", SPEC_CLIENTE, ZERO_VEREDITO, sujeito_no_diff=False,
           nota="mesma baseline, morrendo na carga — idem."),
]


@pytest.mark.parametrize("celula", VIVAS, ids=lambda c: f"{c.quem}/{c.arquivo}/{c.modo}")
def test_toda_celula_viva_tem_saida(celula: Celula):
    quem_pode = saidas(celula)
    assert quem_pode, (
        f"célula ({celula.quem}, {celula.arquivo}, {celula.modo}) ficou sem ator autorizado "
        f"e sem escalada desenhada — {celula.nota}"
    )


@pytest.mark.parametrize("celula", BECOS, ids=lambda c: f"BECO:{c.quem}/{c.arquivo}/{c.modo}")
@pytest.mark.xfail(strict=True, reason="beco documentado: sem ator e sem escalada desenhada; "
                                       "a saída real é exaustão no teto")
def test_becos_documentados_continuam_becos(celula: Celula):
    assert saidas(celula), celula.nota


def test_r2_torna_a_celula_impossivel():
    """(tester, spec_cliente, *) não é um beco — é INALCANÇÁVEL por construção:
    o rename guard (activities.py ~2329) desvia qualquer escrita do Tester sobre
    spec do cliente para um caminho -dse próprio. Se essa guarda cair, a célula
    nasce sem dono e este teste é o lembrete de modelá-la."""
    protegida = Celula("tester", SPEC_CLIENTE, ASSERCAO)
    assert "tester" not in saidas(protegida), "o Tester nunca é ator em spec do cliente"


def test_deferral_nao_e_saida_e_sim_encaminhamento():
    """R4: o deferral não autoriza ninguém — só move o veredito para o L1. Uma
    célula não pode ser considerada 'resolvida' porque o Tester deferiu; a
    prova é que os becos acima existem COM o deferral ligado."""
    beco = BECOS[0]
    assert not saidas(beco)
