import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ActivityPage from '../components/pages/ActivityPage';
import AIPage from '../components/pages/AIPage';
import { api } from '../api/client';
import { DEFAULT_RATES } from '../lib/cost';
import {
  AI_ACTIVITY_URL,
  AI_CSV_HEADERS,
  aiActivityCsv,
  aiResult,
  failureText,
  outcomeLabel,
  sourceSplit,
  summaryFromEvents,
  tokensOf,
  usageTotals,
  whereOf,
  type ActivityEvent,
  type AiActivityResponse,
} from '../lib/aiActivity';
import { useCurrentPageContext, usePageContext, _resetPageContextForTests } from '../hooks/usePageContext';

/**
 * Insights → Activity shows what the AI did — every AI request, from the
 * Copilot or another feature, with where it came from, the model, tokens,
 * cost and failures — and not pipeline runs, whose history is on
 * Workflows → Executions. There is no "who asked": this edition has one
 * user.
 */

function aiEvent(over: Partial<ActivityEvent> & { details?: Record<string, unknown> } = {}): ActivityEvent {
  return {
    kind: 'agent',
    timestamp: '2026-11-06T09:00:00+00:00',
    actor: 'u-ana',
    subject: 'run-1',
    summary: 'Why did the orders pipeline fail?',
    severity: 'info',
    ...over,
    details: {
      user_intent: 'Why did the orders pipeline fail?',
      model: 'gpt-4o-mini',
      provider: 'openai',
      tokens_in: 1000,
      tokens_out: 200,
      tokens: 1200,
      elapsed_ms: 2100,
      step_count: 3,
      page: 'executions.list',
      outcome: 'success',
      failure_reason: null,
      ...(over.details || {}),
    },
  };
}

const failed = aiEvent({
  subject: 'run-2',
  summary: 'Draft a pipeline from orders.csv',
  severity: 'error',
  details: {
    user_intent: 'Draft a pipeline from orders.csv',
    tokens_in: 0,
    tokens_out: 0,
    tokens: 0,
    outcome: 'llm_failure',
    failure_reason: 'llm_call:HTTPStatusError: 401 Unauthorized',
  },
});

// AI calls from other features: the Editor's drafting, and an automatic
// notification summary no person asked for.
const editorDraft = aiEvent({
  subject: 'run-3',
  actor: 'u-bo',
  summary: 'Load sales.csv and total by region',
  details: {
    source: 'planner.ai_generate_pipeline',
    feature: 'Draft a pipeline',
    user_intent: 'Load sales.csv and total by region',
    page: 'editor',
    tokens_in: 900,
    tokens_out: 300,
    tokens: 1200,
  },
});

const notification = aiEvent({
  subject: 'run-4',
  actor: 'anonymous',
  summary: 'Write a notification',
  details: {
    source: 'notifications.summary',
    feature: 'Write a notification',
    user_intent: 'Write a notification',
    page: 'notifications',
    provider: 'ollama',
    model: 'qwen2.5:7b',
    tokens_in: 80,
    tokens_out: 12,
    tokens: 92,
  },
});

// What an older backend that ignores `scope` would add: a pipeline run.
const pipelineRun: ActivityEvent = {
  kind: 'execution',
  timestamp: '2026-11-06T08:00:00+00:00',
  actor: 'schedule',
  subject: 'wf-orders',
  summary: "Pipeline 'Orders' error in 1200ms",
  severity: 'error',
  details: { execution_id: 'ex-1' },
};

function response(events: ActivityEvent[], over: Partial<AiActivityResponse> = {}): AiActivityResponse {
  return {
    events,
    count: events.length,
    kind_counts: { audit: 0, agent: events.filter((e) => e.kind === 'agent').length, execution: 0 },
    severity_counts: { info: 0, warning: 0, error: 0 },
    filter: { scope: 'ai' },
    summary: {
      requests: 12,
      tokens_in: 12000,
      tokens_out: 2400,
      failed: 1,
      blocked: 0,
      by_model: [{ provider: 'openai', model: 'gpt-4o-mini', requests: 12, tokens_in: 12000, tokens_out: 2400 }],
    },
    ...over,
  };
}

function mockApi(body: AiActivityResponse) {
  return vi.spyOn(api, 'get').mockImplementation(async (path: string) => {
    if (path === AI_ACTIVITY_URL) return body as any;
    if (path === '/api/v1/ai/cost-rates') return { rates: DEFAULT_RATES } as any;
    if (path.startsWith('/api/ai/agent/trace/')) {
      return { run_id: 'run-2', outcome: 'llm_failure', iterations: 1, total_tokens_in: 0, total_tokens_out: 0, elapsed_ms: 900, steps: [] } as any;
    }
    throw new Error(`unexpected GET ${path}`);
  });
}

