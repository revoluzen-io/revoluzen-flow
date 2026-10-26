/**
 * CanvasCopyrightMark — Revoluzen brand mark pinned to the bottom-left
 * of the Editor's canvas column. The global CopyrightFooter is fixed
 * to the viewport's bottom-left and gets covered by the Assistant
 * panel on the Editor page, so we render a second copy *inside* the
 * canvas column where it's always visible.
 *
 * Same letter palette + typography as the global footer so the brand
 * mark reads as one consistent element wherever it appears.
 */

import { useDarkMode } from '../hooks/useDarkMode';

// 2026-06-15 — matches CopyrightFooter: palette sampled from the Revoluzen Flow
// logo mark (gold "F", green + red refresh arrows). No blue.
//
// 2026-10-23 — kept in step with CopyrightFooter's contrast fix. The sampled
// gold and green were 2.27:1 and 3.38:1 at 16px/800, below the 4.5:1 that
// applies at this size; each channel was scaled by a single factor so hue
// and channel mix are unchanged and only the value moves. The red already
// passed. See CopyrightFooter.tsx for the full note — these two constants
// must not drift apart, since they render the same mark in two places.
const BRAND_COLORS = [
  '#976C20',   // gold  — the logo "F"          (4.50:1 on amber-50, 4.67 on white)
  '#0D8550',   // green — top refresh arrow     (4.51:1 on amber-50, 4.67 on white)
  '#B03030',   // red   — bottom refresh arrow  (6.12:1 on amber-50, 6.34 on white)
];

export default function CanvasCopyrightMark() {
  const dark = useDarkMode();
  const year = new Date().getFullYear();
  const brand = '@revoluzen'.split('');

  return (
    <a
      href="https://revoluzen.io"
      target="_blank"
      rel="noopener noreferrer"
      // Hidden below 1536px. The canvas zoom toolbar is centred on the
      // same bottom edge, and the canvas column is narrower than the
      // window by both side panels, so the two meet well before the
      // window looks tight — measured overlapping at 1280. The mark
      // ends up half-under the toolbar, which reads as a bug rather
      // than a brand. The global CopyrightFooter still carries it, and
      // a decorative credit is the right thing to drop first when space
      // runs out.
      className="absolute bottom-3 left-3 z-10 hidden 2xl:inline-flex items-baseline gap-1.5 select-none"
      title="Built by Revoluzen"
    >
      <span
        className="text-base font-extrabold tracking-tight"
        style={{ color: dark ? '#F8FAFC' : '#0F172A' }}
      >
        © {year}
      </span>
      <span className="text-base font-extrabold tracking-tight">
        {brand.map((ch, i) => (
          <span key={i} style={{ color: BRAND_COLORS[i % BRAND_COLORS.length] }}>
            {ch}
          </span>
        ))}
      </span>
    </a>
  );
}
