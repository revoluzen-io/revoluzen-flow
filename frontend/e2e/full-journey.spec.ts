/**
 * Author-surface reachability journey.
 *
 * The old version drove the login form with the wrong password and used
 * path routes (`/connections`, `/monitor`) the hash-router never served,
 * and it required a live Postgres at localhost:5432 to go green. This
 * rewrite uses programmatic auth + the real hash routes and asserts the
 * author surfaces are reachable + interactive. It deliberately does NOT
 * submit a real connection or run a pipeline — that needs external infra
 * (a live database) and belongs in a separately-gated integration job.
 */
import { test, expect } from './fixtures';

test.describe('Author surfaces reachable', () => {
  // 2026-10-23 — these asserted `locator('button').first()` is visible,
  // meaning to say "at least one control renders". They are not the same
  // claim: `.first()` is the first button in DOM ORDER, and the first button
  // in this app's DOM is the mobile hamburger, which carries `md:hidden` and
  // so is `display: none` at every width these tests run at.
  //
  // They passed only because the first-run onboarding modal used to render
  // above everything with its own buttons. Once global-setup dismissed that
  // modal — which it must, so the rest of the suite can click things — the
  // latent bad assertion surfaced. `button:visible` says what was meant.
  test('Connections page renders an interactive surface', async ({ authedPage: page }) => {
    await page.goto('/#connections', { waitUntil: 'domcontentloaded' });
    await expect(page.getByLabel(/password/i)).toHaveCount(0);
    // Every authenticated page is interactive — at least one control renders.
    await expect(page.locator('button:visible').first()).toBeVisible({ timeout: 10_000 });
  });

  test('Pipeline editor canvas mounts', async ({ authedPage: page }) => {
    await page.goto('/#editor', { waitUntil: 'domcontentloaded' });
    await expect(page.getByLabel(/password/i)).toHaveCount(0);
    await expect(page.locator('#root')).not.toBeEmpty();
    await expect(page.locator('button:visible').first()).toBeVisible({ timeout: 10_000 });
  });
});
