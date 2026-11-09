// Layout guard — open every menu, dropdown and drawer in the app and fail if
// any panel it opens leaves the viewport.
//
// Why a sweep and not just a spec: `absolute right-0` is the dominant dropdown
// pattern in this codebase, and it is only safe while the trigger happens to
// sit near the right edge of the window. That is a property of the LAYOUT, not
// of the component, so it silently breaks whenever the nav changes. The Steward
// findings panel was `absolute right-0` with `width: min(92vw, 640px)` on a
// trigger in the middle of the global nav: at 900px its left edge landed near
// -233, so the first ~230px of every finding — headings, table names, tab
// labels — was off-screen with no way to scroll to it. It was correct at 1920
// and broken at 900, and nothing caught it, because the layout checks that
// existed only ever measured DEFAULT page states. Nothing was ever opened.
//
// `e2e/popover-placement.spec.ts` is the standing regression guard for a few
// named controls. THIS is the breadth net: every menu-ish trigger on every
// page at several widths.
//
// ── Read this before changing the detector ──────────────────────────────────
// Three earlier versions of this check could not have failed, and each one
// reported success:
//   1. It measured "the largest floating panel on screen", which on the editor
//      is the Copilot drawer — never the menu under test. Removing the Steward
//      clamp did not make it fail. Hence the before/after DIFF below: judge
//      only the panels that APPEARED when the trigger was clicked.
//   2. Its page-side code was passed to `evaluate` as a template STRING, where
//      `return` followed by a newline is `return;` under automatic semicolon
//      insertion. It silently returned undefined. Hence real functions.
//   3. Its destructive-word blocklist matched "duplicate" in the Steward
//      trigger's tooltip ("duplicate detection + reliability findings") and so
//      refused to open the one panel that was broken, while reporting zero
//      offenders. Hence: a DECLARED disclosure is always opened, and every
//      blocklist exclusion is printed.
// The SELF-TEST below exists for the same reason. A clean run means nothing
// unless the detector can be shown to catch a real offender, so it injects one
// and refuses to continue if the detector misses it.
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

import { chromium } from '@playwright/test'

const here = dirname(fileURLToPath(import.meta.url))
const frontend = join(here, '..')

const URL = process.env.REVOLUZEN_FLOW_URL ?? 'http://localhost:5174'
const STATE = process.env.REVOLUZEN_FLOW_E2E_STATE ?? join(frontend, 'e2e', '.auth', 'state.json')
// 768 is not padding: at 1280 and 900 every panel in the app measured clean
// while the notifications panel was starting 210px off the left edge at 768
// and the account menu 62px off. Right-anchored panels fail progressively as
// the window narrows and the trigger walks left, so the narrow end is where
// this guard earns its keep.
const WIDTHS = (process.env.REVOLUZEN_FLOW_WIDTHS ?? '1280,900,768')
  .split(',').map((w) => Number(w.trim())).filter(Boolean)

const PAGES = ['dashboard', 'projects', 'pipelines', 'editor', 'connections',
               'pool', 'storage', 'insights', 'executions', 'settings', 'help']

// Drawers are NOT reachable through the menu sweep: their labels carry no menu
// word ("Expand assistant panel"), and a docked drawer is a flex sibling of the
// canvas rather than a positioned box, so it never enters the measured set.
// They also fail differently — a docked drawer cannot run off the left edge,
// it widens the document past the viewport. Both are checked below.
// `page` matters: the Copilot launcher is deliberately not mounted on the
// editor (App.tsx hides it there), so probing it on /editor checks nothing.
const DRAWERS = [
  { name: 'Assistant drawer', page: 'editor', sel: '[title="Expand assistant panel"], [title="Assistant"]' },
  { name: 'Activities drawer', page: 'editor', sel: '[title="Expand activities panel"]' },
  { name: 'Copilot dock', page: 'dashboard', sel: '[aria-label="Open AI agent"], [aria-label="Open Copilot dock"]' },
]

// Looks like it opens something.
const MENUISH = /columns|export|filter|sort|more|actions|options|view|group|project|tag|scope|environment|notification|steward|account|profile|menu|template|density|outline|help|settings|add|new|import/i
// Looks like it DOES something. Only consulted for triggers that do not
// declare themselves a disclosure — see note 3 in the header.
const DANGER = /delete|remove|archive|run\b|publish|deploy|save|reset|clear|revoke|disable|stop|cancel|sign out|log ?out|upload|submit|merge|approve|reject|restore|duplicate|rename|execute|test/i

