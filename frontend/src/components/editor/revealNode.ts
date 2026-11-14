/**
 * Keeps the node being edited in full view, with its place in the flow, and
 * the canvas still while its settings are open.
 *
 * A node's settings open in a dock along the bottom of the canvas, so a node
 * low on the canvas, or one reached from the dock's own buttons, could end up
 * half behind it. Seeing the node alone is not enough either: a Reconcile
 * step whose two inputs were cut off at the edges of the strip above the dock
 * did not show what it compares, and a pipeline whose first row was cut off
 * at the top did not show how the data gets there. So while the dock is open,
 * the whole drawing shows in full when it fits at a size its names can still
 * be read (WHOLE_MIN_ZOOM): when any step is cut, the canvas glides to put the
 * drawing in the middle of the strip, zooming out only as far as it needs.
 * When it does not fit, the node and the steps wired straight into and out of
 * it show instead, zoomed out never below half size; when even they cannot
 * fit, the node alone is brought into view. When the dock closes, the canvas
 * glides back to where it was, unless the user zoomed or panned meanwhile.
 *
 * When everything already shows in full, nothing moves. Moving a node the
 * user is looking at is what once made people ask "why are you moving the
 * node?", so the canvas only moves when it has to.
 */
import { useSyncExternalStore } from 'react';

import { useWorkflowStore } from '../../stores/workflowStore';

/** Space kept between the node and the edges of the strip it shows in. */
const MARGIN = 20;
/** The smallest zoom a reveal goes down to; below it names stop being legible. */
const MIN_ZOOM = 0.5;
/**
 * The smallest zoom the whole drawing is shown at; below it, the node and its neighbours instead. Half size, as
 * for the neighbours: an eight-step pipeline on two rows needs about 0.6 above the dock on a laptop screen, and
 * at 0.65 it was left with its second row behind the dock and its last step cut at the edge.
 */
export const WHOLE_MIN_ZOOM = 0.5;
/** How long the glide takes, in milliseconds. */
const GLIDE_MS = 320;

