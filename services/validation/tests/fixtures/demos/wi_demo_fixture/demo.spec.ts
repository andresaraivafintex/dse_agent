// WSE-E5-T11 — @demo fixture test (local static page, 100% deterministic).
// Path convention: demos/<work_item_id>/ (ADR-27 / WSC-E3-T4b). WS-C is
// delivering the "official" fixture in parallel; this is WS-E's minimal local
// fixture, documented in the README.
import { test, expect } from '@playwright/test';
import * as path from 'path';

test('increment flow through to done @demo', async ({ page }) => {
  const url = 'file://' + path.join(__dirname, 'page.html');
  await page.goto(url);
  await expect(page.locator('h1')).toContainText('fixture');
  for (let i = 0; i < 3; i++) {
    await page.click('#inc');
    await page.waitForTimeout(250); // gives the video some body (distinct frames)
  }
  await expect(page.locator('#counter')).toHaveText('3');
  await expect(page.locator('#status')).toHaveText('done');
});
