/**
 * Opening a node's settings must not leave the node half behind the
 * settings panel at the bottom of the canvas. The canvas moves only when
 * part of the node is hidden, and then puts it in the middle of the strip
 * above the panel.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  canvasMayRefit,
  returnFromReveal,
  revealNode,
  setSettingsDockOpen,
  useSettingsDockOpen,
  viewportToReveal,
  viewportToShowFlow,
  viewportToShowWhole,
  WHOLE_MIN_ZOOM,
  type CanvasViewport,
  type ScreenBox,
} from '../components/editor/revealNode';
import { useWorkflowStore } from '../stores/workflowStore';

// The canvas: 1200 x 800 pixels from (40, 260); the panel covers it from 540 down.
const pane = { top: 260, bottom: 1060, left: 40, right: 1240 };
const panelTop = 540;
const at = { x: 0, y: 0, zoom: 1 };

/** A node of 140 x 180 pixels (square and name) with its top-left at (left, top). */
const node = (left: number, top: number) => ({ top, bottom: top + 180, left, right: left + 140 });

describe('bringing an opened node into full view', () => {
  it('leaves a node that is already in full view where it is', () => {
    expect(viewportToReveal(node(500, 300), pane, panelTop, at)).toBeNull();
  });

  it('moves a node half behind the panel to the middle of the strip above it', () => {
    const next = viewportToReveal(node(530, 470), pane, panelTop, at);
    expect(next).not.toBeNull();
    expect(next!.zoom).toBe(1);
    // The node's middle (600, 560) lands on the strip's middle (640, 400).
    expect(next!.x).toBeCloseTo(40);
    expect(next!.y).toBeCloseTo(-160);
  });

  it('brings back a node beyond the canvas edge too', () => {
    const next = viewportToReveal(node(1200, 300), pane, panelTop, at);
    expect(next).not.toBeNull();
    expect(next!.x).toBeLessThan(0);
  });

  it('zooms out only as far as the node needs to fit above the panel', () => {
    // A strip 140 pixels high leaves 100 for a 180-pixel node.
    const short = viewportToReveal(node(500, 300), pane, 400, at);
    expect(short!.zoom).toBeCloseTo(100 / 180);
    // Zoomed in to 2, a node 300 pixels high fits the 240 left at 1.6.
    const big = viewportToReveal(
      { top: 300, bottom: 600, left: 500, right: 600 },
      pane,
      panelTop,
      { x: 0, y: 0, zoom: 2 },
    );
    expect(big!.zoom).toBeCloseTo(1.6);
  });

  it('does not zoom out below half size', () => {
    // A strip 100 pixels high leaves 60: the node would need a third of its size.
    expect(viewportToReveal(node(500, 300), pane, 360, at)!.zoom).toBe(0.5);
  });
});

// A Reconcile step at (560, 330) with its two inputs above and below it and
// the table it writes to its right: the inputs reach beyond the strip.
const reconcile = node(560, 330);
const payments = node(300, 200);
const bank = node(300, 470);
const sink = node(820, 330);
const inStrip = (b: ScreenBox, v: CanvasViewport, from: CanvasViewport, top = panelTop) => {
  // Where a box drawn at viewport `from` lands at viewport `v`.
  const k = v.zoom / from.zoom;
  const map = (sx: number, sy: number) => [
    pane.left + v.x + (sx - pane.left - from.x) * k,
    pane.top + v.y + (sy - pane.top - from.y) * k,
  ];
  const [l, t] = map(b.left, b.top);
  const [r, bo] = map(b.right, b.bottom);
  return t >= pane.top && bo <= top && l >= pane.left && r <= pane.right;
};

describe('showing the opened step with the steps wired to it', () => {
  it('brings the inputs that were cut off into view as well, zooming out only as needed', () => {
    const next = viewportToShowFlow(reconcile, [payments, bank, sink], pane, panelTop, at);
    expect(next).not.toBeNull();
    // 450 pixels of steps in a strip of 280 with its margins: about 0.53.
    expect(next!.zoom).toBeCloseTo(240 / 450);
    for (const b of [reconcile, payments, bank, sink]) expect(inStrip(b, next!, at)).toBe(true);
  });

  it('leaves the canvas still when the step and its neighbours already show', () => {
    expect(viewportToShowFlow(node(500, 300), [node(300, 300), node(760, 300)], pane, panelTop + 300, at)).toBeNull();
  });

  it('falls back to the step alone when its neighbours cannot fit at half size', () => {
    const far = node(300, 1600);
    expect(viewportToShowFlow(reconcile, [far], pane, panelTop, at)).toEqual(viewportToReveal(reconcile, pane, panelTop, at));
  });
});

// A Deduplicate step on the second row, wired to the customers and the join
// beside it; the orders and their filter on the row above are cut off at the
// top of the canvas, though nothing is wired from them to the opened step.
const dedupe = node(560, 330);
const orders = node(300, 150);
const filter = node(560, 150);
const customers = node(300, 330);
const join = node(820, 240);

