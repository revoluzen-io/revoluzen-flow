/**
 * The editor's live-progress socket (hooks/useExecutionWS.ts). It connects to
 * the address the app was loaded from (it used to dial port 8001, which a
 * server started on another port or behind a proxy does not answer), speaks
 * `action` (it sent `command`, which the server answered with "Unknown
 * action"), follows each run of the pipeline from its start, and lets the
 * pipeline's own results show again shortly after a run ends.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useExecutionWS } from '../hooks/useExecutionWS';

class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((msg: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }

  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  message(body: unknown) {
    this.onmessage?.({ data: JSON.stringify(body) });
  }
}

const last = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

function event(type: string, stepId?: string, extra: Record<string, unknown> = {}) {
  return { type, workflow_id: 'wf-1', timestamp: '2026-11-13T10:00:00Z', step_id: stepId, ...extra };
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeWebSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeWebSocket);
  localStorage.setItem('revoluzen_flow_token', 'tok-1');
  localStorage.setItem('revoluzen_flow_workspace_id', 'ws-1');
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe('useExecutionWS', () => {
  it('connects to the address the app came from', () => {
    renderHook(() => useExecutionWS('wf-1'));
    const url = new URL(last().url);
    expect(url.host).toBe(window.location.host);
    expect(url.pathname).toBe('/ws/execution/wf-1');
    expect(url.searchParams.get('token')).toBe('tok-1');
    expect(url.searchParams.get('workspace_id')).toBe('ws-1');
  });

  it('does not connect before the pipeline is saved', () => {
    renderHook(() => useExecutionWS(null));
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('sends actions the server understands', () => {
    const { result } = renderHook(() => useExecutionWS('wf-1'));
    act(() => last().open());
    act(() => result.current.execute());
    act(() => result.current.cancel());
    expect(last().sent.map((s) => JSON.parse(s))).toEqual([
      { action: 'execute' },
      { action: 'cancel' },
    ]);
  });

  it('follows a run step by step and starts clean on the next one', () => {
    const { result } = renderHook(() => useExecutionWS('wf-1'));
    const ws = last();
    act(() => ws.open());
    act(() => {
      ws.message(event('workflow_started'));
      ws.message(event('step_started', 'read'));
      ws.message({ type: 'pong', workflow_id: 'wf-1' });
    });
    expect(result.current.isExecuting).toBe(true);
    expect(result.current.stepStates.read.type).toBe('step_started');
    expect(result.current.events.map((e) => e.type)).toEqual(['workflow_started', 'step_started']);

    act(() => {
      ws.message(event('step_error', 'read', { error: 'no such file' }));
      ws.message(event('workflow_completed', undefined, { status: 'error' }));
    });
    expect(result.current.isExecuting).toBe(false);
    expect(result.current.stepStates.read.type).toBe('step_error');

    // The next run (someone else's Run, a published endpoint) replaces the last one's states.
    act(() => {
      ws.message(event('workflow_started'));
      ws.message(event('step_started', 'write'));
    });
    expect(Object.keys(result.current.stepStates)).toEqual(['write']);
  });

  it("lets the pipeline's own results show again shortly after a run ends", () => {
    const { result } = renderHook(() => useExecutionWS('wf-1'));
    const ws = last();
    act(() => ws.open());
    act(() => {
      ws.message(event('workflow_started'));
      ws.message(event('step_completed', 'read'));
      ws.message(event('workflow_completed', undefined, { status: 'success' }));
    });
    expect(result.current.stepStates.read.type).toBe('step_completed');
    act(() => vi.advanceTimersByTime(3900));
    expect(result.current.stepStates.read).toBeDefined();
    act(() => vi.advanceTimersByTime(200));
    expect(result.current.stepStates).toEqual({});
  });

  it('ignores a refusal, which is not run progress', () => {
    const { result } = renderHook(() => useExecutionWS('wf-1'));
    const ws = last();
    act(() => ws.open());
    act(() => ws.message({ type: 'error', error: 'Workflow not found' }));
    expect(result.current.events).toEqual([]);
    expect(result.current.isExecuting).toBe(false);
  });

  it('keeps an idle socket open with a ping', () => {
    renderHook(() => useExecutionWS('wf-1'));
    const ws = last();
    act(() => ws.open());
    act(() => vi.advanceTimersByTime(25_000));
    expect(ws.sent.map((s) => JSON.parse(s))).toEqual([{ action: 'ping' }]);
  });

  it('does not reconnect a socket it closed on purpose', () => {
    const { unmount } = renderHook(() => useExecutionWS('wf-1'));
    act(() => last().open());
    unmount();
    act(() => vi.advanceTimersByTime(60_000));
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('reconnects a socket the server dropped', () => {
    renderHook(() => useExecutionWS('wf-1'));
    act(() => last().open());
    act(() => last().close());
    act(() => vi.advanceTimersByTime(1_000));
    expect(FakeWebSocket.instances).toHaveLength(2);
  });
});
