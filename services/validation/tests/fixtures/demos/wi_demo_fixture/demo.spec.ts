// WSE-E5-T11 — teste @demo fixture (página estática local, 100% determinístico).
// Convenção do path: demos/<work_item_id>/ (ADR-27 / WSC-E3-T4b). O WS-C está
// entregando o fixture "oficial" em paralelo; este é o fixture local mínimo do
// WS-E, documentado no README.
import { test, expect } from '@playwright/test';
import * as path from 'path';

test('fluxo de incremento até concluído @demo', async ({ page }) => {
  const url = 'file://' + path.join(__dirname, 'page.html');
  await page.goto(url);
  await expect(page.locator('h1')).toContainText('fixture');
  for (let i = 0; i < 3; i++) {
    await page.click('#inc');
    await page.waitForTimeout(250); // dá corpo ao vídeo (frames distintos)
  }
  await expect(page.locator('#counter')).toHaveText('3');
  await expect(page.locator('#status')).toHaveText('concluído');
});