describe('showing the whole drawing while a step is open', () => {
  it('brings in the steps cut off at the edge, wired to the opened one or not, when they fit at a readable size', () => {
    const next = viewportToShowWhole(dedupe, [orders, filter, customers, join], pane, panelTop, at);
    expect(next).toBeTruthy();
    // 360 pixels of drawing in a strip of 280 with its margins.
    expect(next!.zoom).toBeCloseTo(240 / 360);
    expect(next!.zoom).toBeGreaterThanOrEqual(WHOLE_MIN_ZOOM);
    for (const b of [dedupe, orders, filter, customers, join]) expect(inStrip(b, next!, at)).toBe(true);
  });

  it('leaves the canvas still when the whole drawing already shows', () => {
    expect(viewportToShowWhole(node(500, 300), [node(300, 300), node(760, 300)], pane, panelTop + 300, at)).toBeNull();
  });

  it('gives way when the whole drawing would be too small to read', () => {
    // 600 pixels of drawing would need 0.4.
    expect(viewportToShowWhole(dedupe, [node(300, -90)], pane, panelTop, at)).toBeUndefined();
  });

  it('shows a two-row pipeline whole even when that takes it down to about 0.6', () => {
    // 400 pixels of drawing in the 240 the strip leaves: 0.6.
    const next = viewportToShowWhole(dedupe, [node(300, 110), node(820, 240)], pane, panelTop, at);
    expect(next).toBeTruthy();
    expect(next!.zoom).toBeCloseTo(0.6);
  });
});

describe('opening a step and closing it again', () => {
  const rect = (b: ScreenBox) => ({ ...b, x: b.left, y: b.top, width: b.right - b.left, height: b.bottom - b.top, toJSON: () => b }) as DOMRect;
  let view: CanvasViewport;

  function mount(boxes: Record<string, ScreenBox>) {
    document.body.innerHTML = '';
    const flow = document.createElement('div');
    flow.className = 'react-flow';
    flow.getBoundingClientRect = () => rect(pane);
    for (const [id, b] of Object.entries(boxes)) {
      const el = document.createElement('div');
      el.className = 'react-flow__node';
      el.dataset.id = id;
      el.getBoundingClientRect = () => rect(b);
      flow.appendChild(el);
    }
    document.body.appendChild(flow);
    view = { ...at };
    useWorkflowStore.setState({
      reactFlowInstance: { getViewport: () => view, setViewport: (v: CanvasViewport) => { view = v; } },
      edges: [
        { id: 'e1', source: 'pay', target: 'rec' },
        { id: 'e2', source: 'bank', target: 'rec' },
        { id: 'e3', source: 'rec', target: 'sink' },
      ],
    } as any);
  }

  afterEach(() => { setSettingsDockOpen(false); document.body.innerHTML = ''; });

  it('shows the flow around the step while its settings are open, then glides back', () => {
    mount({ rec: reconcile, pay: payments, bank, sink });
    setSettingsDockOpen(true);
    expect(revealNode('rec', panelTop)).toBe(true);
    expect(view.zoom).toBeLessThan(1);
    // Closing: the side rails come back and shift the view 290 pixels to keep the drawing still.
    view = { ...view, x: view.x + 290 };
    setSettingsDockOpen(false);
    expect(returnFromReveal()).toBe(true);
    expect(view.x).toBeCloseTo(290);
    expect(view.y).toBeCloseTo(0);
    expect(view.zoom).toBe(1);
  });

  it('shows the whole drawing when it fits, the steps it is wired to when it does not', () => {
    // Two unwired steps on a row above, cut off at the top: all five fit at about 0.67.
    mount({ rec: dedupe, pay: customers, sink: join, top1: orders, top2: filter });
    setSettingsDockOpen(true);
    expect(revealNode('rec', panelTop)).toBe(true);
    expect(view.zoom).toBeCloseTo(240 / 360);
    // A step far below would make the whole drawing unreadable: only the wired ones show.
    mount({ rec: reconcile, pay: payments, bank, sink, far: node(300, 1600) });
    setSettingsDockOpen(true);
    revealNode('rec', panelTop);
    expect(view).toEqual(viewportToShowFlow(reconcile, [payments, bank, sink], pane, panelTop, at));
  });

  it('stays where the user put it when they zoomed while the settings were open', () => {
    mount({ rec: reconcile, pay: payments, bank, sink });
    setSettingsDockOpen(true);
    revealNode('rec', panelTop);
    view = { ...view, zoom: view.zoom * 1.2 };
    const zoomed = view;
    setSettingsDockOpen(false);
    expect(returnFromReveal()).toBe(false);
    expect(view).toBe(zoomed);
  });
});

describe('the canvas while the settings dock is open', () => {
  afterEach(() => { vi.useRealTimers(); });

  it('does not refit the drawing while the dock is open, nor just after it closes', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-10T12:00:00Z'));
    setSettingsDockOpen(true);
    expect(canvasMayRefit()).toBe(false);
    vi.advanceTimersByTime(5000);
    expect(canvasMayRefit()).toBe(false); // still open

    setSettingsDockOpen(false);
    expect(canvasMayRefit()).toBe(false); // the side rails are coming back
    vi.advanceTimersByTime(700);
    expect(canvasMayRefit()).toBe(true);
  });

  it('tells the canvas when to put its corner controls aside', () => {
    const { result } = renderHook(() => useSettingsDockOpen());
    expect(result.current).toBe(false);
    act(() => { setSettingsDockOpen(true); });
    expect(result.current).toBe(true);
    act(() => { setSettingsDockOpen(false); });
    expect(result.current).toBe(false);
  });
});
