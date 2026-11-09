/**
 * ClarifyCard — Phase 3.1 (May 18 2026).
 *
 * Renders a structured clarification ask from /api/planner/chat's clarify_first
 * response: where the data comes from, where it goes, and anything else worth
 * asking before a draft. Each question shows its options as one-click chips,
 * listed by the backend from this workspace's real connections, uploaded files
 * and managed tables. The user picks one per question (or types their own), then
 * clicks Send.
 *
 * Backend response shape (see clarification_payload in ai/clarify_draft.py):
 *   {
 *     "intent": "clarify_first",
 *     "clarification": {
 *       "source_type": "postgres",
 *       "sink_type": "snowflake",
 *       "detected_intent": "Build a pipeline from PostgreSQL to Snowflake",
 *       "questions": [
 *         { field, question, chips: string[], required, slot?, label?, options? },
 *         ...
 *       ]
 *     }
 *   }
 *
 * A missing connection can be created right here: the "New connection" chip
 * opens InlineConnectionForm, and what is saved becomes the selected answer.
 *
 * Trust contract: Send produces a sentence for the transcript and a `choices`
 * object for the planner, both holding ids and names only. Nothing typed into
 * the connection form can reach either one, because the form hands back only
 * the new connection's id, name and type. The backend looks every id up again
 * in the caller's workspace.
 */

import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { api } from '../../api/client';
import type { CreatedConnection } from './InlineConnectionForm';

// Loaded when opened: it pulls in the Connections page's connector tables, and
// most cards are answered without ever needing them.
const InlineConnectionForm = lazy(() => import('./InlineConnectionForm'));

export interface ClarifyOption {
  id: string;
  label: string;
  kind: 'connection' | 'file' | 'keep_file' | 'table' | 'new_table' | 'new_connection' | 'answer';
  recommended?: boolean;
  connection_id?: string;
  connection_name?: string;
  connection_type?: string;
  file_id?: string;
  file_name?: string;
  schema_name?: string;
  table_name?: string;
  new_connection_type?: string;
  new_connection_label?: string;
}

export interface ClarifyQuestion {
  field: string;
  question: string;
  chips: string[];
  required: boolean;
  /** source | destination | source_table | destination_table | write_mode | merge_key */
  slot?: string;
  label?: string;
  options?: ClarifyOption[];
  connection_id?: string;
  connection_name?: string;
  table_hint?: string;
}

export interface ClarificationPayload {
  source_type: string | null;
  sink_type: string | null;
  detected_intent: string;
  question_count: number;
  questions: ClarifyQuestion[];
  /** The write mode the request already stated. */
  write_mode?: string;
  merge_on?: string[];
  table_connection_types?: string[];
  write_mode_connection_types?: string[];
}

/** What the planner receives. Ids and names only. */
export interface ClarifyChoices {
  source?: Record<string, unknown>;
  destination?: Record<string, unknown>;
  source_table?: { table: string; schema: string };
  destination_table?: { table: string; schema: string };
  write_mode?: string;
  merge_on?: string[];
  answers?: Record<string, string>;
}

export interface ClarifyCardProps {
  /** Structured clarification data from the /api/planner/chat response. */
  payload: ClarificationPayload;
  /**
   * Called when the user clicks Send, with the sentence for the transcript and
   * the structured choices. The chat panel posts both as the next message.
   */
  onSubmit: (answerText: string, choices: ClarifyChoices) => void;
  /** Optional cancel — closes the card without sending. */
  onCancel?: () => void;
  busy?: boolean;
}

interface Answer {
  /** The chip picked. */
  optionId?: string;
  /** Typed instead of picking a chip. Wins when non-empty. */
  text?: string;
  table?: string;
  schema?: string;
  newTableName?: string;
  writeMode?: string;
  mergeKey?: string;
}

const WRITE_MODE_VALUES = ['append', 'replace', 'merge'];
const WRITE_MODE_LABELS: Record<string, string> = {
  append: 'Add new rows to the table',
  replace: 'Replace the whole table',
  merge: 'Update rows that match',
};
const WRITE_MODE_HINTS: Record<string, string> = {
  append: 'The table must already exist.',
  replace: 'Everything in the table is replaced on every run.',
};

const MAX_TABLE_CHIPS = 6;

