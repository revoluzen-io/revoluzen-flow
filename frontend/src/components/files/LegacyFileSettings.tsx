/**
 * File step settings for mainframe and older files, and for PGP.
 *
 *   FixedWidthSettings  columns by start and length (or from a list of widths)
 *   CopybookSettings    a COBOL copybook, its code page and record format,
 *                       a "Check copybook" field table and REDEFINES rules
 *   PgpSettings         decrypt (File source) or encrypt (File destination)
 *                       with a key kept in Credentials; check the sender's
 *                       signature, or sign with your own key
 *
 * Rendered by FileNodeConfig in ConfigPanel.tsx. The backend side is
 * nodes/file_node.py, legacy_files/ and openpgp/.
 */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../../api/client';
import {
  BAD_VALUE_LABELS, COLUMN_TYPE_LABELS, COPYBOOK_CODE_PAGES, ENCODING_LABELS, RECORD_FORMAT_LABELS,
  TRIM_LABELS, columnProblems, columnsFromWidths, encryptedName, isPgpCredential, nextStart,
  type FixedColumn, type PgpPurpose, type RedefinesRule,
} from './legacyFileOptions';

type Params = Record<string, any>;
type SetParams = (patch: Params) => void;

const inputClass = 'w-full px-3 py-2 text-sm text-slate-800 bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-pipe-400 placeholder:text-slate-500';
const smallInput = 'w-full px-2 py-1.5 text-xs text-slate-800 bg-white border border-slate-300 rounded-md focus:outline-none focus:ring-2 focus:ring-pipe-300 placeholder:text-slate-400';
const buttonClass = 'px-2.5 py-1 text-xs font-medium rounded-md border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed';

function Label({ children }: { children: React.ReactNode }) {
  return <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5">{children}</label>;
}

function Hint({ children }: { children: React.ReactNode }) {
  return <p className="mt-1 text-[11px] leading-snug text-slate-500">{children}</p>;
}

function Choice({ value, onChange, labels, ariaLabel }: {
  value: string; onChange: (v: string) => void; labels: Record<string, string>; ariaLabel?: string;
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)} className={inputClass} aria-label={ariaLabel}>
      {Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
    </select>
  );
}

function Switch({ value, onChange, label }: { value: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={value} onClick={() => onChange(!value)}
      className="flex items-center gap-2.5 text-left">
      <span className={`w-10 h-5 rounded-full transition-colors relative shrink-0 ${value ? 'bg-pipe-600' : 'bg-slate-300'}`}>
        <span className={`w-4 h-4 bg-white rounded-full absolute top-0.5 transition-transform shadow-sm ${value ? 'translate-x-5' : 'translate-x-0.5'}`} />
      </span>
      <span className="text-sm text-slate-700">{label}</span>
    </button>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-3 space-y-3">
      <div className="text-xs font-semibold text-slate-800">{title}</div>
      {children}
    </div>
  );
}

/** Records, spaces, empty text and bad values: shared by both formats. */
function RecordOptions({ params, set, copybook }: { params: Params; set: SetParams; copybook: boolean }) {
  const recordFormat = params.record_format || (copybook ? 'fixed' : 'lines');
  return (
    <>
      <div>
        <Label>Records</Label>
        <Choice value={recordFormat} onChange={(v) => set({ record_format: v })} labels={RECORD_FORMAT_LABELS} ariaLabel="Records" />
        <Hint>
          A file copied from a mainframe in binary has no line breaks: use fixed length, or RDW for variable-length records.
        </Hint>
      </div>
      {recordFormat === 'fixed' && (
        <div>
          <Label>Record length (bytes)</Label>
          <input type="number" min={0} className={inputClass} aria-label="Record length"
            value={params.record_length ?? 0}
            onChange={(e) => set({ record_length: e.target.value === '' ? 0 : Number(e.target.value) })} />
          <Hint>{copybook ? '0 = the length the copybook describes.' : 'Bytes in each record.'}</Hint>
        </div>
      )}
      <div>
        <Label>Spaces in text</Label>
        <Choice value={params.text_trim || (copybook ? 'right' : 'both')} onChange={(v) => set({ text_trim: v })} labels={TRIM_LABELS} ariaLabel="Spaces in text" />
      </div>
      <Switch value={params.empty_as_null !== false} onChange={(v) => set({ empty_as_null: v })} label="Empty text becomes NULL" />
      <div>
        <Label>When a value cannot be read</Label>
        <Choice value={params.bad_value_rule || 'stop'} onChange={(v) => set({ bad_value_rule: v })} labels={BAD_VALUE_LABELS} ariaLabel="When a value cannot be read" />
      </div>
    </>
  );
}