/**
 * Shows the context the Copilot would read, so tests can check it. Render it
 * BEFORE the page: effects run in tree order, so it subscribes before the
 * page publishes on mount.
 */
function ContextProbe() {
  const ctx = useCurrentPageContext();
  return <pre data-testid="ctx">{JSON.stringify(ctx)}</pre>;
}

function publishedContext() {
  return JSON.parse(screen.getByTestId('ctx').textContent || 'null');
}

beforeEach(() => {
  _resetPageContextForTests();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.location.hash = '';
});

describe('AI activity helpers', () => {
  it('groups outcomes into answered, failed and blocked', () => {
    expect(aiResult('success')).toBe('answered');
    expect(aiResult(undefined)).toBe('answered');
    expect(aiResult('llm_failure')).toBe('failed');
    expect(aiResult('timeout')).toBe('failed');
    expect(aiResult('tool_failure')).toBe('failed');
    expect(aiResult('policy_block')).toBe('blocked');
    expect(outcomeLabel('llm_failure')).toBe('Model error');
    expect(outcomeLabel('timeout')).toBe('Took too long');
    expect(outcomeLabel('policy_block')).toBe('Blocked by a limit');
  });

  it('prices the window per model and calls an all-Ollama window free', () => {
    const t = usageTotals(response([]).summary, DEFAULT_RATES);
    // gpt-4o-mini: 0.15 in / 0.60 out per million tokens.
    expect(t.costUsd).toBeCloseTo((12000 * 0.15 + 2400 * 0.6) / 1_000_000, 10);
    expect(t.allLocal).toBe(false);

    const local = usageTotals(
      { requests: 2, tokens_in: 500, tokens_out: 50, failed: 0, blocked: 0,
        by_model: [{ provider: 'ollama', model: 'qwen2.5:7b', requests: 2, tokens_in: 500, tokens_out: 50 }] },
      DEFAULT_RATES,
    );
    expect(local.allLocal).toBe(true);
    expect(local.costUsd).toBe(0);
  });

  it('says where each request came from', () => {
    expect(whereOf(aiEvent())).toBe('Copilot');
    expect(whereOf(editorDraft)).toBe('Editor · Draft a pipeline');
    // The feature's name is already the request's text, so only the page.
    expect(whereOf(notification)).toBe('Notifications');
    const tool = aiEvent({ details: { source: 'copilot.draft_pipeline', feature: 'Draft a pipeline', page: 'copilot', user_intent: 'orders by day' } });
    expect(whereOf(tool)).toBe('Copilot · Draft a pipeline');
    expect(sourceSplit({
      requests: 7, tokens_in: 0, tokens_out: 0, failed: 0, blocked: 0, by_model: [],
      by_source: [
        { source: 'copilot', requests: 4 },
        { source: 'copilot.draft_pipeline', requests: 1 },
        { source: 'notifications.summary', requests: 2 },
      ],
    })).toEqual({ copilot: 5, other: 2 });
  });

  it('drops the trace tag from a failure reason', () => {
    expect(failureText('llm_call:ReadTimeout: provider did not answer')).toBe('ReadTimeout: provider did not answer');
    expect(failureText('Daily token cap reached')).toBe('Daily token cap reached');
    expect(failureText(null)).toBe('');
  });

  it('works out totals from the rows when a backend sends none', () => {
    const legacy = aiEvent({ subject: 'run-9', details: { tokens_in: 0, tokens_out: 0, tokens: 500, outcome: 'timeout' } });
    const s = summaryFromEvents([aiEvent(), failed, legacy]);
    expect(s.requests).toBe(3);
    expect(s.failed).toBe(2);
    expect(s.tokens_in).toBe(1000 + 500);
    expect(s.tokens_out).toBe(200);
    expect(s.by_model).toHaveLength(1);
    expect(s.by_model[0]).toMatchObject({ provider: 'openai', model: 'gpt-4o-mini', requests: 3 });
  });

  it('falls back to the combined token count for older rows', () => {
    const legacy = aiEvent({ details: { tokens_in: 0, tokens_out: 0, tokens: 640 } });
    expect(tokensOf(legacy).total).toBe(640);
  });

  it('exports one CSV line per request and keeps typed text from running as a formula', () => {
    const risky = aiEvent({ subject: 'run-3', details: { user_intent: '=HYPERLINK("http://x","y")' } });
    const comma = aiEvent({ subject: 'run-4', details: { user_intent: 'Total, by region' } });
    const csv = aiActivityCsv([aiEvent(), risky, comma], DEFAULT_RATES).split('\n');

    expect(csv[0]).toBe(AI_CSV_HEADERS.join(','));
    expect(csv[0]).not.toContain('kind');
    expect(csv[0]).not.toContain('asked_by');
    expect(csv).toHaveLength(4);
    expect(csv[1].split(',')[AI_CSV_HEADERS.indexOf('where')]).toBe('Copilot');
    expect(csv[1]).toContain('Answered');
    expect(csv[1]).toContain('0.000270'); // 1000 × 0.15 + 200 × 0.60, per million
    expect(csv[2]).toContain('"\'=HYPERLINK(""http://x"",""y"")"');
    expect(csv[3]).toContain('"Total, by region"');
  });
});