const MIN_W = 120
const MIN_H = 60

if (!existsSync(STATE)) {
  console.error(
    `check-popover-placement: FAIL — no saved login at ${STATE}.\n`
    + 'Run the E2E suite once first; e2e/global-setup.ts logs in and caches the\n'
    + 'session there (see e2e/README.md). Or point REVOLUZEN_FLOW_E2E_STATE at one.',
  )
  process.exit(1)
}

/** Every floating box on screen, with geometry. Keyed by position+width. */
const floaters = (page) => page.evaluate(({ minW, minH }) =>
  [...document.querySelectorAll('div,ul,section,aside')]
    .filter((d) => {
      const s = getComputedStyle(d)
      if (s.position !== 'fixed' && s.position !== 'absolute') return false
      if (s.visibility === 'hidden' || s.display === 'none') return false
      // Hidden from users by contract — a closed drawer or an inert
      // overlay. Not a placement defect.
      if (d.closest('[aria-hidden="true"]')) return false
      const b = d.getBoundingClientRect()
      return b.width > minW && b.height > minH
    })
    .map((d) => {
      const b = d.getBoundingClientRect()
      return {
        key: `${Math.round(b.left)},${Math.round(b.top)},${Math.round(b.width)}`,
        left: Math.round(b.left),
        right: Math.round(b.right),
        bottom: Math.round(b.bottom),
        width: Math.round(b.width),
        cls: String(d.className || '').slice(0, 44),
      }
    }), { minW: MIN_W, minH: MIN_H })

const viewport = (page) => page.evaluate(() => ({
  vw: window.innerWidth,
  vh: window.innerHeight,
  scrollWidth: document.documentElement.scrollWidth,
}))

/**
 * How far a box escapes the viewport, if at all.
 *
 * A box that does not INTERSECT the viewport horizontally is parked, not
 * clipped: slide-out drawers sit at `right-0` translated fully out while
 * closed, which reads as "420px off the right edge" and is exactly how it
 * should read. The defect this guard is for is a panel the user can see
 * part of and cannot reach — i.e. one that overlaps the viewport and
 * still has content outside it.
 */
function escapes(box, vp) {
  if (box.right <= 0 || box.left >= vp.vw) return null
  const offL = Math.max(0, -box.left)
  const offR = Math.max(0, box.right - vp.vw)
  const offB = Math.max(0, box.bottom - vp.vh)
  return offL + offR + offB === 0 ? null : { offL, offR, offB }
}

const browser = await chromium.launch()
const offenders = []
const skipped = new Set()
// Coverage, not just correctness. See the exit check at the bottom.
let openedTotal = 0

