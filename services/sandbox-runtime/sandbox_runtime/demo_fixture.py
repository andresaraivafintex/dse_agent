"""Fixture `@demo` determinístico (WSC-E3-T4b-c, adendo 02 / ADR-27).

Template do teste de demonstração Playwright que o Tester autora na convenção
`demos/<work_item_id>/` (ver `toolsets.demo_dir_for`). É um fixture no mesmo
espírito do `FakeSubstrate`: o AUTOR do teste é roteirizado (nenhum LLM), mas
tudo que ele produz é real — página HTML estática servida localmente (via
`webServer` do Playwright: `python3 -m http.server`), spec `@demo` real,
execução `npx playwright test --grep @demo` real dentro do container da
imagem `dse-sandbox-base:wsc3`, vídeo real gravado pelo chromium headless.

Quem executa é o pipeline de evidência do WS-E (`RunDemoEvidenceInput` do
contrato da fundação — `demo_dir` default deriva de `demos/<work_item_id>/`):

    cd <workspace>/demos/<work_item_id> && npx playwright test --grep @demo

Notas de formato/ambiente (combinadas com o WS-E):
  - O vídeo gravado pelo Playwright é **.webm** (formato nativo do gravador
    do chromium). O plano fala "mp4"; transcodificação webm→mp4 (se exigida
    pela superfície de exibição) é pós-processamento do pipeline do WS-E —
    o Playwright não produz mp4 nativamente. Documentado, não escondido.
  - `chromiumSandbox: false`: o sandbox de user-namespaces do chromium não
    funciona sob `--cap-drop ALL`; a contenção é a do container do
    docker_driver (rootless, read-only, sem rede), não a do browser.
  - `DSE_DEMO_BASE_URL` (opcional): quando o WS-B/WS-E têm um preview real
    (`TriggerPreview` → `PreviewRef.url`), exportam esta env e o spec navega
    para lá em vez da página estática local — mesmo spec, mesma tag.
"""
from __future__ import annotations

from .toolsets import demo_dir_for

# ---------------------------------------------------------------------------
# Conteúdo do template — arquivos que o Tester escreve em demos/<work_item_id>/
# ---------------------------------------------------------------------------

_INDEX_HTML = """<!doctype html>
<html lang="pt-br">
  <head>
    <meta charset="utf-8" />
    <title>DSE demo fixture</title>
    <style>
      body { font-family: sans-serif; padding: 2rem; transition: background 0.2s; }
      h1 { font-size: 2.5rem; }
      button { font-size: 1.5rem; padding: 0.5rem 1.5rem; }
      output { font-size: 3rem; display: block; margin-top: 1rem; }
    </style>
  </head>
  <body>
    <h1 id="title">DSE demo fixture</h1>
    <p>Página estática determinística usada pelo teste <code>@demo</code>.</p>
    <button id="increment">incrementar</button>
    <output id="count">0</output>
    <script>
      // Cada clique muda contador E fundo — frames visivelmente distintos no
      // vídeo de evidência (um vídeo de página imóvel comprime a ~nada e não
      // demonstra nada a um humano).
      const hues = ['#ffffff', '#ffe9c7', '#c7f0ff', '#d8ffc7', '#ffd0e0'];
      document.getElementById('increment').addEventListener('click', () => {
        const el = document.getElementById('count');
        const n = 1 + Number(el.textContent);
        el.textContent = String(n);
        document.body.style.background = hues[n % hues.length];
      });
    </script>
  </body>
</html>
"""

_PLAYWRIGHT_CONFIG_JS = """// Config do fixture @demo (WSC-E3-T4b). Executado com:
//   npx playwright test --grep @demo
// dentro do container da imagem dse-sandbox-base:wsc3 (toolchain pinada lá).
const { defineConfig } = require('@playwright/test');

const baseURL = process.env.DSE_DEMO_BASE_URL || 'http://127.0.0.1:8931';

module.exports = defineConfig({
  testDir: '.',
  outputDir: './test-results',
  timeout: 60000,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL,
    video: 'on', // evidência: vídeo .webm real de cada teste
    trace: 'on', // trace zip — vira playwright_trace no artifact store
    headless: true,
    // chromiumSandbox: ver docstring do módulo; --disable-dev-shm-usage
    // porque o /dev/shm default do container (64MB) é pequeno demais para o
    // chromium — usa /tmp (tmpfs do sandbox) em vez disso.
    launchOptions: { chromiumSandbox: false, args: ['--disable-dev-shm-usage'] },
  },
  // Página estática SERVIDA LOCALMENTE (não file://): python3 -m http.server
  // existe na imagem base (python:3.11-slim). Quando DSE_DEMO_BASE_URL
  // aponta para um preview real, o webServer local não é usado pelo spec,
  // mas continua inofensivo (sobe e ninguém navega para ele).
  webServer: {
    command: 'python3 -m http.server 8931 --bind 127.0.0.1 --directory .',
    url: 'http://127.0.0.1:8931/index.html',
    reuseExistingServer: true,
    timeout: 30000,
  },
});
"""

_DEMO_SPEC_JS = """// Spec @demo do fixture determinístico (WSC-E3-T4b). A tag @demo no título
// é o contrato de descoberta: o pipeline de evidência roda --grep @demo.
const { test, expect } = require('@playwright/test');

test('fixture da tarefa renderiza e responde a interação @demo', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#title')).toHaveText('DSE demo fixture');
  // Pausas curtas entre interações: o vídeo de evidência precisa ser
  // assistível por um humano (frames distintos, ritmo visível), não um
  // borrão de milissegundos.
  for (const expected of ['1', '2', '3']) {
    await page.waitForTimeout(400);
    await page.click('#increment');
    await expect(page.locator('#count')).toHaveText(expected);
  }
  await page.waitForTimeout(400);
});
"""


def demo_fixture_files(work_item_id: str) -> dict[str, str]:
    """Mapa path relativo (na convenção `demos/<work_item_id>/`) → conteúdo.
    É isto que o Tester roteirizado escreve via `write_file` (passando pelo
    `TesterToolset.check` — o teste de conformidade prova que o toolset
    permite exatamente estes paths e nada fora deles)."""
    d = demo_dir_for(work_item_id)
    return {
        f"{d}index.html": _INDEX_HTML,
        f"{d}playwright.config.js": _PLAYWRIGHT_CONFIG_JS,
        f"{d}demo.spec.js": _DEMO_SPEC_JS,
    }


def demo_authoring_script(work_item_id: str) -> list[dict[str, str]]:
    """Script de autoria (formato do `authoring_script` de
    `_run_tester_turn_impl`) que materializa o fixture — o autor roteirizado
    do teste `@demo`, análogo ao FakeSubstrate do Coder."""
    return [
        {"tool": "write_file", "path": path, "content": content}
        for path, content in demo_fixture_files(work_item_id).items()
    ]
