/**
 * What changed between two saved versions of a pipeline, for the pipeline
 * tab's Versions view: steps added, removed and changed (every setting before
 * and after), the links between steps, and pipeline settings.
 *
 * Steps are matched by id, so a renamed step reads as changed, not as one
 * removed and one added. Moving a step on the canvas is not a change to what
 * the pipeline does; it is reported on its own (`layoutOnly`).
 *
 * Pure: the view fetches the two versions (GET /workflows/{id}?version=N)
 * and passes their `workflow` objects in.
 */

export type StepChange = 'added' | 'removed' | 'changed' | 'same';
export type LinkChange = 'added' | 'removed' | 'same';

export interface SettingRow {
  key: string;
  label: string;
  /** null when the setting, or the whole step, is not in that version. */
  before: string | null;
  after: string | null;
  changed: boolean;
}

export interface StepDiff {
  id: string;
  kind: StepChange;
  type: string;
  /** The step's own name in the newer version (the older one if removed). */
  name: string;
  rows: SettingRow[];
  /** Where it sits: the newer version's position, the older one's if removed. */
  position: { x: number; y: number };
  moved: boolean;
}

export interface LinkDiff {
  from: string;
  to: string;
  kind: LinkChange;
}

export interface VersionDiff {
  steps: StepDiff[];
  links: LinkDiff[];
  /** Pipeline-level settings that differ (name, parameters, ...). */
  pipeline: SettingRow[];
  added: number;
  removed: number;
  changed: number;
  same: number;
  linksChanged: boolean;
  /** Nothing differs, steps' places on the canvas included. */
  identical: boolean;
  /** Only steps' places on the canvas differ. */
  layoutOnly: boolean;
}

export interface DiffOptions {
  /** Connection, credential and shared rule set names by id, so settings read as names. */
  names?: Record<string, string>;
}

const LABELS: Record<string, string> = {
  connection_id: 'Connection',
  credential_id: 'Credential',
  connection_name: 'Connection',
  sql: 'SQL',
  query: 'Query',
  url: 'URL',
  api_url: 'URL',
  file_path: 'File',
  path: 'Path',
  table_name: 'Table',
  table: 'Table',
  schema: 'Schema',
  write_mode: 'Write mode',
  mode: 'Mode',
  condition: 'Condition',
  expression: 'Expression',
  columns: 'Columns',
  format: 'Format',
  rule_sets: 'Shared rule sets',
  _settings: 'Step settings',
};

// Settings that hold ids people know by name. 2026-11-12: a Data Quality step's
// shared rule sets read as "rs_9cd3c05ee31b" in the comparison.
const NAME_KEYS = new Set(['connection_id', 'credential_id', 'rule_sets']);

/** Words that make a setting a password or key, after the server's list of
 *  secret names (connections/inline_secrets.py). Matched as whole words, so
 *  `max_tokens` or a join `key` still show. */
const SECRET_WORDS = new Set(['password', 'passwd', 'pwd', 'secret', 'token', 'passphrase', 'authorization', 'credentials']);
const SECRET_ENDINGS = /(apikey|privatekey|credentialsjson|webhookurl|customheadervalue|appkey|routingkey)$/;
/** A secret word followed by one of these names a place, not a secret: `token_url`, `secret_name`. */
const NOT_SECRET_LAST = new Set(['url', 'uri', 'endpoint', 'name', 'id', 'type', 'field', 'column', 'path', 'ref']);

/** Whether a step setting holds a password or key. The Versions view then
 *  says whether it is set and whether it changed, never what it is. */
export function isSecretSetting(key: string): boolean {
  if (!key) return false;
  if (SECRET_ENDINGS.test(key.toLowerCase().replace(/[^a-z0-9]/g, ''))) return true;
  const words = key.replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  if (words.length === 0 || NOT_SECRET_LAST.has(words[words.length - 1])) return false;
  return words.some((w) => SECRET_WORDS.has(w));
}

