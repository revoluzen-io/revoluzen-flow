import { chromium, type FullConfig } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

/**
 * Logs into Revoluzen Flow once and caches the authenticated session to
 * e2e/.auth/state.json (gitignored). Every spec then starts logged in via
 * `use.storageState`.
 *
 * Credentials come from env so nothing secret is committed:
 *   REVOLUZEN_FLOW_E2E_EMAIL     (default admin@revoluzen.local)
 *   REVOLUZEN_FLOW_E2E_PASSWORD  (required if the app shows a login form)
 * Target app from REVOLUZEN_FLOW_URL (default http://localhost:5174).
 */
const STATE_PATH = 'e2e/.auth/state.json';
const BASE_URL = process.env.REVOLUZEN_FLOW_URL ?? 'http://localhost:5174';
const EMAIL = process.env.REVOLUZEN_FLOW_E2E_EMAIL ?? 'admin@revoluzen.local';
const PASSWORD = process.env.REVOLUZEN_FLOW_E2E_PASSWORD ?? '';

export default async function globalSetup(_config: FullConfig) {
  mkdirSync(dirname(STATE_PATH), { recursive: true });
  const browser = await chromium.launch();
  const page = await browser.newPage();
  try {
    await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });

    // If a login form is shown, sign in. (Some dev setups auto-auth.)
    // Locate the fields by input TYPE, not placeholder text. The password
    // field's placeholder is bullet glyphs (U+2022 "••••••••") which are
    // trivially corrupted into mojibake on a bad re-encode — a
    // getByPlaceholder('••••••••') selector then silently matches nothing
    // and login never happens. type="email"/type="password" is stable.
    const email = page.locator('input[type="email"]').first();
    const passwordField = page.locator('input[type="password"]').first();
    const showsLogin = await email.isVisible({ timeout: 8_000 }).catch(() => false);

    if (showsLogin) {
      if (!PASSWORD) {
        throw new Error(
          'Revoluzen Flow shows a login form but REVOLUZEN_FLOW_E2E_PASSWORD is not set. ' +
          'Set REVOLUZEN_FLOW_E2E_PASSWORD (and optionally REVOLUZEN_FLOW_E2E_EMAIL) and retry.',
        );
      }
      await email.fill(EMAIL);
      await passwordField.fill(PASSWORD);
      await page.keyboard.press('Enter');
      // Login is complete once the form unmounts (password field detaches).
      // This is encoding-proof and independent of any nav label; on a failed
      // login the field stays mounted and this wait times out with a clear
      // signal. The nav-text check is a best-effort secondary confirmation.
      await passwordField.waitFor({ state: 'detached', timeout: 20_000 });
      await page.getByText('Workflows', { exact: false }).first()
        .waitFor({ state: 'visible', timeout: 20_000 }).catch(() => {});
    }

    // Mark onboarding as done before the state is saved.
    //
    // 2026-10-23: this is why visual-layout failed at 1024/740/390 and
    // functional failed on "Test Node". The first-run OnboardingWizard
    // ("Welcome to Revoluzen Flow") renders over the whole app with a
    // dimming backdrop, so anything behind it is in the DOM, enabled, and
    // permanently unclickable — Playwright reports "waiting for element to
    // be visible, enabled and stable" and times out 60s later, which reads
    // like a layout bug and is not one. The widths that passed simply never
    // clicked anything underneath it.
    //
    // App.tsx keys the wizard on this one localStorage flag, so setting it
    // here puts every spec in the state of a returning user, which is what
    // the suite is actually trying to test. Same class of trap as the
    // contrast audit, which measured the modal instead of the page.
    await page.evaluate(() => {
      try { localStorage.setItem('revoluzen_flow_onboarding_done', '1'); } catch { /* private mode */ }
    });

    await page.context().storageState({ path: STATE_PATH });
  } finally {
    await browser.close();
  }
}
