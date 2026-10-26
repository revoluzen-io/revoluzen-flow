import { test, expect } from '@playwright/test';

/**
 * Menus and popovers must open fully on screen (2026-10-26).
 *
 * The Steward findings panel was `absolute right-0` with
 * `width: min(92vw, 640px)` — the right edge pinned to its TRIGGER,
 * which sits in the middle of the global nav rather than near the right
 * edge of the window. At 900px that put an 828px panel's left edge near
 * -233, so the first ~230px of every finding was off-screen with no way
 * to scroll to it: headings, table names and tab labels all cut.
 *
 * Nothing caught it. The layout checks that existed only ever measured
 * DEFAULT page states, so every dropdown, drawer and findings panel in
 * the app went unexercised. `absolute right-0` is the dominant pattern
 * here (21 call sites) and it is only safe while the trigger happens to
 * sit near the right edge — a property of layout, not of the component,
 * and one that changes whenever the nav does.
 *
 * This opens the menus at the narrow width where the bug appeared and
 * asserts each panel is inside the viewport. It is deliberately about
 * PLACEMENT only; what the panels contain is other specs' business.
 */

/**
 * 900px only, deliberately. Right-anchored panels fail by running off
 * the LEFT edge as the viewport narrows, so the wide case cannot catch
 * this class at all — the Steward panel was correct at 1920 and broken
 * at 900. Breadth — every menu-ish trigger on every page at several
 * widths — was covered by a one-off sweep during the audit; this spec
 * is the standing regression guard for the controls it names.
 */
const NARROW = { width: 900, height: 900 };

type Floater = {
  key: string;
  left: number;
  right: number;
  width: number;
  viewport: number;
};

/**
 * Every floating box currently on screen, with its geometry.
 *
 * Measured in a real function (not a templated `evaluate` string) —
 * the string version silently returned `undefined`, because a `return`
 * followed by a newline is `return;` under automatic semicolon
 * insertion, and the measurement was dead code after it.
 */
function floaters(page: import('@playwright/test').Page): Promise<Floater[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll('div,ul,section')]
      .filter((d) => {
        const s = getComputedStyle(d);
        if (s.position !== 'fixed' && s.position !== 'absolute') return false;
        if (s.visibility === 'hidden' || s.display === 'none') return false;
        const b = d.getBoundingClientRect();
        return b.width > 160 && b.height > 80;
      })
      .map((d) => {
        const b = d.getBoundingClientRect();
        return {
          key: `${Math.round(b.left)},${Math.round(b.top)},${Math.round(b.width)}`,
          left: Math.round(b.left),
          right: Math.round(b.right),
          width: Math.round(b.width),
          viewport: window.innerWidth,
        };
      }),
  );
}

/**
 * The panels that appeared since `before` — NOT "the biggest box on
 * screen". The first version of this took the largest floating element,
 * which on the editor is the Copilot drawer, so the assertion never
 * looked at the menu under test: with the Steward clamp deliberately
 * removed it still passed. A placement test that cannot fail is worse
 * than no test, so the diff is the whole point.
 */
function appeared(before: Floater[], after: Floater[]): Floater[] {
  const seen = new Set(before.map((f) => f.key));
  return after.filter((f) => !seen.has(f.key));
}

/**
 * Controls that MUST be present in this build. A missing one fails the
 * test rather than skipping it, because skipping is how fiction gets in:
 * an earlier list named a "Table columns" menu on /pipelines that does
 * not exist anywhere in this app, and the silent skip let it read as a
 * pass for a whole run.
 *
 * Deliberately NOT listed: the workspace switcher. It is edition-gated
 * behind `WORKSPACES_ENABLED` (false in OSS — see src/config/edition.ts)
 * so it never renders here. Add it only to a spec that runs a Plus
 * build, or it becomes another permanent skip.
 */
const TRIGGERS: Array<{ name: string; page: string; selector: string }> = [
  { name: 'Steward findings', page: 'editor', selector: 'button[aria-label="Steward findings"]' },
  { name: 'Notifications', page: 'editor', selector: 'button[aria-label="Notifications"]' },
  { name: 'Pipeline actions', page: 'editor', selector: 'button[title="Pipeline actions"]' },
  { name: 'Account menu', page: 'editor', selector: 'button[title*="Account menu" i]' },
];

for (const t of TRIGGERS) {
  test(`${t.name} opens on screen at ${NARROW.width}px`, async ({ page }) => {
    await page.setViewportSize(NARROW);
    await page.goto(`/#${t.page}`);
    await page.waitForTimeout(2500);

    const trigger = page.locator(t.selector).first();
    expect(
      await trigger.count(),
      `${t.name} not found by \`${t.selector}\`. Either the control was `
      + 'removed/renamed (update TRIGGERS) or it is edition-gated and does '
      + 'not belong in this list — do NOT convert this back into a skip.',
    ).toBeGreaterThan(0);

    const before = await floaters(page);
    await trigger.click();
    await page.waitForTimeout(600);

    const panels = appeared(before, await floaters(page));
    expect(panels.length, `${t.name} opened no new panel — the trigger or the selector has moved`)
      .toBeGreaterThan(0);

    for (const p of panels) {
      expect(p.left, `${t.name} runs off the LEFT edge (left=${p.left}, width=${p.width})`)
        .toBeGreaterThanOrEqual(0);
      expect(p.right, `${t.name} runs off the RIGHT edge (right=${p.right} > ${p.viewport})`)
        .toBeLessThanOrEqual(p.viewport);
    }
  });
}
