/**
 * A connection version set against the connection's settings now, one row per
 * setting.
 *
 * The server sends each password or key as `{ secret: true, tag }`: the same
 * tag on both sides means the same secret. The value is never sent and Flow
 * does not keep old ones, so a secret row can only say whether the version's
 * secret is the same as today's, different, or missing on one side.
 */
import type { ConnectionSettings } from '../../api/client';

export interface SecretTag {
  secret: true;
  tag: string;
}

export function isSecretTag(value: unknown): value is SecretTag {
  return !!value && typeof value === 'object' && (value as any).secret === true && typeof (value as any).tag === 'string';
}

/** same: the version's secret is today's. changed: it was another one.
 *  only-version: the connection has none now. only-now: the version had none. */
export type SecretState = 'same' | 'changed' | 'only-version' | 'only-now';

export interface ConnectionDiffRow {
  /** 'name', 'config.host', 'config.auth.password'. */
  key: string;
  label: string;
  /** As shown; null when not set. */
  version: string | null;
  now: string | null;
  changed: boolean;
  /** A password or key: its name under the settings ('password', 'auth.password'). */
  secretPath?: string;
  secretState?: SecretState;
}

export interface ConnectionDiff {
  rows: ConnectionDiffRow[];
  changed: ConnectionDiffRow[];
  identical: boolean;
}

/** Names for ids, from what the page has loaded. */
export interface DiffNames {
  credential?: (id: string) => string | undefined;
  project?: (id: string) => string | undefined;
  type?: (type: string) => string | undefined;
}

function capital(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/** 'auth.client_secret' → 'Auth › client secret'. */
export function settingLabel(path: string): string {
  return capital(path.split('.').map((part) => part.replace(/_/g, ' ').trim()).join(' › '));
}

function show(value: unknown): string | null {
  if (value === undefined || value === null || value === '') return null;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) {
    return value.length ? value.map((x) => (x && typeof x === 'object' ? JSON.stringify(x) : String(x))).join(', ') : null;
  }
  if (typeof value === 'object' && Object.keys(value as object).length === 0) return null;
  return JSON.stringify(value);
}

function flatten(config: Record<string, any> | null | undefined, prefix = '', out = new Map<string, unknown>()): Map<string, unknown> {
  for (const [key, value] of Object.entries(config || {})) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === 'object' && !Array.isArray(value) && !isSecretTag(value) && Object.keys(value).length > 0) {
      flatten(value, path, out);
    } else {
      out.set(path, value);
    }
  }
  return out;
}

function secretRow(path: string, version: unknown, now: unknown): ConnectionDiffRow {
  const inVersion = isSecretTag(version);
  const inNow = isSecretTag(now);
  const state: SecretState = inVersion && inNow
    ? ((version as SecretTag).tag === (now as SecretTag).tag ? 'same' : 'changed')
    : inVersion ? 'only-version' : 'only-now';
  return {
    key: `config.${path}`,
    label: settingLabel(path),
    version: inVersion ? (state === 'same' ? 'Set, the same as now' : 'Set, a different one') : show(version),
    now: inNow ? 'Set' : show(now),
    changed: state !== 'same',
    secretPath: path,
    secretState: state,
  };
}

const ENVIRONMENTS: Record<string, string> = { all: 'DEV and PROD', dev: 'DEV', prod: 'PROD' };

function roles(caps: string[] | null | undefined): string {
  const list = caps && caps.length ? caps : ['read', 'write'];
  return list.map(capital).join(' · ');
}

export function diffConnection(version: ConnectionSettings, now: ConnectionSettings, names: DiffNames = {}): ConnectionDiff {
  const rows: ConnectionDiffRow[] = [];
  const plain = (key: string, label: string, a: string | null, b: string | null) => {
    rows.push({ key, label, version: a, now: b, changed: a !== b });
  };
  const typeName = (t: string) => (t ? names.type?.(t) || t : null);
  const credential = (id: string | null) => (id ? names.credential?.(id) || `${id} (not found)` : null);
  const project = (id: string | null) => (id ? names.project?.(id) || `${id} (not found)` : 'Global');
  const environment = (env: string | null) => (env ? ENVIRONMENTS[env] || env : null);

  plain('name', 'Name', show(version.name), show(now.name));
  plain('type', 'Type', typeName(version.type), typeName(now.type));
  const a = flatten(version.config);
  const b = flatten(now.config);
  for (const path of [...a.keys(), ...[...b.keys()].filter((k) => !a.has(k))]) {
    const va = a.get(path);
    const vb = b.get(path);
    if (isSecretTag(va) || isSecretTag(vb)) rows.push(secretRow(path, va, vb));
    else plain(`config.${path}`, settingLabel(path), show(va), show(vb));
  }
  plain('credential_id', 'Linked credential', credential(version.credential_id), credential(now.credential_id));
  plain('project_id', 'Project', project(version.project_id), project(now.project_id));
  plain('environment', 'Environment', environment(version.environment), environment(now.environment));
  plain('capabilities', 'Roles', roles(version.capabilities), roles(now.capabilities));
  plain('description', 'Description', show(version.description), show(now.description));
  plain('tags', 'Tags', show(version.tags), show(now.tags));

  const changed = rows.filter((r) => r.changed);
  return { rows, changed, identical: changed.length === 0 };
}

/** "Host and password", "Host, port and 2 more". */
export function changedSummary(labels: string[]): string {
  const names = labels.map((l) => l.toLowerCase());
  if (names.length === 0) return '';
  const shown = names.length > 3 ? [...names.slice(0, 2), `${names.length - 2} more`] : names;
  const text = shown.length === 1 ? shown[0] : `${shown.slice(0, -1).join(', ')} and ${shown[shown.length - 1]}`;
  return capital(text);
}