/** A value to hide: typed text, not a `${...}` or `{{...}}` reference. */
function isSecretValue(value: unknown): boolean {
  return typeof value === 'string' && value !== '' && !/^\s*(\$\{|\{\{)/.test(value);
}

/** "write_mode" → "Write mode", "batchSize" → "Batch size", "_settings" → "Step settings". */
export function settingLabel(key: string): string {
  if (LABELS[key]) return LABELS[key];
  const words = key
    .replace(/^_+/, '')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .trim()
    .toLowerCase();
  if (!words) return key;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Stable text for any setting value: same value, same text. */
export function settingText(value: unknown, key = '', names: Record<string, string> = {}): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') {
    if (NAME_KEYS.has(key) && names[value]) return names[value];
    return value;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return '—';
    if (value.every((v) => v === null || ['string', 'number', 'boolean'].includes(typeof v))) {
      return value.map((v) => settingText(v, NAME_KEYS.has(key) ? key : '', names)).join(', ');
    }
    return stableJson(value);
  }
  return stableJson(value);
}

function stableJson(value: unknown): string {
  return JSON.stringify(sortKeys(value));
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(value as Record<string, unknown>).sort()) out[k] = sortKeys((value as any)[k]);
    return out;
  }
  return value;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

/** One row per setting. A setting holding a small object of its own (the
 *  step settings bag, for one) gets a row per entry inside it. */
function flatten(params: Record<string, unknown> | undefined): Record<string, { label: string; value: unknown; key: string }> {
  const out: Record<string, { label: string; value: unknown; key: string }> = {};
  for (const [k, v] of Object.entries(params || {})) {
    if (isPlainObject(v) && Object.keys(v).length > 0 && Object.keys(v).length <= 12 && Object.values(v).every((x) => !isPlainObject(x))) {
      for (const [sk, sv] of Object.entries(v)) {
        out[`${k}.${sk}`] = { label: `${settingLabel(k)} › ${settingLabel(sk).toLowerCase()}`, value: sv, key: sk };
      }
    } else {
      out[k] = { label: settingLabel(k), value: v, key: k };
    }
  }
  return out;
}

function settingRows(
  before: Record<string, unknown> | undefined,
  after: Record<string, unknown> | undefined,
  names: Record<string, string>,
): SettingRow[] {
  const a = before ? flatten(before) : {};
  const b = after ? flatten(after) : {};
  const keys = [...Object.keys(a)];
  for (const k of Object.keys(b)) if (!keys.includes(k)) keys.push(k);
  return keys.map((k) => {
    const label = (b[k] || a[k]).label;
    const raw = (b[k] || a[k]).key;
    const av = a[k]?.value;
    const bv = b[k]?.value;
    const bt = before ? settingText(av, raw, names) : null;
    const at = after ? settingText(bv, raw, names) : null;
    const changed = bt !== at;
    if (isSecretSetting(raw) && (isSecretValue(av) || isSecretValue(bv))) {
      // A password typed into a step: say it is set, and whether it changed.
      const hide = (value: unknown, text: string | null) => (text === null ? null : isSecretValue(value) ? 'Set' : text);
      const hb = hide(av, bt);
      const ha = hide(bv, at);
      return { key: k, label, before: hb, after: changed && hb === 'Set' && ha === 'Set' ? 'Set, a different value' : ha, changed };
    }
    return { key: k, label, before: bt, after: at, changed };
  });
}

function pos(step: any): { x: number; y: number } {
  return { x: Number(step?.position?.x) || 0, y: Number(step?.position?.y) || 0 };
}

function stepRows(a: any, b: any, names: Record<string, string>): SettingRow[] {
  const rows: SettingRow[] = [];
  const nameA = a ? (a.label || '') : null;
  const nameB = b ? (b.label || '') : null;
  if (a && b) {
    if (nameA !== nameB) rows.push({ key: '__name', label: 'Step name', before: nameA || '—', after: nameB || '—', changed: true });
    if (a.type !== b.type) rows.push({ key: '__type', label: 'Step type', before: a.type, after: b.type, changed: true });
  }
  return rows.concat(settingRows(a ? a.params || {} : undefined, b ? b.params || {} : undefined, names));
}

function paramText(p: any, secret = false): string {
  if (!p) return '—';
  const bits = [p.type || 'string', `default ${secret && isSecretValue(p.default) ? 'set' : settingText(p.default)}`];
  if (p.required) bits.push('required');
  return bits.join(', ');
}

function pipelineRows(a: any, b: any): SettingRow[] {
  const rows: SettingRow[] = [];
  const add = (key: string, label: string, before: string, after: string) => {
    if (before !== after) rows.push({ key, label, before, after, changed: true });
  };
  add('name', 'Pipeline name', a?.name || '—', b?.name || '—');
  add('description', 'Description', a?.description || '—', b?.description || '—');
  add('business_purpose', 'Business purpose', a?.business_purpose || '—', b?.business_purpose || '—');
  add('readme', 'README', a?.readme || '—', b?.readme || '—');
  add('tags', 'Tags', settingText(a?.tags || []), settingText(b?.tags || []));
  add('priority', 'Run priority', settingText(a?.metadata?.priority), settingText(b?.metadata?.priority));
  const pa: Record<string, any> = {};
  const pb: Record<string, any> = {};
  for (const p of a?.parameters || []) if (p?.name) pa[p.name] = p;
  for (const p of b?.parameters || []) if (p?.name) pb[p.name] = p;
  const names = [...Object.keys(pa)];
  for (const n of Object.keys(pb)) if (!names.includes(n)) names.push(n);
  for (const n of names) {
    const secret = isSecretSetting(n);
    const ta = pa[n] ? paramText(pa[n], secret) : '—';
    let tb = pb[n] ? paramText(pb[n], secret) : '—';
    if (secret && pa[n] && pb[n] && ta === tb && settingText(pa[n].default) !== settingText(pb[n].default)) {
      tb = `${tb}, a different value`;
    }
    add(`param.${n}`, `Parameter “${n}”`, ta, tb);
  }
  return rows;
}

/** Compare version `a` (the one picked) with version `b` (usually the current one). */
export function diffVersions(a: any, b: any, opts: DiffOptions = {}): VersionDiff {
  const names = opts.names || {};
  const stepsA: Record<string, any> = {};
  const stepsB: Record<string, any> = {};
  for (const s of a?.steps || []) if (s?.id) stepsA[s.id] = s;
  for (const s of b?.steps || []) if (s?.id) stepsB[s.id] = s;

  const ids = Object.keys(stepsB);
  for (const id of Object.keys(stepsA)) if (!ids.includes(id)) ids.push(id);

  const steps: StepDiff[] = ids.map((id) => {
    const sa = stepsA[id];
    const sb = stepsB[id];
    const kind: StepChange = !sb ? 'removed' : !sa ? 'added' : 'same';
    const rows = stepRows(sa, sb, names);
    const changed = kind === 'same' && rows.some((r) => r.changed);
    const pa = sa ? pos(sa) : null;
    const pb = sb ? pos(sb) : null;
    return {
      id,
      kind: changed ? 'changed' : kind,
      type: (sb || sa).type || '',
      name: (sb || sa).label || '',
      rows,
      position: (pb || pa)!,
      moved: !!pa && !!pb && (pa.x !== pb.x || pa.y !== pb.y),
    };
  });
  // Left to right, as the canvas reads.
  steps.sort((x, y) => x.position.x - y.position.x || x.position.y - y.position.y);

  const key = (c: any) => `${c.from_step}>${c.to_step}>${c.from_port || ''}>${c.to_port || ''}`;
  const linksA = new Map<string, any>();
  const linksB = new Map<string, any>();
  for (const c of a?.connections || []) if (c?.from_step && c?.to_step) linksA.set(key(c), c);
  for (const c of b?.connections || []) if (c?.from_step && c?.to_step) linksB.set(key(c), c);
  const links: LinkDiff[] = [];
  for (const [k, c] of linksB) links.push({ from: c.from_step, to: c.to_step, kind: linksA.has(k) ? 'same' : 'added' });
  for (const [k, c] of linksA) if (!linksB.has(k)) links.push({ from: c.from_step, to: c.to_step, kind: 'removed' });

  const pipeline = pipelineRows(a, b);
  const count = (k: StepChange) => steps.filter((s) => s.kind === k).length;
  const added = count('added');
  const removed = count('removed');
  const changed = count('changed');
  const linksChanged = links.some((l) => l.kind !== 'same');
  const nothing = added + removed + changed === 0 && !linksChanged && pipeline.length === 0;
  const moved = steps.some((s) => s.moved);
  return {
    steps,
    links,
    pipeline,
    added,
    removed,
    changed,
    same: count('same'),
    linksChanged,
    identical: nothing && !moved,
    layoutOnly: nothing && moved,
  };
}
