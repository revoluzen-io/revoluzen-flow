/**
 * Triggers — let another application start a pipeline and pass it values.
 *
 * Front end for the API Gateway: published endpoints, the API keys that open
 * them, and the exact command to call one. The backend is api/gateway.py, and a
 * call to an endpoint runs through the same path as the Run button, so it shows
 * up on Executions with its parameter values like any other run.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import { useCan } from '../../auth/RoleGate';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import EmptyState from '../shared/EmptyState';
import RowActionButton from '../shared/RowActionButton';
import TimeAgo from '../shared/TimeAgo';
import HubTabs, { WORKFLOWS_TABS } from '../HubTabs';
import { uiConfirm } from '../../ui/dialog';
import { usePageContext } from '../../hooks/usePageContext';
import { navigateTo, readHashQuery } from '../../router';
import type { Page } from '../../types';
import {
  API_KEY_PLACEHOLDER,
  buildCommand,
  isValidPath,
  pathFromName,
  publishedUrl,
  type CommandShell,
  type TriggerParameter,
} from '../../utils/triggerCommand';

interface PublishedEndpoint {
  id: string;
  workflow_id: string;
  path: string;
  name: string;
  require_api_key: number | boolean;
  is_active: number | boolean;
  request_count: number;
  last_called_at: number;
}

interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  is_active: number | boolean;
  last_used_at: number;
  created_at: number;
}

interface CreatedKey {
  id: string;
  name: string;
  api_key: string;
}

interface PipelineOption {
  id: string;
  name: string;
  status?: string;
  is_active_dev?: boolean;
  is_active_prod?: boolean;
}

/** How often the call counts refresh while the page is on screen. */
const REFRESH_MS = 5000;

// Writes to /api/gateway/* need rank data_engineer or above (api/gateway.py).
// `useCan` knows only the four canonical roles and lets everyone through outside
// Plus, so the rank is checked here too rather than showing a button that 403s.
const ROLES_THAT_MANAGE_TRIGGERS = new Set([
  'super_admin', 'admin', 'workspace_admin', 'lead', 'data_engineer',
]);

function roleMayManageTriggers(): boolean {
  try {
    const user = JSON.parse(localStorage.getItem('revoluzen_flow_user') || 'null');
    // No stored user means dev auto-login. The backend decides, as elsewhere.
    return !user || ROLES_THAT_MANAGE_TRIGGERS.has(user.role);
  } catch {
    return true;
  }
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Browsers offer the clipboard API on https and localhost only, and this
    // server is often reached over plain http on a network address.
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } catch {
      copied = false;
    }
    document.body.removeChild(area);
    return copied;
  }
}