const chipClass = (selected: boolean) =>
  `px-2.5 py-1 text-xs rounded-md transition-colors disabled:opacity-50 text-left ${
    selected
      ? 'bg-indigo-600 text-white border border-indigo-700'
      : 'bg-slate-50 text-slate-700 border border-slate-200 hover:bg-slate-100'
  }`;
const inputClass =
  'w-full text-xs px-2 py-1 rounded-md border border-slate-200 focus:border-indigo-400 focus:outline-none focus:ring-1 focus:ring-indigo-200 disabled:opacity-50';
const subLabelClass = 'text-xs font-semibold text-slate-700 mb-1';

function isSlotQuestion(q: ClarifyQuestion) {
  return q.slot === 'source' || q.slot === 'destination';
}

export default function ClarifyCard({ payload, onSubmit, onCancel, busy }: ClarifyCardProps) {
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  // The question whose "New connection" form is open, if any.
  const [formField, setFormField] = useState<string | null>(null);
  // Connections saved on this card, shown as chips like the rest.
  const [created, setCreated] = useState<Record<string, ClarifyOption[]>>({});

  const tableTypes = payload.table_connection_types ?? [];
  const writeModeTypes = payload.write_mode_connection_types ?? [];

  const update = (field: string, patch: Answer) =>
    setAnswers((a) => ({ ...a, [field]: { ...a[field], ...patch } }));

  const optionsFor = (q: ClarifyQuestion): ClarifyOption[] => {
    const listed: ClarifyOption[] = q.options ?? q.chips.map((chip) => ({ id: chip, label: chip, kind: 'answer' }));
    const added = created[q.field] ?? [];
    if (added.length === 0) return listed;
    // New ones sit ahead of the chip that made them, which stays last.
    const newChip = listed.findIndex((o) => o.kind === 'new_connection');
    return newChip < 0 ? [...listed, ...added] : [...listed.slice(0, newChip), ...added, ...listed.slice(newChip)];
  };

  const selectedOption = (q: ClarifyQuestion): ClarifyOption | undefined => {
    const answer = answers[q.field];
    if (!answer?.optionId || answer.text?.trim()) return undefined;
    return optionsFor(q).find((o) => o.id === answer.optionId);
  };

  const needsTable = (option?: ClarifyOption) =>
    !!option && option.kind === 'connection' && tableTypes.includes(option.connection_type ?? '');

  const asksWriteMode = (q: ClarifyQuestion, option?: ClarifyOption) => {
    if (q.slot !== 'destination' || !option) return false;
    if (option.kind === 'table') return true;
    return option.kind === 'connection' && writeModeTypes.includes(option.connection_type ?? '');
  };

  const needsMergeKey = (mode: string) => mode === 'merge' && !payload.merge_on?.length;

  const answerFor = (q: ClarifyQuestion, option: ClarifyOption): Answer => ({
    optionId: option.id,
    // A destination table can be new, so the name the request suggests is a
    // fair starting point. A source table has to exist: its picker checks.
    table: q.slot === 'destination' && needsTable(option) ? (q.table_hint ?? '') : '',
    schema: '',
    newTableName: option.kind === 'new_table' ? (option.table_name ?? '') : '',
  });

  const pickOption = (q: ClarifyQuestion, option: ClarifyOption) => {
    if (option.kind === 'new_connection') {
      setFormField(q.field);
      return;
    }
    setAnswers((a) => ({ ...a, [q.field]: answerFor(q, option) }));
  };

  const handleSaved = (q: ClarifyQuestion, saved: CreatedConnection) => {
    const option: ClarifyOption = {
      id: `connection:${saved.id}`,
      label: saved.name,
      kind: 'connection',
      connection_id: saved.id,
      connection_name: saved.name,
      connection_type: saved.type,
    };
    setCreated((c) => ({ ...c, [q.field]: [...(c[q.field] ?? []), option] }));
    setFormField(null);
    setAnswers((a) => ({ ...a, [q.field]: answerFor(q, option) }));
  };

  const isAnswered = (q: ClarifyQuestion): boolean => {
    const answer = answers[q.field] ?? {};
    if (q.slot === 'source_table') {
      return !!answer.table?.trim();
    }
    if (q.slot === 'destination_table') {
      return !!(answer.table ?? q.table_hint ?? '').trim();
    }
    if (q.slot === 'write_mode') {
      return !!answer.writeMode && (!needsMergeKey(answer.writeMode) || !!answer.mergeKey?.trim());
    }
    if (q.slot === 'merge_key') {
      return !!answer.mergeKey?.trim();
    }
    if (answer.text?.trim()) return true;
    const option = selectedOption(q);
    if (!option) return false;
    if (!isSlotQuestion(q)) return true;
    if (needsTable(option) && !answer.table?.trim()) return false;
    if (option.kind === 'new_table' && !answer.newTableName?.trim()) return false;
    if (asksWriteMode(q, option)) {
      const mode = payload.write_mode || answer.writeMode;
      if (!mode) return false;
      if (needsMergeKey(mode) && !answer.mergeKey?.trim()) return false;
    }
    return true;
  };

  const allRequiredAnswered = payload.questions.every((q) => !q.required || isAnswered(q));

  const handleSend = () => {
    if (!allRequiredAnswered || busy) return;
    const { text, choices } = buildAnswer(payload, answers, selectedOption, needsTable, asksWriteMode);
    onSubmit(text, choices);
  };

  return (
    <div className="rounded-2xl bg-white ring-1 ring-indigo-200 shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b border-indigo-100 bg-indigo-50/40">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs font-bold uppercase tracking-wider px-2 py-0.5 rounded-md bg-indigo-100 text-indigo-800">
            Clarify
          </span>
          <h3 className="text-sm font-semibold text-slate-800">
            {payload.detected_intent}
          </h3>
        </div>
        <p className="mt-1 text-xs text-slate-600">
          A few quick questions before I draft it. Pick an option or type your own answer.
        </p>
      </div>

      <div className="px-4 py-3 space-y-4">
        {payload.questions.map((q, i) => {
          const answer = answers[q.field] ?? {};
          const option = selectedOption(q);
          const formOpen = formField === q.field;
          const newChip = optionsFor(q).find((o) => o.kind === 'new_connection');
          return (
            <div key={q.field}>
              <div className="text-xs font-semibold text-slate-700 mb-1.5">
                Q{i + 1}. {q.question}
                {!q.required && (
                  <span className="ml-1.5 text-[10px] font-normal text-slate-500 uppercase tracking-wide">
                    optional
                  </span>
                )}
              </div>

              {q.slot === 'source_table' ? (
                <TablePicker
                  connectionId={q.connection_id ?? ''}
                  connectionName={q.connection_name ?? ''}
                  hint={q.table_hint}
                  table={answer.table ?? ''}
                  schema={answer.schema ?? ''}
                  disabled={busy}
                  onChange={(table, schema) => update(q.field, { table, schema })}
                />
              ) : q.slot === 'destination_table' ? (
                <input
                  type="text"
                  aria-label={q.question}
                  value={answer.table ?? q.table_hint ?? ''}
                  onChange={(e) => update(q.field, { table: e.target.value })}
                  placeholder="Type a table name…"
                  disabled={busy}
                  className={inputClass}
                />
              ) : q.slot === 'write_mode' ? (
                <WriteModeChips
                  mode={answer.writeMode ?? ''}
                  mergeKey={answer.mergeKey ?? ''}
                  askKey={needsMergeKey(answer.writeMode ?? '')}
                  disabled={busy}
                  onMode={(writeMode) => update(q.field, { writeMode })}
                  onKey={(mergeKey) => update(q.field, { mergeKey })}
                />
              ) : q.slot === 'merge_key' ? (
                <input
                  type="text"
                  aria-label={q.question}
                  value={answer.mergeKey ?? ''}
                  onChange={(e) => update(q.field, { mergeKey: e.target.value })}
                  placeholder="customer_id"
                  disabled={busy}
                  className={inputClass}
                />
              ) : formOpen ? (
                <Suspense fallback={<div className="text-xs text-slate-500">Opening the form…</div>}>
                  <InlineConnectionForm
                    presetType={newChip?.new_connection_type}
                    role={q.slot === 'destination' ? 'destination' : 'source'}
                    onSaved={(saved) => handleSaved(q, saved)}
                    onCancel={() => setFormField(null)}
                  />
                </Suspense>
              ) : (
                <>
                  <div className="flex flex-wrap gap-1.5 mb-1.5">
                    {optionsFor(q).map((o) => (
                      <button
                        key={o.id}
                        type="button"
                        onClick={() => pickOption(q, o)}
                        disabled={busy}
                        aria-pressed={answer.optionId === o.id && !answer.text?.trim()}
                        className={chipClass(answer.optionId === o.id && !answer.text?.trim())}
                      >
                        {o.label}
                        {o.recommended && (
                          <span className="ml-1.5 text-[10px] font-semibold uppercase tracking-wide opacity-80">
                            Recommended
                          </span>
                        )}
                      </button>
                    ))}
                  </div>
                  <input
                    type="text"
                    aria-label={`Your own answer to: ${q.question}`}
                    value={answer.text ?? ''}
                    onChange={(e) => update(q.field, { text: e.target.value })}
                    placeholder="Or type your own answer…"
                    disabled={busy}
                    className={inputClass}
                  />

                  {q.slot === 'source' && option && needsTable(option) && (
                    <div className="mt-2">
                      <div className={subLabelClass}>Which table should I read?</div>
                      <TablePicker
                        connectionId={option.connection_id ?? ''}
                        connectionName={option.connection_name ?? ''}
                        hint={q.table_hint}
                        table={answer.table ?? ''}
                        schema={answer.schema ?? ''}
                        disabled={busy}
                        onChange={(table, schema) => update(q.field, { table, schema })}
                      />
                    </div>
                  )}

                  {q.slot === 'destination' && option?.kind === 'new_table' && (
                    <div className="mt-2">
                      <label className={`block ${subLabelClass}`} htmlFor={`${q.field}-new-table`}>
                        What should the new table be called?
                      </label>
                      <input
                        id={`${q.field}-new-table`}
                        type="text"
                        value={answer.newTableName ?? ''}
                        onChange={(e) => update(q.field, { newTableName: e.target.value })}
                        placeholder="returns_by_reason"
                        disabled={busy}
                        className={inputClass}
                      />
                    </div>
                  )}

                  {q.slot === 'destination' && option && needsTable(option) && (
                    <div className="mt-2">
                      <label className={`block ${subLabelClass}`} htmlFor={`${q.field}-table`}>
                        Which table should I write to?
                      </label>
                      <input
                        id={`${q.field}-table`}
                        type="text"
                        value={answer.table ?? ''}
                        onChange={(e) => update(q.field, { table: e.target.value })}
                        placeholder="Type a table name…"
                        disabled={busy}
                        className={inputClass}
                      />
                    </div>
                  )}

                  {asksWriteMode(q, option) && !payload.write_mode && (
                    <div className="mt-2">
                      <div className={subLabelClass}>How should the rows be written?</div>
                      <WriteModeChips
                        mode={answer.writeMode ?? ''}
                        mergeKey={answer.mergeKey ?? ''}
                        askKey={needsMergeKey(answer.writeMode ?? '')}
                        disabled={busy}
                        onMode={(writeMode) => update(q.field, { writeMode })}
                        onKey={(mergeKey) => update(q.field, { mergeKey })}
                      />
                    </div>
                  )}

                  {asksWriteMode(q, option) && needsMergeKey(payload.write_mode ?? '') && (
                    <MergeKeyInput
                      value={answer.mergeKey ?? ''}
                      disabled={busy}
                      onChange={(mergeKey) => update(q.field, { mergeKey })}
                    />
                  )}
                </>
              )}
            </div>
          );
        })}
      </div>

      <div className="px-4 py-3 bg-slate-50 border-t border-slate-100 flex items-center justify-end gap-2">
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-200 rounded-lg transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
        )}
        <button
          type="button"
          onClick={handleSend}
          disabled={!allRequiredAnswered || busy || formField !== null}
          className="px-3 py-1.5 text-xs font-semibold text-white rounded-lg transition-colors disabled:opacity-50 bg-indigo-600 hover:bg-indigo-700"
        >
          {busy ? 'Sending…' : 'Send answers'}
        </button>
      </div>
    </div>
  );
}