describe('ActivityPage', () => {
  it('asks for AI usage only and leaves pipeline runs out', async () => {
    const get = mockApi(response([aiEvent(), failed, pipelineRun]));
    render(<ActivityPage embedded />);

    expect(await screen.findByText('Why did the orders pipeline fail?')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/api/v1/activity?scope=ai&limit=200');
    expect(screen.queryByText(/Pipeline 'Orders'/)).toBeNull();
    expect(screen.queryByText('Execution')).toBeNull();
    expect(screen.queryByText('Audit')).toBeNull();
    expect(screen.getByText('Workflows → Executions')).toBeInTheDocument();
  });

  it('shows the model, tokens, cost and the result, and no "who asked"', async () => {
    mockApi(response([aiEvent(), failed]));
    render(<ActivityPage embedded />);

    const row = (await screen.findByText('Why did the orders pipeline fail?')).closest('tr')!;
    expect(screen.queryByText('Who asked')).toBeNull();
    expect(screen.queryByText(/Asked by/)).toBeNull();
    expect(within(row).getByText('gpt-4o-mini')).toBeInTheDocument();
    expect(within(row).getByText('1,200')).toBeInTheDocument();
    expect(within(row).getByText('$0.0003')).toBeInTheDocument();
    expect(within(row).getByText('Answered')).toBeInTheDocument();

    const bad = screen.getByText('Draft a pipeline from orders.csv').closest('tr')!;
    expect(within(bad).getByText('Model error')).toBeInTheDocument();
    fireEvent.click(bad);
    expect(await screen.findByText('Why it failed: HTTPStatusError: 401 Unauthorized')).toBeInTheDocument();
  });

  it('totals the whole window on the cards, not just the rows shown', async () => {
    mockApi(response([aiEvent(), failed]));
    render(<ActivityPage embedded />);
    await screen.findByText('Why did the orders pipeline fail?');

    // Tokens and Est. cost are also column headers, so allow more than one.
    for (const label of ['AI requests', 'Tokens', 'Est. cost', 'Failed AI calls']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.queryByText('Agent Runs')).toBeNull();
    expect(screen.queryByText('Failed Runs')).toBeNull();
    expect(screen.getByText('12')).toBeInTheDocument(); // summary.requests, not the 2 rows
    expect(screen.getByText('14,400')).toBeInTheDocument();
    expect(screen.getByText(/latest 2 of 12/)).toBeInTheDocument();
  });

  it('totals the rows on the cards when the backend sends no summary', async () => {
    mockApi(response([aiEvent(), failed, pipelineRun], { summary: undefined }));
    render(<ActivityPage embedded />);
    await screen.findByText('Why did the orders pipeline fail?');

    const requestsCard = screen.getByText('AI requests').parentElement!.parentElement!;
    expect(within(requestsCard).getByText('2')).toBeInTheDocument();
    const failedCard = screen.getByText('Failed AI calls').parentElement!.parentElement!;
    expect(within(failedCard).getByText('1')).toBeInTheDocument();
    expect(screen.queryByText(/latest/)).toBeNull();
  });

  it('shows requests from other AI features, and where they came from', async () => {
    mockApi(response([aiEvent(), editorDraft, notification], {
      summary: {
        requests: 3, tokens_in: 1980, tokens_out: 512, failed: 0, blocked: 0,
        by_model: [
          { provider: 'openai', model: 'gpt-4o-mini', requests: 2, tokens_in: 1900, tokens_out: 500 },
          { provider: 'ollama', model: 'qwen2.5:7b', requests: 1, tokens_in: 80, tokens_out: 12 },
        ],
        by_source: [
          { source: 'copilot', requests: 1 },
          { source: 'planner.ai_generate_pipeline', requests: 1 },
          { source: 'notifications.summary', requests: 1 },
        ],
      },
    }));
    render(<ActivityPage embedded />);

    const draft = (await screen.findByText('Load sales.csv and total by region')).closest('tr')!;
    expect(within(draft).getByText('Editor · Draft a pipeline')).toBeInTheDocument();
    const auto = screen.getByText('Write a notification').closest('tr')!;
    expect(within(auto).getByText('Notifications')).toBeInTheDocument();
    expect(screen.getByText('1 Copilot · 2 other features')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Search AI requests'), { target: { value: 'notifications' } });
    expect(screen.queryByText('Load sales.csv and total by region')).toBeNull();
    expect(screen.getByText('Write a notification')).toBeInTheDocument();
  });

  it('filters by result and searches the question', async () => {
    mockApi(response([aiEvent(), failed]));
    render(<ActivityPage embedded />);
    await screen.findByText('Why did the orders pipeline fail?');

    fireEvent.click(screen.getByRole('button', { name: /Answered/ }));
    expect(screen.queryByText('Why did the orders pipeline fail?')).toBeNull();
    expect(screen.getByText('Draft a pipeline from orders.csv')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Answered/ }));
    fireEvent.change(screen.getByLabelText('Search AI requests'), { target: { value: 'orders.csv' } });
    expect(screen.queryByText('Why did the orders pipeline fail?')).toBeNull();
    expect(screen.getByText('Draft a pipeline from orders.csv')).toBeInTheDocument();
  });

  it('says plainly when there is nothing yet', async () => {
    mockApi(response([], { summary: { requests: 0, tokens_in: 0, tokens_out: 0, failed: 0, blocked: 0, by_model: [] } }));
    render(<ActivityPage embedded />);

    expect(await screen.findByText(/No AI requests in the last 7 days\. When you ask the Copilot/)).toBeInTheDocument();
    expect(screen.queryByText(/Everyone in this workspace|Only your requests/)).toBeNull();
    expect(screen.getByRole('button', { name: /Export CSV/ })).toBeDisabled();
  });

  it('exports the rows on screen as CSV', async () => {
    mockApi(response([aiEvent(), failed]));
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((b: Blob) => { blobs.push(b); return 'blob:ai-activity'; }) as any;
    URL.revokeObjectURL = vi.fn() as any;
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    render(<ActivityPage embedded />);
    await screen.findByText('Why did the orders pipeline fail?');

    fireEvent.click(screen.getByRole('button', { name: /Export CSV/ }));
    expect(click).toHaveBeenCalled();
    expect(blobs).toHaveLength(1);
    expect(blobs[0].type).toBe('text/csv;charset=utf-8');
    const anchor = click.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(anchor.download).toMatch(/^revoluzen-flow-ai-activity-.*\.csv$/);
  });

  it('tells the Copilot it is on the AI activity page, with the requests on screen', async () => {
    mockApi(response([aiEvent(), failed]));
    render(<><ContextProbe /><ActivityPage embedded /></>);
    await screen.findByText('Why did the orders pipeline fail?');

    await waitFor(() => expect(publishedContext()?.visible_items?.length).toBe(2));
    const ctx = publishedContext();
    expect(ctx.page).toBe('ai.activity');
    expect(ctx.visible_items[0]).toMatchObject({
      id: 'run-1',
      name: 'Why did the orders pipeline fail?',
      kind: 'ai_request',
      status: 'answered',
      meta: { where: 'Copilot', model: 'gpt-4o-mini', tokens: 1200 },
    });
    expect(ctx.visible_items[1].status).toBe('failed');
  });
});

