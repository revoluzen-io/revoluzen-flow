/**
 * CopyrightFooter
 * ─────────────────────────────────────────────────────────────────────────
 * Revoluzen brand mark pinned to the bottom-LEFT corner. Each letter of
 * "@revoluzen" cycles through the brand palette (gold / green / red) —
 * the three colours of the Revoluzen Flow logo's refresh-cycle motif. Apr 18
 * revision: removed the drop-shadow glow (looked muddy against the light
 * page backgrounds) and bumped the font one step for better legibility.
 */
import { useDarkMode } from '../hooks/useDarkMode';

// 2026-06-15 — palette re-sampled from the actual Revoluzen Flow logo mark
// (public/revoluzen-flow-logo-mark.png): amber-gold "F", green top arrow, red
// bottom arrow. The old palette's blue (#3B82F6) was never a brand colour.
// 2026-10-23 — gold and green darkened to clear WCAG 1.4.3 AA. As sampled
// they were 2.27:1 and 3.38:1 on white at 16px/800, which is not "large
// text", so 4.5:1 applies. Each channel was scaled by a single factor so the
// hue and the relative channel mix are unchanged — these are the same two
// inks, a step darker (k=0.676 gold, k=0.832 green). The red was already
// passing and is untouched.
//
// Targeted against amber-50 (#fffbeb), not white: the footer is
// position:fixed and floats over whatever page surface is beneath it, and
// amber-50 is the lightest one in the app. Clearing 4.5:1 there clears it
// everywhere. If the brand wants the original lighter inks back, the
// alternative is to set them on a dark plate rather than on the page.
//
// It floats over tables too, so it sits on a light plate of its own: without
// one, a row scrolling beneath it reads as garbled text.
const BRAND_COLORS = [
  '#976C20',   // gold  — the logo "F"          (4.50:1 on amber-50, 4.67 on white)
  '#0D8550',   // green — top refresh arrow     (4.51:1 on amber-50, 4.67 on white)
  '#B03030',   // red   — bottom refresh arrow  (6.12:1 on amber-50, 6.34 on white)
];

export default function CopyrightFooter({ environment = 'dev' }: { environment?: 'dev' | 'prod' } = {}) {
  const dark = useDarkMode();
  const isProd = environment === 'prod';
  const year = new Date().getFullYear();

  const brand = '@revoluzen'.split('');

  return (
    <div
      className="fixed bottom-2.5 left-4 z-30 pointer-events-none select-none"
      aria-label="Copyright — Revoluzen"
    >
      <a
        href="https://revoluzen.io"
        target="_blank"
        rel="noopener noreferrer"
        className={`pointer-events-auto inline-flex items-baseline gap-1.5 rounded-md px-1.5 backdrop-blur-sm transition-colors ${
          isProd || dark ? 'text-slate-300 bg-slate-900/70' : 'text-slate-500 bg-white/80'
        }`}
        title="Built by Revoluzen"
      >
        <span
          className="text-base font-extrabold tracking-tight"
          style={{ color: dark || isProd ? '#F8FAFC' : '#0F172A' }}
        >
          © {year}
        </span>
        <span className="text-base font-extrabold tracking-tight">
          {brand.map((ch, i) => (
            <span
              key={i}
              style={{ color: BRAND_COLORS[i % BRAND_COLORS.length] }}
            >
              {ch}
            </span>
          ))}
        </span>
      </a>
    </div>
  );
}