function WriteModeChips({ mode, mergeKey, askKey, disabled, onMode, onKey }: {
  mode: string;
  mergeKey: string;
  askKey: boolean;
  disabled?: boolean;
  onMode: (mode: string) => void;
  onKey: (key: string) => void;
}) {
  return (
    <>
      <div className="flex flex-wrap gap-1.5">
        {WRITE_MODE_VALUES.map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => onMode(value)}
            disabled={disabled}
            aria-pressed={mode === value}
            className={chipClass(mode === value)}
          >
            {WRITE_MODE_LABELS[value]}
          </button>
        ))}
      </div>
      {WRITE_MODE_HINTS[mode] && (
        <p className="mt-1 text-[11px] text-slate-500 leading-snug">{WRITE_MODE_HINTS[mode]}</p>
      )}
      {mode === 'merge' && askKey && (
        <MergeKeyInput value={mergeKey} disabled={disabled} onChange={onKey} />
      )}
    </>
  );
}

function MergeKeyInput({ value, disabled, onChange }: { value: string; disabled?: boolean; onChange: (key: string) => void }) {
  return (
    <div className="mt-2">
      <label className="block">
        <span className={`block ${subLabelClass}`}>Which column identifies a row?</span>
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="customer_id"
          disabled={disabled}
          className={inputClass}
        />
      </label>
    </div>
  );
}

