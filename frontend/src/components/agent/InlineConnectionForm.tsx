/**
 * InlineConnectionForm — create a missing connection without leaving the chat.
 *
 * Opened from the "New connection" chip on ClarifyCard. It asks for what the
 * Connections page asks for (same fields, same saved-credential filling) and
 * calls the same APIs.
 *
 * Secrets stay here: what is typed lives in this component's state and goes
 * straight to /connections. Only the new connection's id, name and type leave
 * through `onSaved`, so nothing typed can reach the transcript, the messages
 * sent to the planner, or a model.
 */

import { useEffect, useId, useMemo, useState } from 'react';
import { api } from '../../api/client';
import {
  CONNECTION_FIELDS,
  CONNECTION_TYPES,
  WRITE_ONLY_TYPES,
  buildConnectionConfig,
  configWithFieldDefaults,
  connectorStatus,
  credentialAutoMap,
  type ConnField,
} from '../pages/ConnectionsPage';

export interface CreatedConnection {
  id: string;
  name: string;
  type: string;
}

export interface InlineConnectionFormProps {
  /** Pre-selected when the request named the technology. */
  presetType?: string;
  /** Which end of the pipeline this connection is for. */
  role: 'source' | 'destination';
  onSaved: (connection: CreatedConnection) => void;
  onCancel: () => void;
}

interface SavedCredential {
  id: string;
  name: string;
  type?: string;
  username?: string;
  config?: Record<string, unknown>;
  environment?: string;
}

const MAX_LISTED_TYPES = 8;

// A long secret pasted into a text box, such as a service account key.
const SECRET_FIELD_RE = /secret|password|token|credential|private_key|api_key/i;

const inputClass =
  'w-full text-xs px-2 py-1.5 rounded-md border border-slate-200 bg-white focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-200 disabled:opacity-50';
const labelClass = 'block text-[11px] font-semibold text-slate-700 mb-1';