function freePath(base: string, taken: Set<string>): string {
  const clean = base || 'pipeline';
  if (!taken.has(clean)) return clean;
  for (let n = 2; ; n += 1) {
    const candidate = `${clean}-${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

function parameterSummary(param: TriggerParameter): string {
  const parts = [param.type || 'string'];
  if (param.required) parts.push('required');
  else if (param.default !== null && param.default !== undefined && param.default !== '') parts.push(`default ${String(param.default)}`);
  else parts.push('optional');
  return parts.join(', ');
}

const PRIMARY_BUTTON = 'px-3 py-1.5 text-xs font-bold text-white rounded-lg shadow-sm hover:shadow-md transition-all flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed';
const PRIMARY_BUTTON_STYLE = { background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' };
const SECONDARY_BUTTON = 'px-3 py-1.5 text-xs font-semibold rounded-lg bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 transition-colors flex items-center gap-1.5 disabled:opacity-50';
const TABLE_HEAD = 'bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900 border-b-2 border-amber-400/40 text-xs uppercase tracking-wider text-amber-300 font-bold';
const INPUT = 'w-full px-3 py-2 text-sm text-slate-700 bg-white border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 placeholder:text-slate-500';

function Section({ title, description, actions, children }: {
  title: string;
  description: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <header className="px-4 py-3 border-b border-slate-200 flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-bold text-slate-800">{title}</h2>
          <p className="text-xs text-slate-500 mt-0.5">{description}</p>
        </div>
        {actions}
      </header>
      {children}
    </section>
  );
}

function DialogFrame({ title, description, onDismiss, children }: {
  title: string;
  description: string;
  /** Leave unset to make the dialog close only through its own buttons. */
  onDismiss?: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4" onClick={onDismiss}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[85vh] overflow-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 py-4 border-b border-slate-200">
          <h2 className="text-lg font-bold text-slate-800">{title}</h2>
          <p className="text-xs text-slate-500 mt-0.5">{description}</p>
        </div>
        <div className="p-6 space-y-5">{children}</div>
      </div>
    </div>
  );
}

function NewEndpointDialog({ pipelines, initialPipelineId, takenPaths, onClose, onCreated }: {
  pipelines: PipelineOption[];
  initialPipelineId: string;
  takenPaths: Set<string>;
  onClose: () => void;
  onCreated: (endpoint: PublishedEndpoint) => void;
}) {
  const [pipelineId, setPipelineId] = useState(initialPipelineId);
  const [path, setPath] = useState('');
  const [pathEdited, setPathEdited] = useState(false);
  const [requireKey, setRequireKey] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const pipeline = pipelines.find((p) => p.id === pipelineId);

  // Follows the chosen pipeline until the user types a path of their own.
  useEffect(() => {
    if (pathEdited) return;
    setPath(pipeline ? freePath(pathFromName(pipeline.name), takenPaths) : '');
  }, [pipeline?.id, pathEdited, takenPaths]);

  const cleanPath = path.trim().replace(/^\/+|\/+$/g, '');
  const pathIsValid = isValidPath(cleanPath);

  const handleCreate = async () => {
    setSaving(true);
    setError('');
    try {
      const created = await api.post<PublishedEndpoint>('/api/gateway/endpoints', {
        workflow_id: pipelineId,
        path: cleanPath,
        name: pipeline?.name || '',
        require_api_key: requireKey,
      });
      onCreated(created);
    } catch (err: any) {
      setError(err?.message || 'The endpoint could not be created.');
      setSaving(false);
    }
  };

  return (
    <DialogFrame
      title="New endpoint"
      description="Give a pipeline a web address that another application can call."
      onDismiss={onClose}
    >
      {pipelines.length === 0 ? (
        <p className="text-sm text-slate-600">
          There are no saved pipelines yet. Save a pipeline first, then come back here to publish it.
        </p>
      ) : (
        <>
          <div>
            <label htmlFor="trigger-pipeline" className="block text-xs font-semibold text-slate-600 mb-1.5">Pipeline</label>
            <select
              id="trigger-pipeline"
              value={pipelineId}
              onChange={(e) => setPipelineId(e.target.value)}
              className={INPUT}
              autoFocus
            >
              <option value="">Choose a saved pipeline</option>
              {pipelines.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </div>
          <div>
            <label htmlFor="trigger-path" className="block text-xs font-semibold text-slate-600 mb-1.5">URL path</label>
            <div className="flex items-center rounded-lg border border-slate-200 focus-within:ring-2 focus-within:ring-blue-300 overflow-hidden">
              <span className="pl-3 pr-1 py-2 text-sm font-mono text-slate-500 bg-slate-50 border-r border-slate-200 shrink-0">/api/published/</span>
              <input
                id="trigger-path"
                type="text"
                value={path}
                onChange={(e) => { setPath(e.target.value); setPathEdited(true); }}
                placeholder="daily-orders"
                className="flex-1 min-w-0 px-2 py-2 text-sm font-mono text-slate-700 bg-white focus:outline-none placeholder:text-slate-500"
              />
            </div>
            {cleanPath !== '' && !pathIsValid && (
              <p className="text-xs text-red-600 mt-1">
                The path can use letters, numbers, hyphens, underscores, dots and slashes, and each part must start with a letter or number.
              </p>
            )}
          </div>
          <label className="flex items-start gap-2.5 cursor-pointer">
            <input
              type="checkbox"
              checked={requireKey}
              onChange={(e) => setRequireKey(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              <span className="block text-sm font-semibold text-slate-700">Require an API key</span>
              <span className="block text-xs text-slate-500 mt-0.5">
                {requireKey
                  ? 'Recommended. Only callers holding a key from this page can start the pipeline.'
                  : 'Without a key, anyone who can reach this server can start the pipeline.'}
              </span>
            </span>
          </label>
        </>
      )}
      {error && (
        <div role="alert" className="px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-700">{error}</div>
      )}
      <div className="flex justify-end gap-2 pt-1">
        <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>Cancel</button>
        <button
          type="button"
          onClick={handleCreate}
          disabled={saving || !pipelineId || !pathIsValid}
          className={PRIMARY_BUTTON}
          style={PRIMARY_BUTTON_STYLE}
        >
          {saving ? 'Creating…' : 'Create endpoint'}
        </button>
      </div>
    </DialogFrame>
  );
}

function NewKeyDialog({ onClose, onCreated }: {
  onClose: () => void;
  onCreated: (key: CreatedKey) => void;
}) {
  const [name, setName] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [created, setCreated] = useState<CreatedKey | null>(null);
  const [copied, setCopied] = useState(false);

  const handleCreate = async () => {
    setSaving(true);
    setError('');
    try {
      const key = await api.post<CreatedKey>('/api/gateway/keys', { name: name.trim() });
      setCreated(key);
      onCreated(key);
    } catch (err: any) {
      setError(err?.message || 'The key could not be created.');
    }
    setSaving(false);
  };

  const handleCopy = async () => {
    if (!created) return;
    if (await copyText(created.api_key)) {
      setCopied(true);
    } else {
      toast.error('Copy failed', 'Your browser blocked the clipboard. Select the key and copy it by hand.');
    }
  };

  if (created) {
    return (
      <DialogFrame title="Copy your API key" description={`The key "${created.name}" is ready to use.`}>
        <div role="alert" className="px-3 py-2.5 rounded-lg bg-amber-50 border border-amber-200 text-sm text-amber-900">
          This is the only time the full key is shown. Copy it now and store it somewhere safe. If you lose it, revoke it and create a new one.
        </div>
        <div>
          <label htmlFor="trigger-new-key" className="block text-xs font-semibold text-slate-600 mb-1.5">API key</label>
          <input
            id="trigger-new-key"
            type="text"
            readOnly
            value={created.api_key}
            onFocus={(e) => e.target.select()}
            className={`${INPUT} font-mono text-xs`}
          />
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={handleCopy} className={SECONDARY_BUTTON}>
            {copied ? 'Copied' : 'Copy key'}
          </button>
          <button type="button" onClick={onClose} className={PRIMARY_BUTTON} style={PRIMARY_BUTTON_STYLE}>Done</button>
        </div>
      </DialogFrame>
    );
  }

  return (
    <DialogFrame
      title="New API key"
      description="An API key proves that the caller may start a pipeline. Name it after the application that will use it."
      onDismiss={onClose}
    >
      <div>
        <label htmlFor="trigger-key-name" className="block text-xs font-semibold text-slate-600 mb-1.5">Key name</label>
        <input
          id="trigger-key-name"
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && name.trim() && !saving) void handleCreate(); }}
          placeholder="Order system"
          className={INPUT}
          autoFocus
        />
      </div>
      {error && (
        <div role="alert" className="px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-700">{error}</div>
      )}
      <div className="flex justify-end gap-2 pt-1">
        <button type="button" onClick={onClose} className={SECONDARY_BUTTON}>Cancel</button>
        <button
          type="button"
          onClick={handleCreate}
          disabled={saving || !name.trim()}
          className={PRIMARY_BUTTON}
          style={PRIMARY_BUTTON_STYLE}
        >
          {saving ? 'Creating…' : 'Create key'}
        </button>
      </div>
    </DialogFrame>
  );
}

export default function TriggersPage({ environment = 'dev', tier = 'free' }: {
  environment?: 'dev' | 'prod';
  tier?: string;
}) {
  const canManage = useCan('create', environment) && roleMayManageTriggers();
  const [endpoints, setEndpoints] = useState<PublishedEndpoint[]>([]);
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [pipelines, setPipelines] = useState<PipelineOption[]>([]);
  const [parametersByPipeline, setParametersByPipeline] = useState<Record<string, TriggerParameter[]>>({});
  const [loaded, setLoaded] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [shell, setShell] = useState<CommandShell>('curl');
  const [commandCopied, setCommandCopied] = useState(false);
  const [endpointDialog, setEndpointDialog] = useState<{ pipelineId: string } | null>(null);
  const [keyDialogOpen, setKeyDialogOpen] = useState(false);
  // The full key exists only here, for this visit: the server keeps a hash.
  const [sessionKey, setSessionKey] = useState<CreatedKey | null>(null);
  const commandRef = useRef<HTMLDivElement>(null);

  const refreshCounts = useCallback(async () => {
    const [nextEndpoints, nextKeys] = await Promise.all([
      api.get<PublishedEndpoint[]>('/api/gateway/endpoints'),
      api.get<ApiKey[]>('/api/gateway/keys'),
    ]);
    setEndpoints(Array.isArray(nextEndpoints) ? nextEndpoints : []);
    setKeys(Array.isArray(nextKeys) ? nextKeys : []);
    return Array.isArray(nextEndpoints) ? nextEndpoints : [];
  }, []);

  const loadParameters = useCallback(async (workflowIds: string[]) => {
    const unique = Array.from(new Set(workflowIds));
    const found = await Promise.all(unique.map(async (id) => {
      try {
        const data = await api.getWorkflow(id);
        const wf = data?.workflow || data;
        return [id, Array.isArray(wf?.parameters) ? wf.parameters : []] as const;
      } catch {
        return [id, []] as const;
      }
    }));
    setParametersByPipeline((prev) => ({ ...prev, ...Object.fromEntries(found) }));
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const [nextEndpoints, workflows] = await Promise.all([refreshCounts(), api.listWorkflows()]);
      setPipelines(
        (Array.isArray(workflows) ? workflows : [])
          .filter((w: any) => w.status !== 'archived')
          .map((w: any) => ({
            id: w.id, name: w.name, status: w.status, is_active_dev: w.is_active_dev, is_active_prod: w.is_active_prod,
          }))
          .sort((a: PipelineOption, b: PipelineOption) => a.name.localeCompare(b.name)),
      );
      await loadParameters(nextEndpoints.map((e) => e.workflow_id));
    } catch (err: any) {
      toast.error('Could not load triggers', err?.message || 'Try reloading the page.');
    }
    setLoaded(true);
  }, [refreshCounts, loadParameters]);

  useEffect(() => { void loadAll(); }, [loadAll]);

  // Call counts move while another application is calling, so keep them fresh
  // while the page is on screen. A failed refresh stays quiet: the app shell
  // already reports an unreachable backend, and the next tick retries.
  useEffect(() => {
    const timer = setInterval(() => {
      if (!document.hidden) refreshCounts().catch(() => {});
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refreshCounts]);

  // Coming back from the Editor in another tab, the parameters may have changed.
  useEffect(() => {
    const onFocus = () => { void loadAll(); };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [loadAll]);

  useEffect(() => {
    if (selectedId && endpoints.some((e) => e.id === selectedId)) return;
    setSelectedId(endpoints[0]?.id ?? null);
  }, [endpoints, selectedId]);

  // "Run from another app" on the Pipelines page lands here as
  // #triggers?pipeline=<id>. Consumed once, so a reload does not reopen it.
  useEffect(() => {
    if (!loaded) return;
    const consume = () => {
      const wanted = readHashQuery().pipeline;
      if (!wanted) return;
      window.history.replaceState(null, '', '#triggers');
      const existing = endpoints.find((e) => e.workflow_id === wanted);
      if (existing) {
        setSelectedId(existing.id);
      } else if (canManage) {
        setEndpointDialog({ pipelineId: wanted });
      } else {
        toast.info('No endpoint yet', 'This pipeline has no endpoint, and your role cannot create one.');
      }
    };
    consume();
    window.addEventListener('hashchange', consume);
    return () => window.removeEventListener('hashchange', consume);
  }, [loaded, endpoints, canManage]);

  const pipelineById = useMemo(() => new Map(pipelines.map((p) => [p.id, p])), [pipelines]);
  const takenPaths = useMemo(() => new Set(endpoints.map((e) => e.path)), [endpoints]);
  const selected = endpoints.find((e) => e.id === selectedId) ?? null;
  const selectedParameters = selected ? (parametersByPipeline[selected.workflow_id] ?? []) : [];
  const selectedPipeline = selected ? pipelineById.get(selected.workflow_id) : undefined;
  const selectedNeedsKey = !!selected?.require_api_key;

  const commandFor = (endpoint: PublishedEndpoint, chosenShell: CommandShell = shell) => buildCommand({
    origin: window.location.origin,
    path: endpoint.path,
    requireApiKey: !!endpoint.require_api_key,
    apiKey: sessionKey?.api_key,
    parameters: parametersByPipeline[endpoint.workflow_id] ?? [],
    shell: chosenShell,
  });

  const copyCommand = async (endpoint: PublishedEndpoint) => {
    if (await copyText(commandFor(endpoint))) {
      toast.success('Command copied', 'Paste it into a terminal to start the pipeline.');
      if (endpoint.id === selectedId) {
        setCommandCopied(true);
        setTimeout(() => setCommandCopied(false), 2000);
      }
    } else {
      toast.error('Copy failed', 'Your browser blocked the clipboard. Select the command and copy it by hand.');
    }
  };

  const setEndpointOn = async (endpoint: PublishedEndpoint, on: boolean) => {
    setEndpoints((prev) => prev.map((e) => (e.id === endpoint.id ? { ...e, is_active: on ? 1 : 0 } : e)));
    try {
      await api.put(`/api/gateway/endpoints/${endpoint.id}`, { is_active: on ? 1 : 0 });
      await refreshCounts();
    } catch (err: any) {
      toast.error('Could not change the endpoint', err?.message || 'Try again.');
      await refreshCounts().catch(() => {});
    }
  };

  const deleteEndpoint = async (endpoint: PublishedEndpoint) => {
    const ok = await uiConfirm({
      title: `Delete /${endpoint.path}?`,
      message: 'Applications that call this address will get a 404 error. The pipeline itself is not changed.',
      confirmLabel: 'Delete',
      destructive: true,
    });
    if (!ok) return;
    try {
      await api.delete(`/api/gateway/endpoints/${endpoint.id}`);
      toast.success('Endpoint deleted');
      await refreshCounts();
    } catch (err: any) {
      toast.error('Could not delete the endpoint', err?.message || 'Try again.');
    }
  };

  const revokeKey = async (key: ApiKey) => {
    const ok = await uiConfirm({
      title: `Revoke "${key.name}"?`,
      message: 'Applications using this key will get a 403 error straight away. This cannot be undone.',
      confirmLabel: 'Revoke',
      destructive: true,
    });
    if (!ok) return;
    try {
      await api.delete(`/api/gateway/keys/${key.id}`);
      if (sessionKey?.id === key.id) setSessionKey(null);
      toast.success('API key revoked');
      await refreshCounts();
    } catch (err: any) {
      toast.error('Could not revoke the key', err?.message || 'Try again.');
    }
  };

  const handleEndpointCreated = async (endpoint: PublishedEndpoint) => {
    setEndpointDialog(null);
    toast.success('Endpoint created', `/${endpoint.path} is ready to call.`);
    await Promise.all([refreshCounts(), loadParameters([endpoint.workflow_id])]);
    setSelectedId(endpoint.id);
    commandRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  };

  usePageContext({
    page: 'triggers',
    visible_ids: endpoints.map((e) => e.id),
    selected_ids: selectedId ? [selectedId] : [],
    environment,
  });

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      <div className="flex-1 overflow-auto bg-canvas-bg">
        <PageHeader
          environment={environment}
          icon={(
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-blue-500">
              <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" />
            </svg>
          )}
          title="Triggers"
          titleAccessory={<TierChip tier={tier} environment={environment} />}
          subtitle="Let another application start a pipeline and pass it values."
          tabs={(
            <HubTabs
              tabs={WORKFLOWS_TABS}
              active="triggers"
              onNavigate={(p) => navigateTo(p as Page)}
              environment={environment}
            />
          )}
          actions={canManage ? (
            <div className="flex flex-wrap justify-end items-center gap-2 sm:flex-nowrap sm:shrink-0">
              <button type="button" onClick={() => setKeyDialogOpen(true)} className={SECONDARY_BUTTON}>
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="7.5" cy="15.5" r="5.5" /><path d="m21 2-9.6 9.6" /><path d="m15.5 7.5 3 3L22 7l-3-3" /></svg>
                New API key
              </button>
              <button
                type="button"
                onClick={() => setEndpointDialog({ pipelineId: '' })}
                className={PRIMARY_BUTTON}
                style={PRIMARY_BUTTON_STYLE}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></svg>
                New endpoint
              </button>
            </div>
          ) : undefined}
        />

        <div className="w-full max-w-[1500px] mx-auto px-8 py-6 space-y-6">
          {!canManage && (
            <p className="px-3 py-2 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-600">
              You can view endpoints and API keys here. Creating or changing them needs a data engineer or administrator role.
            </p>
          )}

          <Section
            title="Endpoints"
            description="An endpoint is a web address that starts one pipeline when another application calls it."
          >
            {!loaded ? (
              <div className="flex items-center justify-center py-12">
                <div className="w-6 h-6 border-2 border-indigo-300 border-t-transparent rounded-full animate-spin" />
              </div>
            ) : endpoints.length === 0 ? (
              <EmptyState
                title="No endpoints yet"
                body="Create an endpoint to give a saved pipeline a web address that another application can call."
                primaryCta={canManage ? { label: 'New endpoint', onClick: () => setEndpointDialog({ pipelineId: '' }) } : undefined}
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm border-collapse">
                  <thead className={TABLE_HEAD}>
                    <tr>
                      <th className="px-4 py-2.5 text-left">Pipeline</th>
                      <th className="px-3 py-2.5 text-left">URL path</th>
                      <th className="px-3 py-2.5 text-left">Key required</th>
                      <th className="px-3 py-2.5 text-right">Calls</th>
                      <th className="px-3 py-2.5 text-left">Last called</th>
                      <th className="px-3 py-2.5 text-left">On/Off</th>
                      <th className="px-3 py-2.5 text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {endpoints.map((endpoint) => {
                      const pipeline = pipelineById.get(endpoint.workflow_id);
                      const on = !!endpoint.is_active;
                      const isSelected = endpoint.id === selectedId;
                      return (
                        <tr
                          key={endpoint.id}
                          onClick={() => setSelectedId(endpoint.id)}
                          aria-current={isSelected ? 'true' : undefined}
                          className={`cursor-pointer transition-colors ${isSelected ? 'bg-blue-50/70' : 'hover:bg-slate-50'}`}
                        >
                          <td className="px-4 py-2.5 max-w-[320px]">
                            <span className="block font-semibold text-slate-800 truncate" title={pipeline?.name || endpoint.name}>
                              {pipeline?.name || endpoint.name || 'Pipeline not found'}
                            </span>
                          </td>
                          <td className="px-3 py-2.5 text-xs font-mono text-slate-700 whitespace-nowrap">/api/published/{endpoint.path}</td>
                          <td className="px-3 py-2.5 text-xs font-semibold">
                            {endpoint.require_api_key
                              ? <span className="text-emerald-600">Yes</span>
                              : <span className="text-amber-600" title="Anyone who can reach this server can call it.">No</span>}
                          </td>
                          <td className={`px-3 py-2.5 text-right tabular-nums font-semibold ${endpoint.request_count > 0 ? 'text-blue-600' : 'text-slate-800'}`}>
                            {endpoint.request_count}
                          </td>
                          <td className="px-3 py-2.5">
                            <TimeAgo value={endpoint.last_called_at || null} emptyText="Never" className="text-xs !text-slate-500" />
                          </td>
                          <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                            <div className="flex items-center gap-2">
                              <button
                                type="button"
                                role="switch"
                                aria-checked={on}
                                aria-label={`Endpoint /${endpoint.path} is ${on ? 'on' : 'off'}`}
                                disabled={!canManage}
                                onClick={() => setEndpointOn(endpoint, !on)}
                                className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors disabled:opacity-60 disabled:cursor-not-allowed ${on ? 'bg-emerald-500' : 'bg-slate-300'}`}
                              >
                                <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${on ? 'translate-x-[18px]' : 'translate-x-0.5'}`} />
                              </button>
                              <span className="text-xs text-slate-600">{on ? 'On' : 'Off'}</span>
                            </div>
                          </td>
                          <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                            <div className="flex items-center justify-end gap-1">
                              <RowActionButton onClick={() => copyCommand(endpoint)} title="Copy the command for this endpoint" tone="blue" label="Copy command">
                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
                              </RowActionButton>
                              {canManage && (
                                <RowActionButton onClick={() => deleteEndpoint(endpoint)} title="Delete this endpoint" tone="red">
                                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-2 14H7L5 6" /><path d="M9 6V4h6v2" /></svg>
                                </RowActionButton>
                              )}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Section>

          {selected && (
            <div ref={commandRef}>
              <Section
                title="Ready-to-use command"
                description="Run this in a terminal to start the pipeline, or send the same request from your application."
                actions={(
                  <div className="flex items-center gap-2">
                    {endpoints.length > 1 && (
                      <select
                        aria-label="Endpoint for this command"
                        value={selected.id}
                        onChange={(e) => setSelectedId(e.target.value)}
                        className="px-2 py-1.5 text-xs font-mono text-slate-700 bg-white border border-slate-300 rounded-lg max-w-[260px]"
                      >
                        {endpoints.map((e) => <option key={e.id} value={e.id}>/{e.path}</option>)}
                      </select>
                    )}
                    <div role="group" aria-label="Command format" className="inline-flex rounded-lg border border-slate-300 overflow-hidden">
                      {(['curl', 'powershell'] as const).map((option) => (
                        <button
                          key={option}
                          type="button"
                          aria-pressed={shell === option}
                          onClick={() => setShell(option)}
                          title={option === 'curl' ? 'For macOS, Linux and Git Bash' : 'For Windows PowerShell'}
                          className={`px-2.5 py-1.5 text-xs font-semibold transition-colors ${shell === option ? 'bg-slate-800 text-white' : 'bg-white text-slate-600 hover:bg-slate-50'}`}
                        >
                          {option === 'curl' ? 'curl' : 'PowerShell'}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              >
                <div className="p-4 space-y-4">
                  {!selected.is_active && (
                    <p className="px-3 py-2 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-800">
                      This endpoint is off, so calls get a 503 error until you turn it on.
                    </p>
                  )}
                  {(selectedPipeline?.is_active_dev === false || selectedPipeline?.is_active_prod === false) && (
                    <p className="px-3 py-2 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-800">
                      This pipeline is deactivated, so calls get a 423 error until it is activated on the Pipelines page.
                    </p>
                  )}
                  <div className="relative">
                    <pre
                      data-testid="trigger-command"
                      className="rounded-lg bg-slate-900 text-slate-100 text-xs leading-relaxed font-mono p-4 pr-28 overflow-x-auto whitespace-pre"
                    >
                      {commandFor(selected)}
                    </pre>
                    <button
                      type="button"
                      onClick={() => copyCommand(selected)}
                      className="absolute top-2.5 right-2.5 px-2.5 py-1 text-xs font-semibold rounded-md bg-white/10 text-slate-100 border border-white/20 hover:bg-white/20 transition-colors"
                    >
                      {commandCopied ? 'Copied' : 'Copy command'}
                    </button>
                  </div>

                  <dl className="text-xs text-slate-600 space-y-1.5">
                    <div className="flex gap-2">
                      <dt className="w-16 shrink-0 font-semibold text-slate-700">Address</dt>
                      <dd>The address tells Revoluzen Flow which pipeline to start.</dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="w-16 shrink-0 font-semibold text-slate-700">Key</dt>
                      <dd>
                        {!selectedNeedsKey
                          ? 'This endpoint needs no key, so anyone who can reach this server can start the pipeline.'
                          : sessionKey
                            ? `The X-API-Key header proves the caller may start it. This command holds the key "${sessionKey.name}" you just created, so treat it like a password.`
                            : `The X-API-Key header proves the caller may start it. Replace ${API_KEY_PLACEHOLDER} with a key you saved when you created it. A key is only shown once, so create a new one if you no longer have it.`}
                      </dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="w-16 shrink-0 font-semibold text-slate-700">Values</dt>
                      <dd>
                        {selectedParameters.length > 0
                          ? 'Each value in the body replaces that parameter of the pipeline for this one run. Leave one out to use its default.'
                          : 'This pipeline has no parameters, so the call sends no values. Add parameters in the Editor to pass some.'}
                      </dd>
                    </div>
                    <div className="flex gap-2">
                      <dt className="w-16 shrink-0 font-semibold text-slate-700">Result</dt>
                      <dd>The call waits for the run to finish, then returns its status, its run ID and the row count of each step. It never returns your data.</dd>
                    </div>
                  </dl>

                  {selectedParameters.length > 0 && (
                    <div>
                      <h3 className="text-xs font-semibold text-slate-700 mb-1.5">Parameters of this pipeline</h3>
                      <ul className="text-xs text-slate-600 space-y-1">
                        {selectedParameters.map((param) => (
                          <li key={param.name}>
                            <code className="font-mono font-semibold text-slate-800">{param.name}</code>
                            <span className="text-slate-500"> ({parameterSummary(param)})</span>
                            {param.description ? <span>: {param.description}</span> : null}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <button
                    type="button"
                    onClick={() => navigateTo('executions')}
                    className="text-xs font-semibold text-blue-700 hover:underline"
                  >
                    See its runs on the Executions page
                  </button>
                </div>
              </Section>
            </div>
          )}

          <Section
            title="API keys"
            description="One key opens every endpoint that requires a key in this workspace. Revoke a key to shut out whoever holds it."
          >
            {!loaded ? null : keys.length === 0 ? (
              <EmptyState
                title="No API keys yet"
                body="Create a key for each application that will call your endpoints, so you can revoke one without affecting the others."
                primaryCta={canManage ? { label: 'New API key', onClick: () => setKeyDialogOpen(true) } : undefined}
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm border-collapse">
                  <thead className={TABLE_HEAD}>
                    <tr>
                      <th className="px-4 py-2.5 text-left">Name</th>
                      <th className="px-3 py-2.5 text-left">Key</th>
                      <th className="px-3 py-2.5 text-left">Created</th>
                      <th className="px-3 py-2.5 text-left">Last used</th>
                      <th className="px-3 py-2.5 text-right">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {keys.map((key) => {
                      const active = !!key.is_active;
                      return (
                        <tr key={key.id}>
                          <td className={`px-4 py-2.5 font-semibold ${active ? 'text-slate-800' : 'text-slate-400'}`}>{key.name}</td>
                          <td className={`px-3 py-2.5 text-xs font-mono ${active ? 'text-slate-700' : 'text-slate-400'}`}>{key.key_prefix}••••••••</td>
                          <td className="px-3 py-2.5"><TimeAgo value={key.created_at || null} className="text-xs !text-slate-500" /></td>
                          <td className="px-3 py-2.5"><TimeAgo value={key.last_used_at || null} emptyText="Never" className="text-xs !text-slate-500" /></td>
                          <td className="px-3 py-2.5">
                            <div className="flex items-center justify-end">
                              {!active ? (
                                <span className="text-xs font-semibold text-slate-500">Revoked</span>
                              ) : canManage ? (
                                <RowActionButton onClick={() => revokeKey(key)} title="Revoke this API key" tone="red" label="Revoke">
                                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><line x1="4.93" y1="4.93" x2="19.07" y2="19.07" /></svg>
                                </RowActionButton>
                              ) : null}
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Section>
        </div>
      </div>

      {endpointDialog && (
        <NewEndpointDialog
          pipelines={pipelines}
          initialPipelineId={endpointDialog.pipelineId}
          takenPaths={takenPaths}
          onClose={() => setEndpointDialog(null)}
          onCreated={handleEndpointCreated}
        />
      )}
      {keyDialogOpen && (
        <NewKeyDialog
          onClose={() => setKeyDialogOpen(false)}
          onCreated={(key) => { setSessionKey(key); void refreshCounts(); }}
        />
      )}
    </div>
  );
}
