"""Deterministic `@demo` fixture (WSC-E3-T4b-c, adendo 02 / ADR-27).

Template of the Playwright demonstration test the Tester authors under the
`demos/<work_item_id>/` convention (see `toolsets.demo_dir_for`). It is a
fixture in the same spirit as `FakeSubstrate`: the test AUTHOR is scripted (no
LLM), but everything it produces is real — a static HTML page served locally
(via Playwright's `webServer`: `python3 -m http.server`), a real `@demo` spec,
a real `npx playwright test --grep @demo` run inside a container of the
`dse-sandbox-base:wsc3` image, and a real video recorded by headless chromium.

The executor is WS-E's evidence pipeline (`RunDemoEvidenceInput` from the
foundation contract — the `demo_dir` default derives from
`demos/<work_item_id>/`):

    cd <workspace>/demos/<work_item_id> && npx playwright test --grep @demo

Format/environment notes (agreed with WS-E):
  - The video Playwright records is **.webm** (chromium's native recorder
    format). The plan says "mp4"; webm→mp4 transcoding (if the display surface
    requires it) is post-processing in WS-E's pipeline — Playwright does not
    produce mp4 natively. Documented, not hidden.
  - `chromiumSandbox: false`: chromium's user-namespace sandbox does not work
    under `--cap-drop ALL`; the containment is the docker_driver container's
    (rootless, read-only, no network), not the browser's.
  - `DSE_DEMO_BASE_URL` (optional): when WS-B/WS-E have a real preview
    (`TriggerPreview` → `PreviewRef.url`), they export this env and the spec
    navigates there instead of the local static page — same spec, same tag.
"""
from __future__ import annotations

from .toolsets import demo_dir_for

# ---------------------------------------------------------------------------
# Template content — files the Tester writes into demos/<work_item_id>/
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
    <p>Deterministic static page used by the <code>@demo</code> test.</p>
    <button id="increment">increment</button>
    <output id="count">0</output>
    <script>
      // Every click changes the counter AND the background — visibly distinct
      // frames in the evidence video (a video of a static page compresses to
      // ~nothing and demonstrates nothing to a human).
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

_PLAYWRIGHT_CONFIG_JS = """// Config of the @demo fixture (WSC-E3-T4b). Run with:
//   npx playwright test --grep @demo
// inside the dse-sandbox-base:wsc3 image container (toolchain pinned there).
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
    video: 'on', // evidence: a real .webm video of every test
    trace: 'on', // trace zip — becomes playwright_trace in the artifact store
    headless: true,
    // chromiumSandbox: see the module docstring; --disable-dev-shm-usage
    // because the container's default /dev/shm (64MB) is far too small for
    // chromium — it uses /tmp (the sandbox tmpfs) instead.
    launchOptions: { chromiumSandbox: false, args: ['--disable-dev-shm-usage'] },
  },
  // Static page SERVED LOCALLY (not file://): python3 -m http.server exists
  // in the base image (python:3.11-slim). When DSE_DEMO_BASE_URL points at a
  // real preview, the local webServer is not used by the spec, but it stays
  // harmless (it starts and nobody navigates to it).
  webServer: {
    command: 'python3 -m http.server 8931 --bind 127.0.0.1 --directory .',
    url: 'http://127.0.0.1:8931/index.html',
    reuseExistingServer: true,
    timeout: 30000,
  },
});
"""

_DEMO_SPEC_JS = """// @demo spec of the deterministic fixture (WSC-E3-T4b). The @demo tag in the
// title is the discovery contract: the evidence pipeline runs --grep @demo.
const { test, expect } = require('@playwright/test');

test('task fixture renders and responds to interaction @demo', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#title')).toHaveText('DSE demo fixture');
  // Short pauses between interactions: the evidence video has to be
  // watchable by a human (distinct frames, visible pace), not a
  // millisecond-long blur.
  for (const expected of ['1', '2', '3']) {
    await page.waitForTimeout(400);
    await page.click('#increment');
    await expect(page.locator('#count')).toHaveText(expected);
  }
  await page.waitForTimeout(400);
});
"""


def demo_fixture_files(work_item_id: str) -> dict[str, str]:
    """Map of relative path (under the `demos/<work_item_id>/` convention) →
    content. This is what the scripted Tester writes via `write_file` (going
    through `TesterToolset.check` — the conformance test proves the toolset
    allows exactly these paths and nothing outside them)."""
    d = demo_dir_for(work_item_id)
    return {
        f"{d}index.html": _INDEX_HTML,
        f"{d}playwright.config.js": _PLAYWRIGHT_CONFIG_JS,
        f"{d}demo.spec.js": _DEMO_SPEC_JS,
    }


def demo_authoring_script(work_item_id: str) -> list[dict[str, str]]:
    """Authoring script (in the `authoring_script` format of
    `_run_tester_turn_impl`) that materializes the fixture — the scripted author
    of the `@demo` test, analogous to the Coder's FakeSubstrate."""
    return [
        {"tool": "write_file", "path": path, "content": content}
        for path, content in demo_fixture_files(work_item_id).items()
    ]