// ── Fixed-width text ──

export function FixedWidthSettings({ params, set }: { params: Params; set: SetParams }) {
  const columns: FixedColumn[] = Array.isArray(params.fixed_columns) ? params.fixed_columns : [];
  const [widths, setWidths] = useState<string>(params.fixed_widths || '');
  const [names, setNames] = useState<string>(params.fixed_names || '');
  const [widthError, setWidthError] = useState<string>('');
  const problems = columnProblems(columns);

  const update = (index: number, patch: Partial<FixedColumn>) =>
    set({ fixed_columns: columns.map((c, i) => (i === index ? { ...c, ...patch } : c)) });
  const remove = (index: number) => set({ fixed_columns: columns.filter((_, i) => i !== index) });
  const add = () => set({
    fixed_columns: [...columns, { name: `column_${columns.length + 1}`, start: nextStart(columns), length: 10, type: 'text' }],
  });
  const fill = () => {
    const result = columnsFromWidths(widths, names);
    setWidthError(result.error || '');
    if (!result.error) set({ fixed_columns: result.columns, fixed_widths: widths, fixed_names: names });
  };

  return (
    <Section title="Fixed-width columns">
      <div className="space-y-1.5" data-testid="fixed-width-columns">
        {columns.length > 0 && (
          <div className="grid grid-cols-[1fr_52px_52px_1fr_20px] gap-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
            <span>Name</span><span>Start</span><span>Length</span><span>Type</span><span />
          </div>
        )}
        {columns.map((column, index) => (
          <div key={index} className="space-y-1">
            <div className="grid grid-cols-[1fr_52px_52px_1fr_20px] gap-1 items-center">
              <input className={smallInput} aria-label={`Column ${index + 1} name`} value={column.name ?? ''}
                onChange={(e) => update(index, { name: e.target.value })} />
              <input className={smallInput} type="number" min={1} aria-label={`Column ${index + 1} start`} value={column.start ?? ''}
                onChange={(e) => update(index, { start: Number(e.target.value) })} />
              <input className={smallInput} type="number" min={1} aria-label={`Column ${index + 1} length`} value={column.length ?? ''}
                onChange={(e) => update(index, { length: Number(e.target.value) })} />
              <select className={smallInput} aria-label={`Column ${index + 1} type`} value={column.type || 'text'}
                onChange={(e) => update(index, { type: e.target.value })}>
                {Object.entries(COLUMN_TYPE_LABELS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
              </select>
              <button type="button" onClick={() => remove(index)} title={`Remove ${column.name || 'column'}`}
                className="text-slate-400 hover:text-red-500 text-sm leading-none">×</button>
            </div>
            {(column.type === 'decimal' || column.type === 'implied') && (
              <div className="flex items-center gap-2 pl-1 text-[11px] text-slate-600">
                <span>Decimal places</span>
                <input className={`${smallInput} w-16`} type="number" min={0} max={37} aria-label={`Column ${index + 1} decimal places`}
                  value={column.decimals ?? 0} onChange={(e) => update(index, { decimals: Number(e.target.value) })} />
                <span className="text-slate-400">{column.type === 'implied' ? '0001234 with 2 = 12.34' : '1234.50 with 2'}</span>
              </div>
            )}
            {column.type === 'date' && (
              <div className="flex items-center gap-2 pl-1 text-[11px] text-slate-600">
                <span>Date format</span>
                <input className={`${smallInput} w-28`} placeholder="YYYYMMDD" aria-label={`Column ${index + 1} date format`}
                  value={column.format ?? ''} onChange={(e) => update(index, { format: e.target.value })} />
                <span className="text-slate-400">YYYY YY MM DD DDD HH MI SS</span>
              </div>
            )}
          </div>
        ))}
        <button type="button" className={buttonClass} onClick={add}>Add column</button>
        {problems.length > 0 && (
          <ul className="text-[11px] text-red-600 list-disc pl-4">{problems.map((p) => <li key={p}>{p}</li>)}</ul>
        )}
      </div>
      <div className="border-t border-slate-200 pt-2.5">
        <Label>Or make the columns from a list of widths</Label>
        <input className={inputClass} placeholder="8, 30, 12" value={widths} aria-label="Widths"
          onChange={(e) => setWidths(e.target.value)} />
        <input className={`${inputClass} mt-1.5`} placeholder="Names (optional): customer_id, name, balance" value={names}
          aria-label="Names for the widths" onChange={(e) => setNames(e.target.value)} />
        <div className="mt-1.5 flex items-center gap-2">
          <button type="button" className={buttonClass} onClick={fill}>Make columns</button>
          {widthError && <span className="text-[11px] text-red-600">{widthError}</span>}
        </div>
        <Hint>Start positions count from 1. Make columns replaces the list above; then set each column's type.</Hint>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div>
          <Label>Skip lines at start</Label>
          <input type="number" min={0} className={inputClass} aria-label="Lines to skip at the start"
            value={params.skip_lines_start ?? 0} onChange={(e) => set({ skip_lines_start: Number(e.target.value) || 0 })} />
        </div>
        <div>
          <Label>Skip lines at end</Label>
          <input type="number" min={0} className={inputClass} aria-label="Lines to skip at the end"
            value={params.skip_lines_end ?? 0} onChange={(e) => set({ skip_lines_end: Number(e.target.value) || 0 })} />
        </div>
      </div>
      <Hint>For header and trailer lines. They are skipped in every file.</Hint>
      <div>
        <Label>Decimal point</Label>
        <Choice value={params.decimal_separator || '.'} onChange={(v) => set({ decimal_separator: v })}
          labels={{ '.': 'Point (1,234.50)', ',': 'Comma (1.234,50)' }} ariaLabel="Decimal point" />
      </div>
      <RecordOptions params={params} set={set} copybook={false} />
    </Section>
  );
}

// ── COBOL copybook ──

interface LayoutField { column: string; cobol_name: string; start: number; length: number; type: string; repeats: boolean; in_redefines: boolean }
interface LayoutResult {
  ok: boolean; error?: string; record_length?: number; column_count?: number; fields?: LayoutField[];
  redefines?: { group: number; start: number; branches: string[] }[]; selector_fields?: string[];
  variable_tables?: { name: string; depending_on: string; max: number }[];
}

const COPYBOOK_EXAMPLE = `       01  CUSTOMER-RECORD.
           05  REC-TYPE        PIC X.
           05  CUST-ID         PIC 9(6).
           05  BALANCE         PIC S9(7)V99 COMP-3.`;

export function CopybookSettings({ params, set }: { params: Params; set: SetParams }) {
  const [layout, setLayout] = useState<LayoutResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [showFields, setShowFields] = useState(true);
  const rules: RedefinesRule[] = Array.isArray(params.redefines_rules) ? params.redefines_rules : [];
  const branches = useMemo(() => (layout?.redefines || []).flatMap((g) => g.branches), [layout]);
  const selectors = layout?.selector_fields || [];

  const check = async () => {
    setChecking(true);
    try {
      const result = await api.post<LayoutResult>('/legacy-files/copybook-layout', {
        copybook: params.copybook || '', code_page: params.code_page || 'cp037',
        column_names: params.column_names || 'underscores', redefines_rules: rules,
      });
      setLayout(result);
      setShowFields(true);
    } catch (err: any) {
      setLayout({ ok: false, error: err?.message || 'The copybook could not be checked.' });
    } finally {
      setChecking(false);
    }
  };

  const updateRule = (index: number, patch: Partial<RedefinesRule>) =>
    set({ redefines_rules: rules.map((r, i) => (i === index ? { ...r, ...patch } : r)) });

  return (
    <Section title="COBOL copybook">
      <div>
        <Label>Copybook</Label>
        <textarea rows={10} spellCheck={false} aria-label="Copybook"
          className={`${inputClass} font-mono text-xs leading-snug resize-y`}
          placeholder={COPYBOOK_EXAMPLE} value={params.copybook || ''}
          onChange={(e) => set({ copybook: e.target.value })} />
        <Hint>Paste the record layout (the 01 level and the fields under it). Sequence numbers in columns 1 to 6 are fine.</Hint>
      </div>
      <div>
        <Label>Or: copybook file</Label>
        <input className={inputClass} placeholder="layouts/CUSTMAST.cpy" value={params.copybook_path || ''}
          aria-label="Copybook file" onChange={(e) => set({ copybook_path: e.target.value })} />
        <Hint>Used when the box above is empty. The file is read when the step runs.</Hint>
      </div>
      <div className="flex items-center gap-2">
        <button type="button" className={buttonClass} onClick={check} disabled={checking || !(params.copybook || '').trim()}>
          {checking ? 'Checking...' : 'Check copybook'}
        </button>
        {layout?.ok && (
          <span className="text-[11px] text-slate-600">
            {layout.column_count} columns, records of {layout.record_length} bytes
          </span>
        )}
      </div>
      {layout && !layout.ok && (
        <div role="alert" className="text-xs text-red-700 bg-red-50 border border-red-200 rounded-md px-2.5 py-2">{layout.error}</div>
      )}
      {layout?.ok && (
        <div className="rounded-md border border-slate-200 bg-white">
          <button type="button" onClick={() => setShowFields((v) => !v)}
            className="w-full text-left px-2.5 py-1.5 text-[11px] font-semibold text-slate-700">
            {showFields ? 'Hide' : 'Show'} the fields
          </button>
          {showFields && (
            <div className="max-h-56 overflow-auto border-t border-slate-100" data-testid="copybook-fields">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-slate-100 text-slate-600">
                  <tr><th className="text-left px-2 py-1">Column</th><th className="text-right px-2 py-1">Start</th><th className="text-right px-2 py-1">Bytes</th><th className="text-left px-2 py-1">Read as</th></tr>
                </thead>
                <tbody>
                  {(layout.fields || []).map((f) => (
                    <tr key={f.column} className="border-t border-slate-100">
                      <td className="px-2 py-0.5 font-mono">{f.column}</td>
                      <td className="px-2 py-0.5 text-right">{f.start}</td>
                      <td className="px-2 py-0.5 text-right">{f.length}</td>
                      <td className="px-2 py-0.5 text-slate-600">{f.type}{f.in_redefines ? ' (REDEFINES)' : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {(layout.variable_tables || []).length > 0 && (
            <div className="px-2.5 py-1.5 border-t border-slate-100 text-[11px] text-slate-600">
              {(layout.variable_tables || []).map((t) => `${t.name} repeats up to ${t.max} times, as many as ${t.depending_on} says.`).join(' ')}
            </div>
          )}
        </div>
      )}
      <div>
        <Label>Code page</Label>
        <select className={inputClass} value={params.code_page || 'cp037'} aria-label="Code page"
          onChange={(e) => set({ code_page: e.target.value })}>
          {COPYBOOK_CODE_PAGES.map((cp) => <option key={cp} value={cp}>{ENCODING_LABELS[cp] || cp}</option>)}
        </select>
        <Hint>EBCDIC for a file copied from the mainframe in binary. Latin-1 if it was already converted to ASCII (then it must not hold COMP or COMP-3 fields).</Hint>
      </div>
      <div className="space-y-1.5">
        <Label>REDEFINES rules</Label>
        {rules.length === 0 && (
          <Hint>Without a rule every REDEFINES part is read for every record. Add a rule when a field such as a record type says which part a record uses.</Hint>
        )}
        <datalist id="copybook-branches">{branches.map((b) => <option key={b} value={b} />)}</datalist>
        <datalist id="copybook-selectors">{selectors.map((s) => <option key={s} value={s} />)}</datalist>
        {rules.map((rule, index) => (
          <div key={index} className="flex flex-wrap items-center gap-1 text-[11px] text-slate-600">
            <span>Read</span>
            <input className={`${smallInput} w-32`} list="copybook-branches" placeholder="PAYMENT-INFO" value={rule.branch}
              aria-label={`Rule ${index + 1} part`} onChange={(e) => updateRule(index, { branch: e.target.value })} />
            <span>when</span>
            <input className={`${smallInput} w-28`} list="copybook-selectors" placeholder="REC-TYPE" value={rule.field}
              aria-label={`Rule ${index + 1} field`} onChange={(e) => updateRule(index, { field: e.target.value })} />
            <span>is</span>
            <input className={`${smallInput} w-20`} placeholder="P" value={rule.values}
              aria-label={`Rule ${index + 1} values`} onChange={(e) => updateRule(index, { values: e.target.value })} />
            <button type="button" className="text-slate-400 hover:text-red-500" title="Remove rule"
              onClick={() => set({ redefines_rules: rules.filter((_, i) => i !== index) })}>×</button>
          </div>
        ))}
        <button type="button" className={buttonClass}
          onClick={() => set({ redefines_rules: [...rules, { branch: '', field: '', values: '' }] })}>Add rule</button>
        {rules.length > 0 && <Hint>Several values: separate them with commas. A record no rule matches reads the parts that have no rule.</Hint>}
      </div>
      <RecordOptions params={params} set={set} copybook />
      <details className="text-xs">
        <summary className="cursor-pointer font-semibold text-slate-700">Number formats and column names</summary>
        <div className="mt-2 space-y-2.5">
          <div>
            <Label>Binary numbers (COMP)</Label>
            <Choice value={params.binary_byte_order || 'big'} onChange={(v) => set({ binary_byte_order: v })}
              labels={{ big: 'Mainframe (big-endian)', little: 'PC (little-endian, COMP-5)' }} ariaLabel="Binary numbers" />
          </div>
          <div>
            <Label>Floating point (COMP-1, COMP-2)</Label>
            <Choice value={params.float_format || 'ibm'} onChange={(v) => set({ float_format: v })}
              labels={{ ibm: 'IBM hexadecimal (mainframe)', ieee: 'IEEE (PC)' }} ariaLabel="Floating point" />
          </div>
          <div>
            <Label>Column names</Label>
            <Choice value={params.column_names || 'underscores'} onChange={(v) => set({ column_names: v })}
              labels={{ underscores: 'CUST_ID (hyphens become underscores)', as_written: 'CUST-ID (as in the copybook)' }} ariaLabel="Column names" />
          </div>
        </div>
      </details>
    </Section>
  );
}

// ── PGP ──

interface KeySummary {
  user_ids: string[]; fingerprint: string; algorithm: string; encrypts_with: string; problem: string;
  has_private_key: boolean; signs_with?: string; sign_problem?: string;
}
interface KeyCheck { ok: boolean; error?: string; keys?: KeySummary[]; unlocks?: boolean }
type Credential = { id: string; name: string; type?: string; config?: Record<string, unknown> };

const isOn = (value: unknown) => value === true || value === 'true';

/** A credential picker with its own "Check key" button and result. */
function PgpKeyPicker({ purpose, label, hint, value, onChange, credentials }: {
  purpose: PgpPurpose; label: string; hint: string; value: string; onChange: (id: string) => void;
  credentials: Credential[];
}) {
  const [check, setCheck] = useState<KeyCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const usable = credentials.filter((c) => isPgpCredential(c, purpose));
  const runCheck = async () => {
    setChecking(true);
    try {
      setCheck(await api.post<KeyCheck>('/legacy-files/pgp-key-check', { credential_id: value, purpose }));
    } catch (err: any) {
      setCheck({ ok: false, error: err?.message || 'The key could not be checked.' });
    } finally {
      setChecking(false);
    }
  };
  const unlocks = (purpose === 'decrypt' || purpose === 'sign') && check?.unlocks;
  return (
    <>
      <div>
        <Label>{label}</Label>
        <select className={inputClass} aria-label={label} value={value}
          onChange={(e) => { onChange(e.target.value); setCheck(null); }}>
          <option value="">Choose a credential</option>
          {usable.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <Hint>
          {hint}
          {usable.length === 0 && ' No PGP key credential yet: add one on the Credentials page.'}
        </Hint>
      </div>
      <div className="flex items-center gap-2">
        <button type="button" className={buttonClass} disabled={!value || checking} onClick={runCheck}>
          {checking ? 'Checking...' : 'Check key'}
        </button>
      </div>
      {check && !check.ok && (
        <div role="alert" className="text-xs text-red-700 bg-red-50 border border-red-200 rounded-md px-2.5 py-2">{check.error}</div>
      )}
      {check?.ok && (check.keys || []).map((key) => (
        <div key={key.fingerprint} className="text-[11px] text-slate-700 bg-white border border-slate-200 rounded-md px-2.5 py-2 space-y-0.5" data-testid="pgp-key-summary">
          <div className="font-semibold">{key.user_ids.join(', ') || 'Key without a name'}</div>
          <div className="font-mono text-slate-500">{key.fingerprint}</div>
          <div>{key.algorithm}{unlocks ? ' · the passphrase unlocks it' : ''}</div>
          {(purpose === 'verify' || purpose === 'sign') && key.signs_with && (
            <div>Signs with <span className="font-mono">{key.signs_with}</span></div>
          )}
          {purpose === 'encrypt' && key.problem && <div className="text-red-600">{key.problem}</div>}
          {(purpose === 'verify' || purpose === 'sign') && key.sign_problem && <div className="text-red-600">{key.sign_problem}</div>}
        </div>
      ))}
    </>
  );
}

export function PgpSettings({ params, set, purpose, filePath }: {
  params: Params; set: SetParams; purpose: 'decrypt' | 'encrypt'; filePath?: string;
}) {
  const enabledKey = purpose === 'decrypt' ? 'pgp_decrypt' : 'pgp_encrypt';
  const enabled = isOn(params[enabledKey]);
  const [credentials, setCredentials] = useState<Credential[]>([]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    api.listCredentials()
      .then((rows) => { if (!cancelled) setCredentials(Array.isArray(rows) ? rows : []); })
      .catch(() => { if (!cancelled) setCredentials([]); });
    return () => { cancelled = true; };
  }, [enabled]);

  const verifying = purpose === 'decrypt' && isOn(params.pgp_verify);
  const signing = purpose === 'encrypt' && isOn(params.pgp_sign);

  return (
    <div className="space-y-2.5">
      <Switch value={enabled} onChange={(v) => set({ [enabledKey]: v })}
        label={purpose === 'decrypt' ? 'Decrypt with PGP' : 'Encrypt with PGP'} />
      {enabled && (
        <Section title={purpose === 'decrypt' ? 'PGP decryption' : 'PGP encryption'}>
          <PgpKeyPicker purpose={purpose} label="PGP key credential" credentials={credentials}
            value={params.pgp_credential_id || ''} onChange={(id) => set({ pgp_credential_id: id })}
            hint={purpose === 'decrypt'
              ? 'Your private key and its passphrase, kept encrypted in Credentials (category PGP key, fields private_key and passphrase).'
              : 'The partner’s public key, kept in Credentials (category PGP key, field public_key).'} />
          {purpose === 'encrypt' && (
            <>
              <Switch value={isOn(params.pgp_armor)} onChange={(v) => set({ pgp_armor: v })}
                label="Armored text (.asc) instead of binary (.pgp)" />
              {filePath && (
                <Hint>The file is saved as <span className="font-mono">{encryptedName(filePath, isOn(params.pgp_armor))}</span>. Only the encrypted file is kept.</Hint>
              )}
            </>
          )}
          {purpose === 'decrypt' && (
            <Hint>The file is decrypted into a private temporary folder, read, and the decrypted copy is removed. Set Format to what is inside, or leave Auto for names like orders.csv.pgp.</Hint>
          )}
          <div className="pt-3 border-t border-slate-200 space-y-2.5">
            {purpose === 'decrypt' ? (
              <>
                <Switch value={verifying} onChange={(v) => set({ pgp_verify: v })} label="Check the sender’s signature" />
                <Hint>The run stops unless the file was signed with the sender’s key and has not been changed since. A file that is signed but not encrypted is read this way too.</Hint>
                {verifying && (
                  <PgpKeyPicker purpose="verify" label="Sender’s public key" credentials={credentials}
                    value={params.pgp_verify_credential_id || ''} onChange={(id) => set({ pgp_verify_credential_id: id })}
                    hint="The sender’s public key, kept in Credentials (field public_key). It can be the credential above if that one holds their public key too." />
                )}
              </>
            ) : (
              <>
                <Switch value={signing} onChange={(v) => set({ pgp_sign: v })} label="Sign with your key" />
                <Hint>The partner can then check that the file came from you and was not changed on the way.</Hint>
                {signing && (
                  <PgpKeyPicker purpose="sign" label="Your signing key" credentials={credentials}
                    value={params.pgp_sign_credential_id || ''} onChange={(id) => set({ pgp_sign_credential_id: id })}
                    hint="Your private key and its passphrase, kept encrypted in Credentials (fields private_key and passphrase). It can be the credential above if that one holds your private key too." />
                )}
              </>
            )}
          </div>
        </Section>
      )}
    </div>
  );
}