// ── Self-test ───────────────────────────────────────────────────────────────
// Inject a panel hanging off the left edge — the exact shape of the bug this
// guard exists for — and require the detector to flag it.
{
  const ctx = await browser.newContext({ storageState: STATE, viewport: { width: 900, height: 900 } })
  const page = await ctx.newPage()
  await page.goto(`${URL}/#dashboard`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const before = new Set((await floaters(page)).map((f) => f.key))
  await page.evaluate(() => {
    const d = document.createElement('div')
    d.id = '__placement_selftest'
    d.style.cssText = 'position:fixed;left:-233px;top:120px;width:828px;height:400px;background:#fff;z-index:9999'
    document.body.appendChild(d)
  })
  const vp = await viewport(page)
  const caught = (await floaters(page))
    .filter((f) => !before.has(f.key))
    .map((f) => escapes(f, vp))
    .find((e) => e && e.offL === 233)
  await page.evaluate(() => document.getElementById('__placement_selftest')?.remove())
  await ctx.close()
  if (!caught) {
    console.error(
      'check-popover-placement: FAIL — SELF-TEST did not catch a panel 233px off\n'
      + 'the left edge. The detector is blind, so any result it reports is\n'
      + 'meaningless. Fix the detector before trusting this guard.',
    )
    await browser.close()
    process.exit(1)
  }
  console.log('self-test ok — detector caught a panel 233px off the left edge\n')
}

// ── Are we actually signed in? ──────────────────────────────────────────────
// `storageState` is origin-scoped. A state file saved against a different
// origin — a previous run's auto-picked port, say — loads without error and
// grants nothing, so the sweep crawls the LOGIN page: four buttons, none of
// them menu-ish, zero panels opened, and a confident "every panel opened
// fully on screen". That happened. Fail here instead, where the cause is
// obvious.
{
  const ctx = await browser.newContext({ storageState: STATE, viewport: { width: 1280, height: 900 } })
  const page = await ctx.newPage()
  await page.goto(`${URL}/#editor`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  const loggedOut = await page.evaluate(() => !!document.querySelector('input[type="password"]'))
  const origins = await page.evaluate(() => location.origin)
  await ctx.close()
  if (loggedOut) {
    console.error(
      `check-popover-placement: FAIL — not signed in at ${origins}.\n`
      + `The saved session at ${STATE} does not apply to this origin, so the\n`
      + 'sweep would crawl the login page and report success having opened\n'
      + 'nothing. Re-run the E2E suite from this directory so global-setup\n'
      + 'rewrites the state for this origin, or point REVOLUZEN_FLOW_E2E_STATE\n'
      + 'at the right file. Note that global-setup writes a RELATIVE path, so\n'
      + 'it lands next to the cwd you launched Playwright from.',
    )
    await browser.close()
    process.exit(1)
  }
}

// ── Menus and dropdowns ─────────────────────────────────────────────────────
for (const width of WIDTHS) {
  const ctx = await browser.newContext({ storageState: STATE, viewport: { width, height: 900 } })
  const page = await ctx.newPage()

  for (const p of PAGES) {
    await page.goto(`${URL}/#${p}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    const found = await page.evaluate(({ menuish, danger }) => {
      const M = new RegExp(menuish, 'i')
      const D = new RegExp(danger, 'i')
      const triggers = []
      const excluded = []
      ;[...document.querySelectorAll('button')].forEach((b, i) => {
        const r = b.getBoundingClientRect()
        if (r.width < 8 || r.height < 8 || b.disabled) return
        const label = [
          b.getAttribute('title') || '',
          b.getAttribute('aria-label') || '',
          b.textContent || '',
        ].join(' ').replace(/\s+/g, ' ').trim()
        const short = label.slice(0, 40) || '(icon)'
        // A declared disclosure is always safe to OPEN; the destructive items
        // live inside it, and this sweep never clicks those.
        const disclosure = b.getAttribute('aria-haspopup') !== null
                        || b.getAttribute('aria-expanded') !== null
        if (!disclosure && D.test(label)) { excluded.push(short); return }
        if (!disclosure && !M.test(label)) return
        b.setAttribute('data-placement-trigger', String(i))
        triggers.push({ i, label: short, disclosure })
      })
      return { triggers, excluded }
    }, { menuish: MENUISH.source, danger: DANGER.source })

    let navigatedAway = 0
    for (const t of found.triggers) {
      const btn = page.locator(`[data-placement-trigger="${t.i}"]`)
      if (!(await btn.count())) continue
      const before = new Set((await floaters(page)).map((f) => f.key))
      const hashBefore = await page.evaluate(() => location.hash)
      await btn.click({ timeout: 2500 }).catch(() => {})
      await page.waitForTimeout(500)

      // Some triggers match MENUISH but are nav items ("Projects"), and a
      // click navigates instead of opening anything. The before/after diff
      // then compares two different PAGES, and every panel on the new one
      // counts as "newly appeared" — which is how a closed slide-out drawer
      // on the destination page got reported as an off-screen menu. Skip
      // the measurement and go back.
      if (await page.evaluate(() => location.hash) !== hashBefore) {
        navigatedAway++
        await page.goto(`${URL}/#${p}`, { waitUntil: 'domcontentloaded' })
        await page.waitForTimeout(1500)
        continue
      }

      const vp = await viewport(page)
      for (const box of await floaters(page)) {
        if (before.has(box.key)) continue
        const off = escapes(box, vp)
        if (!off) continue
        offenders.push({ width, page: p, trigger: t.label, ...off, ...box })
        console.log(
          `OFF  ${String(width).padStart(4)} ${p.padEnd(11)} "${t.label}" -> `
          + `left=${box.left} w=${box.width} offL=${off.offL} offR=${off.offR} offB=${off.offB}  ${box.cls}`,
        )
      }
      await page.keyboard.press('Escape').catch(() => {})
      await page.waitForTimeout(250)
    }

    openedTotal += found.triggers.length
    const decl = found.triggers.filter((x) => x.disclosure).length
    console.log(
      `     ${String(width).padStart(4)} ${p.padEnd(11)} ${found.triggers.length} opened `
      + `(${decl} declared disclosures), ${found.excluded.length} skipped`
      + (navigatedAway ? `, ${navigatedAway} navigated` : ''),
    )
    for (const s of found.excluded) skipped.add(`${p}: ${s}`)
  }
  await ctx.close()
}

// ── Drawers ─────────────────────────────────────────────────────────────────
console.log('')
for (const width of WIDTHS) {
  const ctx = await browser.newContext({ storageState: STATE, viewport: { width, height: 900 } })
  const page = await ctx.newPage()
  for (const d of DRAWERS) {
    await page.goto(`${URL}/#${d.page}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
    const btn = page.locator(d.sel).first()
    if (!(await btn.count())) {
      // Expected in some states: at wide widths the docked drawers are already
      // expanded, so only their "Collapse" control is rendered.
      console.log(`     ${String(width).padStart(4)} ${d.name.padEnd(18)} toggle not rendered here`)
      continue
    }
    const base = await viewport(page)
    const before = new Set((await floaters(page)).map((f) => f.key))
    await btn.click({ timeout: 3000 }).catch(() => {})
    await page.waitForTimeout(1000)

    const vp = await viewport(page)
    const problems = []
    // 1px of slack for sub-pixel rounding.
    if (vp.scrollWidth > vp.vw + 1 && base.scrollWidth <= base.vw + 1) {
      problems.push(`H-SCROLL introduced: scrollWidth=${vp.scrollWidth} > vw=${vp.vw}`)
    }
    for (const box of await floaters(page)) {
      if (before.has(box.key)) continue
      const off = escapes(box, vp)
      if (off) problems.push(`offL=${off.offL} offR=${off.offR} offB=${off.offB} [${box.cls}]`)
    }
    if (problems.length) {
      offenders.push({ width, page: d.page, trigger: d.name, drawer: true, problems })
      console.log(`OFF  ${String(width).padStart(4)} ${d.name.padEnd(18)} <<< ${problems.join('  ')}`)
    } else {
      console.log(`     ${String(width).padStart(4)} ${d.name.padEnd(18)} ok (scrollWidth=${vp.scrollWidth}/${vp.vw})`)
    }
    await page.keyboard.press('Escape').catch(() => {})
    await page.waitForTimeout(300)
  }
  await ctx.close()
}
await browser.close()

// ── Report ──────────────────────────────────────────────────────────────────
// The skip list is part of the result, not trivia: "0 offenders" is only as
// good as the set of triggers that were actually opened, and a menu hiding in
// this list is a menu nobody checked.
console.log(`\n==== ${skipped.size} triggers NOT opened (looked destructive) ====`)
for (const s of [...skipped].sort()) console.log(`  ${s}`)

// A clean result is only as good as its coverage. "No offenders" out of no
// triggers is the shape every false pass in this file's history took, so it
// is an error, not a pass.
if (openedTotal === 0) {
  console.error(
    '\ncheck-popover-placement: FAIL — opened 0 triggers across every page and\n'
    + 'width, so "no offenders" means nothing was tested. Check that the app\n'
    + 'rendered and that the trigger heuristics still match this build.',
  )
  process.exit(1)
}

if (offenders.length === 0) {
  console.log(`\ncheck-popover-placement: OK — ${openedTotal} triggers opened, every panel fully on screen.`)
  process.exit(0)
}

const unique = new Map()
for (const o of offenders) unique.set(`${o.page} "${o.trigger}"`, o)
console.error(`\ncheck-popover-placement: FAIL — ${unique.size} control(s) open off-screen:`)
for (const [k, o] of unique) {
  console.error(o.drawer
    ? `  ${k} — ${o.problems.join('; ')}`
    : `  ${k} — offL=${o.offL} offR=${o.offR} offB=${o.offB} (w=${o.width}, ${o.cls})`)
}
console.error(
  '\nRight-anchoring (`absolute right-0`) only works for a trigger near the right\n'
  + 'edge. Use the measured, viewport-clamped `fixed` panel instead:\n'
  + '  const pos = useAnchoredPanel(open, triggerRef, { width: 420 })\n'
  + 'from src/hooks/useAnchoredPanel.ts, then render `fixed` with pos.top/left/\n'
  + 'width. The Steward panel, the notification bell and the account menu all\n'
  + 'share it — this bug had to be fixed three times before it was extracted.',
)
process.exit(1)