export default function InlineConnectionForm({ presetType, role, onSaved, onCancel }: InlineConnectionFormProps) {
  const formId = useId();
  const pickable = useMemo(
    () => CONNECTION_TYPES.filter(
      (t) => connectorStatus(t.type) !== 'roadmap' && (role === 'destination' || !WRITE_ONLY_TYPES.has(t.type)),
    ),
    [role],
  );

  const [type, setType] = useState(() => (pickable.some((t) => t.type === presetType) ? presetType! : ''));
  const [search, setSearch] = useState('');
  const [name, setName] = useState('');
  const [config, setConfig] = useState<Record<string, string>>(() => {
    const preset = pickable.some((t) => t.type === presetType) ? presetType! : '';
    return preset ? configWithFieldDefaults(preset, {}) : {};
  });
  const [credentials, setCredentials] = useState<SavedCredential[]>([]);
  const [credentialId, setCredentialId] = useState('');
  const [credentialMap, setCredentialMap] = useState<Record<string, string>>({});
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; latencyMs: number; detail: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let alive = true;
    api.listCredentials()
      .then((rows) => {
        if (!alive) return;
        const usable = (Array.isArray(rows) ? rows : []) as SavedCredential[];
        setCredentials(usable.filter((c) => (c.environment || 'all') === 'all' || c.environment === 'dev'));
      })
      .catch(() => { if (alive) setCredentials([]); });
    return () => { alive = false; };
  }, []);

  // A pass on the old details says nothing about the new ones.
  useEffect(() => { setTestResult(null); }, [type, config, credentialId]);

  const meta = CONNECTION_TYPES.find((t) => t.type === type);
  const fields: ConnField[] = type ? (CONNECTION_FIELDS[type] || CONNECTION_FIELDS['rest_api'] || []) : [];
  const effectiveConfig = type ? configWithFieldDefaults(type, config) : config;
  const missingRequired = fields.filter((f) => f.required && !effectiveConfig[f.key]);
  const detailsReady = !!type && missingRequired.length === 0;
  const canSave = detailsReady && name.trim().length > 0 && !saving;
  const selectedCredential = credentials.find((c) => c.id === credentialId);

  const listedTypes = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matches = q
      ? pickable.filter((t) =>
          t.label.toLowerCase().includes(q) || t.type.toLowerCase().includes(q) || t.category.toLowerCase().includes(q))
      : pickable;
    return matches.slice(0, MAX_LISTED_TYPES);
  }, [pickable, search]);

  const chooseType = (next: string) => {
    setType(next);
    setSearch('');
    setConfig(next ? configWithFieldDefaults(next, {}) : {});
    setCredentialId('');
    setCredentialMap({});
    setError('');
  };

  // Same as the Connections page: picking a saved credential fills the fields
  // it holds, and what an earlier choice filled comes out first.
  const chooseCredential = (nextId: string) => {
    const credential = credentials.find((c) => c.id === nextId);
    const map = credentialAutoMap(fields, credential?.config ? Object.keys(credential.config) : []);
    const next = { ...config };
    Object.keys(credentialMap).forEach((key) => { delete next[key]; });
    setConfig({ ...next, ...map });
    setCredentialMap(map);
    setCredentialId(nextId);
  };

  const setField = (key: string, value: string) => setConfig((c) => ({ ...c, [key]: value }));

  const handleTest = async () => {
    if (!detailsReady || testing) return;
    setTesting(true);
    setTestResult(null);
    const started = Date.now();
    try {
      const res = await api.post<any>('/api/connections/test-inline', {
        type,
        config: buildConnectionConfig(type, config, credentialId, credentialMap),
        credential_id: credentialId || undefined,
      });
      const ok = res?.success === true || res?.status === 'ok' || res?.ok === true;
      setTestResult({
        ok,
        latencyMs: typeof res?.latency_ms === 'number' ? res.latency_ms : Date.now() - started,
        detail: ok
          ? ''
          : [res?.detail || res?.message, res?.error, res?.suggestion].filter(Boolean).join(' · '),
      });
    } catch (err: any) {
      setTestResult({ ok: false, latencyMs: Date.now() - started, detail: err?.message || 'The test could not run.' });
    }
    setTesting(false);
  };

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    setError('');
    try {
      const created = await api.createConnection({
        name: name.trim(),
        type,
        config: buildConnectionConfig(type, config, credentialId, credentialMap),
        credential_id: credentialId || null,
        tags: [],
        project_id: null,
        // The Connections page lists a connection by environment. This edition
        // has one, and its form saves this same value.
        environment: 'dev',
        capabilities: WRITE_ONLY_TYPES.has(type) ? ['write'] : ['read', 'write'],
      });
      // A test that passed on this draft shouldn't leave the row saying
      // "Untested". Best-effort: the save itself has already succeeded.
      if (testResult?.ok && created?.id) {
        await api.testConnection(created.id).catch(() => {});
      }
      // Nothing typed here outlives the form.
      setConfig({});
      onSaved({ id: created.id, name: created.name, type: created.type });
    } catch (err: any) {
      setError(err?.message || 'The connection could not be saved.');
      setSaving(false);
    }
  };

  return (
    <div data-testid="inline-connection-form" className="rounded-lg border border-indigo-200 bg-indigo-50/30 p-3 space-y-2.5">
      <div className="text-xs font-semibold text-slate-800">
        {meta ? `New ${meta.label} connection` : 'New connection'}
      </div>

      <div>
        <label className={labelClass} htmlFor={`${formId}-type`}>Connector type</label>
        {type ? (
          <div className="flex items-center gap-2">
            <span id={`${formId}-type`} className="text-xs text-slate-700">{meta?.label || type}</span>
            <button
              type="button"
              onClick={() => chooseType('')}
              disabled={saving}
              className="text-[11px] font-medium text-indigo-700 hover:underline disabled:opacity-50"
            >
              Change
            </button>
          </div>
        ) : (
          <>
            <input
              id={`${formId}-type`}
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search connectors…"
              autoFocus
              className={inputClass}
            />
            <div className="mt-1 max-h-40 overflow-y-auto rounded-md border border-slate-200 bg-white">
              {listedTypes.map((t) => (
                <button
                  key={t.type}
                  type="button"
                  onClick={() => chooseType(t.type)}
                  className="w-full text-left px-2 py-1 text-xs text-slate-700 hover:bg-indigo-50"
                >
                  {t.label}
                </button>
              ))}
              {listedTypes.length === 0 && (
                <div className="px-2 py-1 text-xs text-slate-500">No connector matches that.</div>
              )}
            </div>
          </>
        )}
      </div>

      {type && (
        <>
          <div>
            <label className={labelClass} htmlFor={`${formId}-name`}>
              Connection name<span className="text-red-400 ml-0.5">*</span>
            </label>
            <input
              id={`${formId}-name`}
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={`e.g. Sales ${meta?.label || 'database'}`}
              disabled={saving}
              className={inputClass}
            />
          </div>

          <div>
            <label className={labelClass} htmlFor={`${formId}-credential`}>Saved credential</label>
            <select
              id={`${formId}-credential`}
              value={credentialId}
              onChange={(e) => chooseCredential(e.target.value)}
              disabled={saving}
              className={inputClass}
            >
              <option value="">Use inline fields below</option>
              {credentials.map((cred) => (
                <option key={cred.id} value={cred.id}>
                  {cred.name}{cred.username ? ` - ${cred.username}` : ''}{cred.type ? ` (${cred.type})` : ''}
                </option>
              ))}
            </select>
            {selectedCredential && (
              <p className="mt-1 text-[11px] text-slate-500 leading-snug">
                The fields {selectedCredential.name} holds fill in below, and their values stay hidden.
              </p>
            )}
          </div>

          {fields.map((f) => (
            <ConnectionField
              key={f.key}
              id={`${formId}-${f.key}`}
              field={f}
              value={config[f.key]}
              credentialKey={credentialMap[f.key]}
              credentialName={selectedCredential?.name}
              disabled={saving}
              onChange={(value) => setField(f.key, value)}
            />
          ))}

          <p className="text-[11px] text-slate-500 leading-snug">
            Passwords and keys you type here go straight to your connections. They are never added to this chat.
          </p>
        </>
      )}

      {testResult && (
        <div
          role="status"
          className={`rounded-md border px-2 py-1.5 text-xs ${
            testResult.ok ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : 'border-red-200 bg-red-50 text-red-800'
          }`}
        >
          <span className="font-semibold">{testResult.ok ? 'Connected' : 'Could not connect'}</span>
          {testResult.ok && <span> in {Math.round(testResult.latencyMs)} ms</span>}
          {testResult.detail && <div className="mt-0.5 break-words">{testResult.detail}</div>}
        </div>
      )}
      {error && <div role="alert" className="rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-xs text-red-800 break-words">{error}</div>}

      <div className="flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          disabled={saving}
          className="px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-200 rounded-md transition-colors disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleTest}
          disabled={!detailsReady || testing || saving}
          className="px-2.5 py-1 text-xs font-semibold text-indigo-700 bg-white border border-indigo-200 hover:bg-indigo-50 rounded-md transition-colors disabled:opacity-50"
        >
          {testing ? 'Testing…' : 'Test'}
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={!canSave}
          className="px-2.5 py-1 text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-md transition-colors disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  );
}

