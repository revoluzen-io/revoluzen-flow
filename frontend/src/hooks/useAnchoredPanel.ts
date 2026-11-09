import { useEffect, useState, type RefObject } from 'react';

export type AnchoredPanelPos = { top: number; left: number; width: number };

/**
 * Position a popover against its trigger, clamped to the viewport.
 *
 * `absolute right-0 top-full` is the usual way to hang a panel off a
 * button, and it is only correct while that button happens to sit near
 * the right edge of the window — a property of the surrounding layout,
 * not of the component. Every time the nav changed, panels anchored
 * this way silently slid off-screen: the Steward findings panel lost
 * its first ~230px at 900px, and at 768px the notifications panel
 * (420px wide) started 210px off the left edge and the account menu
 * 62px off. Nothing clips or scrolls a panel positioned that way, so
 * the content is simply unreachable.
 *
 * This measures the trigger instead and right-aligns to it only where
 * there is room, pushing the panel back inside the viewport when there
 * is not. `fixed` rather than `absolute` so a transformed or scrolling
 * ancestor cannot redefine what the offsets resolve against.
 *
 * Returns null while closed, so callers can skip rendering entirely.
 */
export function useAnchoredPanel(
  open: boolean,
  anchorRef: RefObject<HTMLElement | null>,
  opts: { width: number; gutter?: number; offset?: number } = { width: 320 },
): AnchoredPanelPos | null {
  const { width: preferredWidth, gutter = 12, offset = 8 } = opts;
  const [pos, setPos] = useState<AnchoredPanelPos | null>(null);

  useEffect(() => {
    if (!open) { setPos(null); return; }
    const measure = () => {
      const r = anchorRef.current?.getBoundingClientRect();
      if (!r) return;
      // Narrow windows get a narrower panel rather than one that
      // overflows: the gutter is reserved on BOTH sides first.
      const width = Math.min(preferredWidth, window.innerWidth - gutter * 2);
      const left = Math.max(
        gutter,
        Math.min(r.right - width, window.innerWidth - width - gutter),
      );
      setPos({ top: r.bottom + offset, left, width });
    };
    measure();
    window.addEventListener('resize', measure);
    // Capture phase: a scroll inside any ancestor moves the trigger too.
    window.addEventListener('scroll', measure, true);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, [open, anchorRef, preferredWidth, gutter, offset]);

  return pos;
}

export default useAnchoredPanel;
