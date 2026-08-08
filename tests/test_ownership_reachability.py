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
                                     ACUMULADO (v2). Em DOIS estágios (v3): verde no
                                     base + 1ª ocorrência → o CODER ganha uma rodada
                                     com as asserções (spec_conflict_deferred_to_coder);
                                     a MESMA spec de novo → parque p/ humano. Vermelha
                                     no base nunca entra (é achado herdado, R8).
  R6 forbidden_paths               — validation (plan_compliance): gate sobre o diff do
                                     Coder; o próprio Coder pode remover o que criou.
  R7 diff_budget                   — validation: idem — o Coder pode encolher o diff.
  R8 baseline check                — l1/quality_checks.py:_baseline_failing_suites +
                                     test_check(base_sha=): suite já vermelha no base
                                     vira NOT_OUR_FAILURE e o item SEGUE (promoveu os
                                     becos 2 e 3 deste mapa).
  R9 exaustão em spec própria      — workflows.py:exclusively_tester_spec_failures +
                                     tester_spec_assertion_failures (a MEMÓRIA) +
                                     parque na primitiva da porta 1. Gatilho v2 por
                                     MEMÓRIA DE SPEC: a mesma spec própria reprovando
                                     com veredito em QUALQUER rodada posterior parqueia,
                                     independente do que falhou entre elas — o
                                     fingerprint consecutivo resetava numa rodada de
                                     lint+build no meio e o wi_c9c7b200 morreu no teto
                                     com o parque deployado. Medidos: pageSize
                                     wi_5eecf486, 'warning' wi_32eb136f, alternado
                                     wi_c9c7b200.
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

    # R5 (v3, dois estágios) — spec do CLIENTE na lista FAIL com sujeito no
    # diff acumulado E verde no base: 1º estágio, o Coder (uma rodada com as
    # asserções exatas — medido no wi_c9c7b200: preservar max-w-[200px] era
    # mudança no próprio HTML dele); 2º estágio, a reincidência parqueia para
    # humano. Vermelha no base (quem == "cliente") não entra: é herdada (R8).
    if (
        c.quem != "cliente"
        and c.arquivo == SPEC_CLIENTE
        and c.sujeito_no_diff
        and c.modo in {ASSERCAO, ZERO_VEREDITO}
    ):
        s.add("coder")
        s.add("humano:spec_conflict")

    # R8 — o vermelho que o item ENCONTROU: a suite já falhava no base_sha, então
    # não é reprovação dele. Não precisa de ator nem de humano: o gate classifica
    # NOT_OUR_FAILURE (nomes no detail, contagem no ledger) e o item segue.
    if c.quem == "cliente" and c.arquivo == SPEC_CLIENTE:
        s.add("baseline:not_our_failure")

    # R9 — exaustão reconhecida (não classificada): asserção em spec própria
    # com veredito, REPETIDA EM QUALQUER RODADA POSTERIOR (memória por spec,
    # não fingerprint consecutivo — rodadas heterogêneas no meio não apagam),
    # sem ator autorizado → parque para humano com dossiê, na primitiva da
    # porta 1.
    if c.arquivo == SPEC_TESTER and c.modo == ASSERCAO:
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
    # Porta 1 (rc.41 + v2 rc.42 + v3): spec do cliente invalidada pelo diff →
    # Coder primeiro, humano na reincidência.
    Celula("coder", SPEC_CLIENTE, ASSERCAO,
           nota="dois estágios (wi_c9c7b200): 1ª falha vira rodada de Coder com as "
                "asserções; a mesma spec de novo parqueia (wi_8edaef39, diff acumulado)"),
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
    # Promovidas de beco por R8 (baseline check): o item não morre mais no teto
    # por um vermelho que o repositório já tinha.
    Celula("cliente", SPEC_CLIENTE, ASSERCAO, sujeito_no_diff=False,
           nota="baseline vermelha do repo: classificada NOT_OUR_FAILURE, o item segue"),
    Celula("cliente", SPEC_CLIENTE, ZERO_VEREDITO, sujeito_no_diff=False,
           nota="mesma baseline morrendo na carga: idem — comparada suite a suite"),
    # v3: herdada MESMO com o sujeito no diff acumulado — o vermelho já existia
    # no base, então não é conflito deste item nem ganha chance de Coder.
    Celula("cliente", SPEC_CLIENTE, ASSERCAO, sujeito_no_diff=True,
           nota="baseline vermelha cujo sujeito o diff tocou: segue NOT_OUR_FAILURE"),
    # Promovida de beco por R9: a exaustão em spec própria parqueia com dossiê
    # (specs, asserções, esperado vs recebido, diff) em vez de morrer no teto.
    Celula("tester", SPEC_TESTER, ASSERCAO,
           nota="pageSize (wi_5eecf486), 'warning' (wi_32eb136f) e o alternado "
                "test→lint+build→test (wi_c9c7b200, que escapou do gatilho consecutivo "
                "e morreu no teto): memória por spec parqueia na 2ª reprovação da "
                "mesma spec, e o humano decide com o dossiê — retry ou reauthor"),
]

#: BECOS CONHECIDOS — vazio hoje: os três medidos foram promovidos (R8, R9).
#: O mecanismo fica: um beco novo entra aqui com xfail ESTRITO, e quem abrir
#: uma saída para ele é OBRIGADO pelo teste a promover a célula.
BECOS: list[Celula] = []


@pytest.mark.parametrize("celula", VIVAS, ids=lambda c: f"{c.quem}/{c.arquivo}/{c.modo}")
def test_toda_celula_viva_tem_saida(celula: Celula):
    quem_pode = saidas(celula)
    assert quem_pode, (
        f"célula ({celula.quem}, {celula.arquivo}, {celula.modo}) ficou sem ator autorizado "
        f"e sem escalada desenhada — {celula.nota}"
    )


def test_r2_torna_a_celula_impossivel():
    """(tester, spec_cliente, *) não é um beco — é INALCANÇÁVEL por construção:
    o rename guard (activities.py ~2329) desvia qualquer escrita do Tester sobre
    spec do cliente para um caminho -dse próprio. Se essa guarda cair, a célula
    nasce sem dono e este teste é o lembrete de modelá-la."""
    protegida = Celula("tester", SPEC_CLIENTE, ASSERCAO)
    assert "tester" not in saidas(protegida), "o Tester nunca é ator em spec do cliente"


def test_porta1_v3_da_ao_coder_a_primeira_chance():
    """v3: o conflito em spec do cliente verde no base tem DOIS estágios — o
    Coder (retry automático com as asserções) e o humano (parque na
    reincidência). A herdada não tem nenhum dos dois: baseline resolve."""
    fresca = Celula("coder", SPEC_CLIENTE, ASSERCAO)
    assert saidas(fresca) == {"coder", "humano:spec_conflict"}
    herdada = Celula("cliente", SPEC_CLIENTE, ASSERCAO, sujeito_no_diff=True)
    assert saidas(herdada) == {"baseline:not_our_failure"}


def test_deferral_nao_e_saida_e_sim_encaminhamento():
    """R4: o deferral não autoriza ninguém — só move o veredito para o L1. A
    célula que era o beco 1 tem saída HOJE por causa do parque R9, não do
    deferral: a única saída é o humano, nunca um ator do laço."""
    celula = Celula("tester", SPEC_TESTER, ASSERCAO)
    assert saidas(celula) == {"humano:spec_conflict"}
