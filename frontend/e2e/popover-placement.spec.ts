import { test, expect } from '@playwright/test';
import { openEditor } from './helpers';

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
 * Self-test: the detector must be able to fail.
 *
 * Every "all panels on screen" result below is worth exactly as much as
 * this test. Twice now a placement check passed while a panel really was
 * off-screen — once because it measured the largest floating box (the
 * Copilot drawer) instead of the panel that opened, once because the
 * app's first-run wizard ate the clicks so nothing opened at all. Both
 * printed green.
 *
 * So: inject a panel 233px off the left edge — the exact shape of the
 * original Steward bug — and require the detector to see it. This runs
 * in the suite rather than as a one-off source edit, because the last
 * hand-rolled negative control was left behind in a component and
 * shipped the bug back.
 */
test('the off-screen detector actually detects', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: NARROW.height });
  await openEditor(page);

  const before = await floaters(page);
  await page.evaluate(() => {
    const d = document.createElement('div');
    d.id = '__placement_selftest';
    d.style.cssText =
      'position:fixed;left:-233px;top:120px;width:828px;height:400px;background:#fff;z-index:9999';
    document.body.appendChild(d);
  });
  const panels = appeared(before, await floaters(page));
  await page.evaluate(() => document.getElementById('__placement_selftest')?.remove());

  const offLeft = panels.filter((p) => p.left < 0).map((p) => p.left);
  expect(
    offLeft,
    'The detector did not flag a panel hanging 233px off the left edge, so '
    + 'every other assertion in this file is vacuous. Fix the measurement '
    + 'before trusting a green run.',
  ).toContain(-233);
});

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

/**
 * Widths, not one width.
 *
 * This spec first checked 900px only, and passed — while the
 * notifications panel was starting 210px off the left edge at 768px and
 * the account menu 62px off. One sample proves one sample: right-anchored
 * panels fail progressively as the window narrows and the trigger walks
 * left, so the guard has to cover the narrow end where they actually
 * break, not the comfortable middle.
 */
const WIDTHS = [1280, 900, 768];

for (const t of TRIGGERS) {
 for (const width of WIDTHS) {
  test(`${t.name} opens on screen at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: NARROW.height });
    // openEditor waits for the canvas pane and dismisses the first-run
    // wizard, whose backdrop otherwise swallows every click — measured
    // through it, all four triggers report "no panel appeared" and the
    // run still prints a clean result.
    await openEditor(page);

    // Wait for the control, don't sleep at it. A fixed 2.5s pause passed
    // when this spec ran alone and failed inside the full suite, where a
    // loaded machine had not finished mounting the editor toolbar yet —
    // reported as "Pipeline actions not found", which reads like the
    // control was deleted rather than like a slow boot.
    const trigger = page.locator(t.selector).first();
    await expect(
      trigger,
      `${t.name} never appeared, matched by \`${t.selector}\`. Either the `
      + 'control was removed/renamed (update TRIGGERS) or it is edition-gated '
      + 'and does not belong in this list — do NOT convert this back into a skip.',
    ).toBeVisible({ timeout: 20_000 });

    const before = await floaters(page);
    await trigger.click();
    await page.waitForTimeout(600);

    const panels = appeared(before, await floaters(page));
    expect(
      panels.length,
      `${t.name} opened no new panel at ${width}px. The trigger or selector `
      + 'may have moved — but check first that nothing is covering the app: a '
      + 'first-run wizard swallowing every click reported exactly this for all '
      + 'four triggers at all widths, and still printed a confident pass.',
    ).toBeGreaterThan(0);

    for (const p of panels) {
      expect(p.left, `${t.name} runs off the LEFT edge at ${width}px (left=${p.left}, width=${p.width})`)
        .toBeGreaterThanOrEqual(0);
      expect(p.right, `${t.name} runs off the RIGHT edge at ${width}px (right=${p.right} > ${p.viewport})`)
        .toBeLessThanOrEqual(p.viewport);
    }
  });
 }
}
