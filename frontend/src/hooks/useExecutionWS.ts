import { useState, useEffect, useRef, useCallback } from 'react';

export interface ExecutionEvent {
  type: 'workflow_started' | 'step_started' | 'step_completed' | 'step_error' | 'workflow_completed';
  timestamp: string;
  workflow_id: string;
  step_id?: string;
  step_type?: string;
  row_count?: number;
  duration_ms?: number;
  error?: string;
  status?: string;
  progress?: number;
}

interface UseExecutionWSReturn {
  events: ExecutionEvent[];
  connected: boolean;
  execute: () => void;
  executeStep: (stepId: string) => void;
  cancel: () => void;
  lastEvent: ExecutionEvent | null;
  /** Map of step_id -> latest event for that step */
  stepStates: Record<string, ExecutionEvent>;
  /** Whether the workflow is currently running (between workflow_started and workflow_completed) */
  isExecuting: boolean;
  clearEvents: () => void;
}

/** The messages that are run progress; the socket also answers pings and actions. */
const PROGRESS_TYPES: ReadonlySet<string> = new Set([
  'workflow_started', 'step_started', 'step_completed', 'step_error', 'workflow_completed',
]);

/** How long a finished run's step states stay on the canvas; then the
 *  pipeline's own run results show again. */
const CLEAR_AFTER_MS = 4000;

/** A ping this often keeps proxies from closing an idle socket. */
const KEEPALIVE_MS = 25000;

type TimeoutRef = { current: ReturnType<typeof setTimeout> | null };
type IntervalRef = { current: ReturnType<typeof setInterval> | null };

function stopTimeout(ref: TimeoutRef): void {
  if (ref.current) {
    clearTimeout(ref.current);
    ref.current = null;
  }
}

function stopInterval(ref: IntervalRef): void {
  if (ref.current) {
    clearInterval(ref.current);
    ref.current = null;
  }
}

/**
 * WebSocket hook for real-time workflow execution tracking.
 * Connects to /ws/execution/{workflowId} on the address the app was loaded
 * from and streams the progress of the pipeline's runs: the Run button's,
 * a published endpoint's and those started over this socket.
 * Auto-reconnects on disconnect with exponential backoff.
 */
export function useExecutionWS(workflowId: string | null): UseExecutionWSReturn {
  const [events, setEvents] = useState<ExecutionEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const keepAliveTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const clearTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttempt = useRef(0);
  const maxReconnectAttempts = 10;

  // Derived state
  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const stepStates: Record<string, ExecutionEvent> = {};
  for (const evt of events) {
    if (evt.step_id) {
      stepStates[evt.step_id] = evt;
    }
  }

  const clearEvents = useCallback(() => {
    stopTimeout(clearTimer);
    setEvents([]);
    setIsExecuting(false);
  }, []);

  const connect = useCallback(() => {
    if (!workflowId) return;

    // Clean up existing connection
    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.close();
      wsRef.current = null;
    }
    stopInterval(keepAliveTimer);

    // Same address as the page: the server serves the app, the API and this
    // socket together, and the dev server proxies /ws. A fixed port 8001
    // missed every server started on another port or behind a proxy.
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

    // Browsers can't set custom headers on `new WebSocket()`, so the
    // backend expects auth + workspace as query params. We read the
    // same localStorage keys the REST client uses, so the WS runs in
    // exactly the same tenant as the rest of the app.
    const token = localStorage.getItem('revoluzen_flow_token') || '';
    const workspaceId = localStorage.getItem('revoluzen_flow_workspace_id') || 'default';
    const qs = new URLSearchParams({
      token,
      workspace_id: workspaceId,
    }).toString();
    const url = `${protocol}//${window.location.host}/ws/execution/${encodeURIComponent(workflowId)}?${qs}`;

    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        reconnectAttempt.current = 0;
        stopInterval(keepAliveTimer);
        keepAliveTimer.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ action: 'ping' }));
          }
        }, KEEPALIVE_MS);
      };

      ws.onmessage = (msg) => {
        let event: ExecutionEvent;
        try {
          event = JSON.parse(msg.data);
        } catch {
          return; // Ignore malformed messages
        }
        // Pongs, acknowledgements and errors are not run progress.
        if (!event || !PROGRESS_TYPES.has(event.type)) return;
        stopTimeout(clearTimer);

        if (event.type === 'workflow_started') {
          // A new run: the previous run's step states no longer apply.
          setEvents([event]);
          setIsExecuting(true);
          return;
        }
        setEvents(prev => [...prev, event]);
        if (event.type === 'workflow_completed' || (event.type === 'step_error' && !event.step_id)) {
          setIsExecuting(false);
        }
        if (event.type === 'workflow_completed') {
          // Leave the outcome up for a moment, then let the pipeline's own
          // results show again (a later Test Node must not be painted over).
          clearTimer.current = setTimeout(() => {
            clearTimer.current = null;
            setEvents([]);
          }, CLEAR_AFTER_MS);
        }
      };

      ws.onclose = () => {
        setConnected(false);
        stopInterval(keepAliveTimer);
        wsRef.current = null;

        // Auto-reconnect with exponential backoff
        if (reconnectAttempt.current < maxReconnectAttempts) {
          const delay = Math.min(1000 * Math.pow(2, reconnectAttempt.current), 30000);
          reconnectAttempt.current += 1;
          reconnectTimer.current = setTimeout(connect, delay);
        }
      };

      ws.onerror = () => {
        // onclose will fire after onerror, handling reconnect
      };
    } catch {
      // WebSocket construction failed (e.g., invalid URL)
      setConnected(false);
    }
  }, [workflowId]);

  // Connect when workflowId changes
  useEffect(() => {
    connect();

    return () => {
      stopTimeout(reconnectTimer);
      stopInterval(keepAliveTimer);
      stopTimeout(clearTimer);
      if (wsRef.current) {
        // No reconnect for a socket closed on purpose.
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
      setEvents([]);
      setIsExecuting(false);
      setConnected(false);
    };
  }, [connect]);

  // The server reads `action` (api/websocket.py); this sent `command`,
  // which it answered with "Unknown action".
  const sendAction = useCallback((action: string, payload?: Record<string, unknown>) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action, ...payload }));
    }
  }, []);

  const execute = useCallback(() => {
    stopTimeout(clearTimer);
    setEvents([]);
    setIsExecuting(true);
    sendAction('execute');
  }, [sendAction]);

  const executeStep = useCallback((stepId: string) => {
    sendAction('execute_step', { step_id: stepId });
  }, [sendAction]);

  const cancel = useCallback(() => {
    sendAction('cancel');
    setIsExecuting(false);
  }, [sendAction]);

  return {
    events,
    connected,
    execute,
    executeStep,
    cancel,
    lastEvent,
    stepStates,
    isExecuting,
    clearEvents,
  };
}