describe('Insights → Activity tab', () => {
  it('titles the tab for AI and lets the tab, not the hub, speak to the Copilot', async () => {
    // No requests, so the tab publishes only once, on mount. If the hub
    // published too, its later effect would leave 'ai' behind.
    mockApi(response([], { summary: { requests: 0, tokens_in: 0, tokens_out: 0, failed: 0, blocked: 0, by_model: [] } }));
    render(<><ContextProbe /><AIPage user={null} initialTab="activity" /></>);

    expect(await screen.findByRole('heading', { name: /AI activity/ })).toBeInTheDocument();
    expect(screen.getByText(/Each AI request: where it came from/)).toBeInTheDocument();
    expect(await screen.findByText(/No AI requests in the last 7 days/)).toBeInTheDocument();
    expect(publishedContext()?.page).toBe('ai.activity');
  });
});

describe('usePageContext enabled flag', () => {
  function Hub({ enabled }: { enabled: boolean }) {
    usePageContext({ page: 'hub', filters: { tab: 'x' }, enabled });
    return null;
  }

  it('publishes nothing while disabled and publishes once enabled', () => {
    const { rerender } = render(<><ContextProbe /><Hub enabled={false} /></>);
    expect(publishedContext()).toBeNull();
    act(() => { rerender(<><ContextProbe /><Hub enabled /></>); });
    expect(publishedContext()?.page).toBe('hub');
  });
});