interface CatalogTable {
  name: string;
  parent: string;
}

/** Tables on a connection, from the same catalog the node config browses. */
function TablePicker({ connectionId, connectionName, hint, table, schema, disabled, onChange }: {
  connectionId: string;
  connectionName: string;
  hint?: string;
  table: string;
  schema: string;
  disabled?: boolean;
  onChange: (table: string, schema: string) => void;
}) {
  const [tables, setTables] = useState<CatalogTable[] | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [typed, setTyped] = useState('');

  useEffect(() => {
    let alive = true;
    setTables(null);
    setUnavailable(false);
    setTyped('');
    api.getConnectionCatalog(connectionId)
      .then((catalog) => {
        if (!alive) return;
        const found = (catalog?.items ?? [])
          .filter((item) => ['table', 'view', 'collection'].includes(item.kind))
          .map((item) => ({ name: item.name, parent: item.parent }));
        setTables(found);
        setUnavailable(catalog?.supported === false);
        // The request named a table: pick it, or leave the name to confirm.
        if (hint) {
          const match = found.find((t) => t.name.toLowerCase() === hint.toLowerCase());
          if (match) {
            onChange(match.name, match.parent);
          } else {
            setTyped(hint);
            onChange(hint, '');
          }
        }
      })
      .catch(() => {
        if (!alive) return;
        setTables([]);
        setUnavailable(true);
        if (hint) {
          setTyped(hint);
          onChange(hint, '');
        }
      });
    return () => { alive = false; };
    // Only a different connection means a different list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionId]);

  const severalSchemas = useMemo(() => new Set((tables ?? []).map((t) => t.parent)).size > 1, [tables]);
  const narrowed = useMemo(() => {
    const q = typed.trim().toLowerCase();
    return (tables ?? []).filter((t) => !q || `${t.parent}.${t.name}`.toLowerCase().includes(q));
  }, [tables, typed]);

  return (
    <div>
      {tables === null && <div className="text-xs text-slate-500 mb-1">Looking up the tables in {connectionName}…</div>}
      {tables !== null && (unavailable || tables.length === 0) && (
        <div className="text-xs text-slate-500 mb-1">
          {unavailable
            ? `I could not list the tables in ${connectionName}. Type the table name instead.`
            : `I found no tables in ${connectionName}. Type the table name instead.`}
        </div>
      )}
      {narrowed.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-1.5">
          {narrowed.slice(0, MAX_TABLE_CHIPS).map((t) => {
            const selected = !typed.trim() && table === t.name && schema === t.parent;
            return (
              <button
                key={`${t.parent}.${t.name}`}
                type="button"
                onClick={() => { setTyped(''); onChange(t.name, t.parent); }}
                disabled={disabled}
                aria-pressed={selected}
                className={chipClass(selected)}
              >
                {severalSchemas && t.parent ? `${t.parent}.${t.name}` : t.name}
              </button>
            );
          })}
        </div>
      )}
      {narrowed.length > MAX_TABLE_CHIPS && (
        <p className="text-[11px] text-slate-500 mb-1">
          Showing {MAX_TABLE_CHIPS} of {narrowed.length}. Type to narrow the list.
        </p>
      )}
      <input
        type="text"
        aria-label={`Table in ${connectionName}`}
        value={typed}
        onChange={(e) => {
          setTyped(e.target.value);
          onChange(e.target.value, '');
        }}
        placeholder="Or type a table name…"
        disabled={disabled}
        className={inputClass}
      />
    </div>
  );
}

/** The transcript's sentence and the planner's choices, from the same answers. */
function buildAnswer(
  payload: ClarificationPayload,
  answers: Record<string, Answer>,
  selectedOption: (q: ClarifyQuestion) => ClarifyOption | undefined,
  needsTable: (option?: ClarifyOption) => boolean,
  asksWriteMode: (q: ClarifyQuestion, option?: ClarifyOption) => boolean,
): { text: string; choices: ClarifyChoices } {
  const lines: string[] = [];
  const choices: ClarifyChoices = {};
  const otherAnswers: Record<string, string> = {};

  const setWriteMode = (answer: Answer) => {
    const mode = answer.writeMode || payload.write_mode || '';
    if (mode) choices.write_mode = mode;
    const key = answer.mergeKey?.trim();
    if (key) choices.merge_on = [key];
    if (answer.writeMode) {
      lines.push(
        answer.writeMode === 'merge' && key
          ? `${WRITE_MODE_LABELS.merge} on ${key}.`
          : `${WRITE_MODE_LABELS[answer.writeMode]}.`,
      );
    } else if (key) {
      lines.push(`Match rows on ${key}.`);
    }
  };

  for (const q of payload.questions) {
    const answer = answers[q.field] ?? {};
    const typed = answer.text?.trim();

    if (q.slot === 'source_table') {
      const table = answer.table?.trim();
      if (table) {
        choices.source_table = { table, schema: answer.schema ?? '' };
        lines.push(`Read the table ${table}.`);
      }
      continue;
    }
    if (q.slot === 'destination_table') {
      const table = (answer.table ?? q.table_hint ?? '').trim();
      if (table) {
        choices.destination_table = { table, schema: answer.schema ?? '' };
        lines.push(`Write to the table ${table}.`);
      }
      continue;
    }
    if (q.slot === 'write_mode' || q.slot === 'merge_key') {
      setWriteMode(answer);
      continue;
    }

    if (!isSlotQuestion(q)) {
      const option = selectedOption(q);
      const value = typed || option?.label;
      if (value) {
        otherAnswers[q.field] = value;
        lines.push(`${q.label || q.field}: ${value}`);
      }
      continue;
    }

    const reading = q.slot === 'source';
    const verb = reading ? 'Read from' : 'Write to';
    if (typed) {
      choices[reading ? 'source' : 'destination'] = { kind: 'text', text: typed };
      lines.push(`${verb} ${typed}.`);
      continue;
    }
    const option = selectedOption(q);
    if (!option) continue;

    let picked: Record<string, unknown>;
    let sentence: string;
    if (option.kind === 'connection') {
      const table = needsTable(option) ? answer.table?.trim() : '';
      picked = { kind: 'connection', connection_id: option.connection_id, table: table ?? '', schema: answer.schema ?? '' };
      sentence = table ? `${verb} ${option.connection_name}, table ${table}.` : `${verb} ${option.connection_name}.`;
    } else if (option.kind === 'file') {
      picked = { kind: 'file', file_id: option.file_id, file_name: option.file_name };
      sentence = `${verb} ${option.file_name}.`;
    } else if (option.kind === 'keep_file') {
      picked = { kind: 'keep_file', file_name: option.file_name };
      sentence = `${verb} ${option.file_name}, which I will upload later.`;
    } else if (option.kind === 'table') {
      picked = { kind: 'table', schema_name: option.schema_name, table_name: option.table_name };
      sentence = `${verb} the managed table ${option.schema_name}.${option.table_name}.`;
    } else {
      const name = answer.newTableName?.trim() ?? '';
      picked = { kind: 'new_table', table_name: name };
      sentence = `${verb} a new managed table called ${name}.`;
    }
    choices[reading ? 'source' : 'destination'] = picked;
    lines.push(sentence);
    if (asksWriteMode(q, option)) setWriteMode(answer);
  }

  if (Object.keys(otherAnswers).length > 0) choices.answers = otherAnswers;
  return { text: lines.join('\n'), choices };
}