export interface ScreenBox {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export interface CanvasViewport {
  x: number;
  y: number;
  zoom: number;
}

/**
 * The viewport that shows the node in full above `coverTop` (the top edge of
 * whatever covers the lower part of the canvas), or null when the node shows
 * in full already. All boxes are in screen pixels.
 */
export function viewportToReveal(
  node: ScreenBox,
  pane: ScreenBox,
  coverTop: number,
  viewport: CanvasViewport,
): CanvasViewport | null {
  const area = {
    top: pane.top,
    bottom: Math.min(pane.bottom, coverTop),
    left: pane.left,
    right: pane.right,
  };
  const inView =
    node.top >= area.top + MARGIN &&
    node.bottom <= area.bottom - MARGIN &&
    node.left >= area.left + MARGIN &&
    node.right <= area.right - MARGIN;
  if (inView) return null;

  const width = node.right - node.left;
  const height = node.bottom - node.top;
  const roomWidth = area.right - area.left - 2 * MARGIN;
  const roomHeight = area.bottom - area.top - 2 * MARGIN;
  if (width <= 0 || height <= 0 || roomWidth <= 0 || roomHeight <= 0) return null;

  const { x, y, zoom } = viewport;
  // Zoom out only as far as the node needs to fit, and never below
  // MIN_ZOOM (unless the user is already zoomed out further).
  const fit = Math.min(1, roomWidth / width, roomHeight / height);
  const nextZoom = Math.max(Math.min(zoom, MIN_ZOOM), zoom * fit);

  // The node's middle in canvas units, then placed at the strip's middle.
  const middleX = ((node.left + node.right) / 2 - pane.left - x) / zoom;
  const middleY = ((node.top + node.bottom) / 2 - pane.top - y) / zoom;
  const targetX = (area.left + area.right) / 2 - pane.left;
  const targetY = (area.top + area.bottom) / 2 - pane.top;
  return { x: targetX - middleX * nextZoom, y: targetY - middleY * nextZoom, zoom: nextZoom };
}

function around(boxes: ScreenBox[]): ScreenBox {
  return {
    top: Math.min(...boxes.map((b) => b.top)),
    bottom: Math.max(...boxes.map((b) => b.bottom)),
    left: Math.min(...boxes.map((b) => b.left)),
    right: Math.max(...boxes.map((b) => b.right)),
  };
}

/**
 * The viewport that shows the node and the steps wired to it (`neighbours`)
 * in full above `coverTop`, or null when they all show already. When they
 * cannot fit at half size, whatever brings the node alone into view.
 */
export function viewportToShowFlow(
  node: ScreenBox,
  neighbours: ScreenBox[],
  pane: ScreenBox,
  coverTop: number,
  viewport: CanvasViewport,
): CanvasViewport | null {
  if (!neighbours.length) return viewportToReveal(node, pane, coverTop, viewport);
  const all = around([node, ...neighbours]);
  const area = {
    top: pane.top,
    bottom: Math.min(pane.bottom, coverTop),
    left: pane.left,
    right: pane.right,
  };
  const inView =
    all.top >= area.top + MARGIN &&
    all.bottom <= area.bottom - MARGIN &&
    all.left >= area.left + MARGIN &&
    all.right <= area.right - MARGIN;
  if (inView) return null;

  const width = all.right - all.left;
  const height = all.bottom - all.top;
  const roomWidth = area.right - area.left - 2 * MARGIN;
  const roomHeight = area.bottom - area.top - 2 * MARGIN;
  if (width <= 0 || height <= 0 || roomWidth <= 0 || roomHeight <= 0) return null;

  const { x, y, zoom } = viewport;
  const nextZoom = zoom * Math.min(1, roomWidth / width, roomHeight / height);
  if (nextZoom < Math.min(zoom, MIN_ZOOM)) return viewportToReveal(node, pane, coverTop, viewport);

  const middleX = ((all.left + all.right) / 2 - pane.left - x) / zoom;
  const middleY = ((all.top + all.bottom) / 2 - pane.top - y) / zoom;
  const targetX = (area.left + area.right) / 2 - pane.left;
  const targetY = (area.top + area.bottom) / 2 - pane.top;
  return { x: targetX - middleX * nextZoom, y: targetY - middleY * nextZoom, zoom: nextZoom };
}

/**
 * The viewport that shows the whole drawing (the node and every other step,
 * `others`) in full above `coverTop`: null when it all shows already, and
 * undefined when it would need a zoom below WHOLE_MIN_ZOOM (or below the
 * user's own, when they are zoomed out further), so the caller shows less.
 */
export function viewportToShowWhole(
  node: ScreenBox,
  others: ScreenBox[],
  pane: ScreenBox,
  coverTop: number,
  viewport: CanvasViewport,
): CanvasViewport | null | undefined {
  const all = around([node, ...others]);
  const area = {
    top: pane.top,
    bottom: Math.min(pane.bottom, coverTop),
    left: pane.left,
    right: pane.right,
  };
  const inView =
    all.top >= area.top + MARGIN &&
    all.bottom <= area.bottom - MARGIN &&
    all.left >= area.left + MARGIN &&
    all.right <= area.right - MARGIN;
  if (inView) return null;

  const width = all.right - all.left;
  const height = all.bottom - all.top;
  const roomWidth = area.right - area.left - 2 * MARGIN;
  const roomHeight = area.bottom - area.top - 2 * MARGIN;
  if (width <= 0 || height <= 0 || roomWidth <= 0 || roomHeight <= 0) return undefined;

  const { x, y, zoom } = viewport;
  const nextZoom = zoom * Math.min(1, roomWidth / width, roomHeight / height);
  if (nextZoom < Math.min(zoom, WHOLE_MIN_ZOOM)) return undefined;

  const middleX = ((all.left + all.right) / 2 - pane.left - x) / zoom;
  const middleY = ((all.top + all.bottom) / 2 - pane.top - y) / zoom;
  const targetX = (area.left + area.right) / 2 - pane.left;
  const targetY = (area.top + area.bottom) / 2 - pane.top;
  return { x: targetX - middleX * nextZoom, y: targetY - middleY * nextZoom, zoom: nextZoom };
}

/** The steps wired straight into and out of this one. */
function wiredTo(nodeId: string): string[] {
  const ids = new Set<string>();
  for (const edge of useWorkflowStore.getState().edges || []) {
    if (edge.source === nodeId) ids.add(edge.target);
    if (edge.target === nodeId) ids.add(edge.source);
  }
  ids.delete(nodeId);
  return [...ids];
}

/** The node as people see it: its square plus the name drawn under it. */
function nodeBox(element: HTMLElement): ScreenBox {
  const boxes: ScreenBox[] = [element.getBoundingClientRect()];
  const caption = element.querySelector<HTMLElement>('[data-node-caption]');
  if (caption) boxes.push(caption.getBoundingClientRect());
  return {
    top: Math.min(...boxes.map((b) => b.top)),
    bottom: Math.max(...boxes.map((b) => b.bottom)),
    left: Math.min(...boxes.map((b) => b.left)),
    right: Math.max(...boxes.map((b) => b.right)),
  };
}

function nodeElement(nodeId: string): HTMLElement | null {
  const id = typeof CSS !== 'undefined' && typeof CSS.escape === 'function' ? CSS.escape(nodeId) : nodeId.replace(/["\\]/g, '\\$&');
  return document.querySelector<HTMLElement>(`.react-flow__node[data-id="${id}"]`);
}

function glideMs(): number {
  const still =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  return still ? 0 : GLIDE_MS;
}

// Where the canvas was before a reveal moved it while the dock was open, and
// where the last reveal put it: what closing the dock glides back from.
let parked: { before: CanvasViewport; set: CanvasViewport } | null = null;

/**
 * Glides the canvas so the node with this id shows in full: above `coverTop`
 * when something covers the lower part of the canvas, with the whole drawing
 * when it fits and else with the steps wired to it; without a cover, on its
 * own anywhere on the canvas. Returns whether the canvas moved.
 */
export function revealNode(nodeId: string, coverTop: number = Number.POSITIVE_INFINITY): boolean {
  const flow = useWorkflowStore.getState().reactFlowInstance;
  if (!flow || typeof document === 'undefined') return false;
  const element = nodeElement(nodeId);
  const pane = element?.closest<HTMLElement>('.react-flow');
  if (!element || !pane) return false;
  const now = flow.getViewport();
  const covered = Number.isFinite(coverTop);
  const paneBox = pane.getBoundingClientRect();
  let next: CanvasViewport | null;
  if (covered) {
    const others = Array.from(pane.querySelectorAll<HTMLElement>('.react-flow__node')).filter((e) => e !== element).map(nodeBox);
    const whole = viewportToShowWhole(nodeBox(element), others, paneBox, coverTop, now);
    next = whole !== undefined
      ? whole
      : viewportToShowFlow(
          nodeBox(element),
          wiredTo(nodeId).map(nodeElement).filter((e): e is HTMLElement => !!e).map(nodeBox),
          paneBox,
          coverTop,
          now,
        );
  } else {
    next = viewportToReveal(nodeBox(element), paneBox, coverTop, now);
  }
  if (!next) return false;
  if (covered) parked = { before: parked?.before ?? now, set: next };
  flow.setViewport(next, { duration: glideMs() });
  return true;
}

/**
 * Once the dock has closed: glides back to the view from before the dock
 * moved it, so opening a step and closing it again leaves the drawing as it
 * was. Not when the user zoomed or panned meanwhile. Returns whether the
 * canvas moved.
 */
export function returnFromReveal(): boolean {
  const flow = useWorkflowStore.getState().reactFlowInstance;
  const last = parked;
  parked = null;
  if (!flow || !last) return false;
  const now = flow.getViewport();
  if (Math.abs(now.zoom - last.set.zoom) > 0.001 || Math.abs(now.y - last.set.y) > 1) return false;
  // The side rails coming back shifted the view sideways by this much, to
  // keep the drawing still on screen; the view from before is in the same
  // place once shifted the same way.
  const shift = now.x - last.set.x;
  flow.setViewport({ x: last.before.x + shift, y: last.before.y, zoom: last.before.zoom }, { duration: glideMs() });
  return true;
}

// ── The canvas holds still while the settings dock is open ──
// Opening a node's settings folds the side rails away to give the dock room,
// and closing them brings the rails back. Both resize the canvas, and on a
// resize the canvas refits the whole drawing (Canvas.tsx). That refit moved
// every node on each open and close, and put the opened node back behind the
// dock. While the dock is open, and for a moment after it closes, the canvas
// keeps the drawing where it is on screen instead.

/** How long after the dock closes the canvas still holds still. */
const HOLD_AFTER_CLOSE_MS = 700;
let dockOpen = false;
let heldUntil = 0;
const listeners = new Set<() => void>();

/** Called by the settings dock as it opens and closes. */
export function setSettingsDockOpen(open: boolean): void {
  // A dock opening afresh starts from the canvas as it is now.
  if (open && !dockOpen) parked = null;
  dockOpen = open;
  heldUntil = Date.now() + HOLD_AFTER_CLOSE_MS;
  listeners.forEach((listener) => listener());
}

/** Whether the canvas may refit the whole drawing after it resizes. */
export function canvasMayRefit(): boolean {
  return !dockOpen && Date.now() >= heldUntil;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/**
 * Whether a node's settings are open. The canvas's corner controls and its
 * bottom toolbar step aside meanwhile: the dock covers them, and on a short
 * window it would cut them in half.
 */
export function useSettingsDockOpen(): boolean {
  return useSyncExternalStore(subscribe, () => dockOpen, () => false);
}