function ConnectionField({ id, field, value, credentialKey, credentialName, disabled, onChange }: {
  id: string;
  field: ConnField;
  value: string | undefined;
  /** The saved credential's key that fills this field, if one does. */
  credentialKey?: string;
  credentialName?: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const label = (
    <label className={labelClass} htmlFor={id}>
      {field.label}{field.required && <span className="text-red-400 ml-0.5">*</span>}
    </label>
  );
  const hint = field.hint && field.type !== 'checkbox' && (
    <p className="mt-0.5 text-[11px] text-slate-500 leading-snug">{field.hint}</p>
  );

  if (field.type === 'select' && field.options) {
    return (
      <div>
        {label}
        <select id={id} value={value || field.defaultValue || ''} onChange={(e) => onChange(e.target.value)} disabled={disabled} className={inputClass}>
          <option value="">— Select —</option>
          {field.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        {hint}
      </div>
    );
  }

  if (field.type === 'checkbox') {
    const checked = value === 'true' || (value as unknown) === true || (value === undefined && field.defaultValue === 'true');
    return (
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked ? 'true' : 'false')}
          disabled={disabled}
          className="w-3.5 h-3.5 rounded border-slate-300"
        />
        <label className="text-[11px] font-semibold text-slate-700" htmlFor={id}>{field.label}</label>
      </div>
    );
  }

  if (field.type === 'textarea') {
    return (
      <div>
        {label}
        <textarea
          id={id}
          value={value || ''}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          rows={3}
          disabled={disabled}
          autoComplete="off"
          spellCheck={false}
          // A password input can't hold several lines, so this masks the same way.
          style={SECRET_FIELD_RE.test(field.key) ? ({ WebkitTextSecurity: 'disc' } as React.CSSProperties) : undefined}
          className={`${inputClass} font-mono`}
        />
        {hint}
      </div>
    );
  }

  if (credentialKey) {
    return (
      <div>
        {label}
        <input
          id={id}
          type="text"
          value={`From ${credentialName ?? 'saved credential'} · ${credentialKey}`}
          readOnly
          className={`${inputClass} border-indigo-200 bg-indigo-50 text-indigo-800`}
        />
      </div>
    );
  }

  return (
    <div>
      {label}
      <input
        id={id}
        type={field.type === 'number' ? 'text' : field.type}
        inputMode={field.type === 'number' ? 'numeric' : undefined}
        value={value ?? field.defaultValue ?? ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder={field.placeholder}
        disabled={disabled}
        autoComplete={field.type === 'password' ? 'new-password' : 'off'}
        className={inputClass}
      />
      {hint}
    </div>
  );
}
