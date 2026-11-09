import { useEffect, useState } from 'react';

import {
  DQ_OPS,
  dqValueKind,
  ignoredParamKeys,
  ignoredRuleKeys,
  isFailureSettingKey,
  isTextType,
  ruleProblem,
  textsFromValue,
  valueForColumnType,
  valueForOpChange,
  valueFromTexts,
} from '../utils/dataQualityRules';

type Rule = Record<string, any>;

interface DataQualityRulesProps {
  rules: Rule[];
  /** Upstream column names; when empty the column is typed. */
  columns?: string[];
  /** Upstream column -> DuckDB type, when known. A text column keeps numeric-looking values as text. */
  sourceTypes?: Record<string, string>;
  onChange: (rules: Rule[]) => void;
}

const CONTROL =
  'px-2.5 py-1.5 text-xs text-slate-800 bg-white border border-slate-300 rounded-lg ' +
  'focus:outline-none focus:ring-2 focus:ring-pipe-300 focus:border-pipe-400 placeholder:text-slate-500';

/** The rule with its value set, or removed for an op that takes none. Keeps the { column, op, value } shape. */
function withValue(rule: Rule, value: unknown): Rule {
  const { value: _previous, ...rest } = rule;
  return value === undefined ? rest : { ...rest, value };
}

/** The rule without these keys. */
function withoutKeys(rule: Rule, keys: string[]): Rule {
  return Object.fromEntries(Object.entries(rule).filter(([key]) => !keys.includes(key)));
}

const NOTE = 'text-[10px] text-amber-600';
const NOTE_BUTTON = 'underline hover:text-amber-800';

/**
 * The settings of the Data Quality step that it does not read (an `on_failure`
 * where it reads `mode`, ...), with a way to remove them. Shows nothing when
 * every setting is one it reads.
 */
export function IgnoredStepSettings({
  params,
  onRemove,
}: {
  params: Record<string, any> | undefined;
  onRemove: (keys: string[]) => void;
}) {
  const keys = ignoredParamKeys(params);
  if (keys.length === 0) return null;
  return (
    <div className={`mt-1 flex flex-wrap items-center gap-2 ${NOTE}`}>
      <span>
        Not read by this step: {keys.join(', ')}.
        {keys.some(isFailureSettingKey) ? ' What happens to a failing row is set by On Failure above.' : ''}
      </span>
      <button
        type="button"
        onClick={() => onRemove(keys)}
        className={NOTE_BUTTON}
        aria-label={`Remove ${keys.join(', ')}`}
      >Remove</button>
    </div>
  );
}

/**
 * The boxes for one rule's value: one for a single value, a comma-separated one
 * for in / not_in, low and high for between. It keeps the text as it is typed
 * ("1." on the way to "1.5", "a," on the way to "a,b") and reports the value
 * that text means; it reads the stored value again only when that changes
 * from somewhere else.
 */
function RuleValue({
  label,
  op,
  value,
  textColumn,
  onValue,
}: {
  label: string;
  op: string;
  value: unknown;
  textColumn: boolean;
  onValue: (value: unknown) => void;
}) {
  const kind = dqValueKind(op);
  const [texts, setTexts] = useState<string[]>(() => textsFromValue(op, value));

  useEffect(() => {
    const typed = valueFromTexts(op, texts, { textColumn });
    if (JSON.stringify(typed) !== JSON.stringify(value)) setTexts(textsFromValue(op, value));
    // Only a change to the stored value (or to what the text would mean) is a reason to re-read it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [op, value, textColumn]);

  const type = (index: number, text: string) => {
    const next = texts.slice();
    next[index] = text;
    setTexts(next);
    onValue(valueFromTexts(op, next, { textColumn }));
  };

  if (kind === 'range') {
    return (
      <div className="flex items-center gap-1.5">
        <input
          aria-label={`${label} low`}
          value={texts[0] ?? ''}
          onChange={(e) => type(0, e.target.value)}
          placeholder="low"
          className={`flex-1 min-w-0 ${CONTROL}`}
        />
        <span className="text-[10px] uppercase tracking-wide text-slate-500">and</span>
        <input
          aria-label={`${label} high`}
          value={texts[1] ?? ''}
          onChange={(e) => type(1, e.target.value)}
          placeholder="high"
          className={`flex-1 min-w-0 ${CONTROL}`}
        />
      </div>
    );
  }

  if (kind === 'list') {
    return (
      <div className="flex flex-col gap-0.5">
        <input
          aria-label={`${label} values`}
          value={texts[0] ?? ''}
          onChange={(e) => type(0, e.target.value)}
          placeholder="north, south, east"
          className={`w-full ${CONTROL}`}
        />
        <span className="text-[10px] text-slate-500">Separate the values with commas.</span>
      </div>
    );
  }

  return (
    <input
      aria-label={`${label} value`}
      value={texts[0] ?? ''}
      onChange={(e) => type(0, e.target.value)}
      placeholder={op === 'regex' ? 'pattern' : op === 'min_length' || op === 'max_length' ? 'number of characters' : 'value'}
      className={`w-full ${CONTROL}`}
    />
  );
}

/** The Validation Rules list of the Data Quality node. */
export default function DataQualityRules({ rules, columns = [], sourceTypes, onChange }: DataQualityRulesProps) {
  const textColumnOf = (column: string | undefined) => isTextType(sourceTypes?.[column ?? '']);
  const replace = (index: number, rule: Rule) => onChange(rules.map((r, i) => (i === index ? rule : r)));

  const setColumn = (index: number, column: string) => {
    const rule = rules[index];
    // A value typed for a number column is read again for a text column, and back.
    const value = valueForColumnType(rule.op, rule.value, { textColumn: textColumnOf(column) });
    replace(index, withValue({ ...rule, column }, value));
  };
  const setOp = (index: number, op: string) => {
    const rule = rules[index];
    const value = valueForOpChange(rule.op, op, rule.value, { textColumn: textColumnOf(rule.column) });
    replace(index, withValue({ ...rule, op }, value));
  };
  const setValue = (index: number, value: unknown) => replace(index, withValue(rules[index], value));

  return (
    <div className="flex flex-col gap-2">
      {rules.map((rule, index) => {
        const op: string = rule.op || 'not_null';
        const label = `Rule ${index + 1}`;
        const problem = ruleProblem({ op, value: rule.value });
        // Keys the step does not read: a rule is { column, op, value } and nothing else.
        const unread = ignoredRuleKeys(rule);
        // A rule keeps showing its own column even when it is not (yet) among the upstream ones.
        const columnOptions = rule.column && !columns.includes(rule.column) ? [rule.column, ...columns] : columns;
        return (
          <div key={index} className="flex flex-col gap-1.5 p-2 bg-slate-50 rounded-lg border border-slate-100">
            <div className="flex items-center gap-1">
              {columnOptions.length > 0 ? (
                <select
                  aria-label={`${label} column`}
                  value={rule.column || ''}
                  onChange={(e) => setColumn(index, e.target.value)}
                  className={`flex-1 min-w-0 ${CONTROL}`}
                >
                  {/* A rule with no column yet says so, instead of showing the first one it does not have. */}
                  {!rule.column && <option value="">Choose a column</option>}
                  {columnOptions.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              ) : (
                <input
                  aria-label={`${label} column`}
                  value={rule.column || ''}
                  onChange={(e) => setColumn(index, e.target.value)}
                  placeholder="column"
                  className={`flex-1 min-w-0 ${CONTROL}`}
                />
              )}
              <select
                aria-label={`${label} check`}
                value={op}
                onChange={(e) => setOp(index, e.target.value)}
                className={`flex-1 min-w-0 ${CONTROL}`}
              >
                {DQ_OPS.map((o) => (
                  <option key={o} value={o}>{o}</option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => onChange(rules.filter((_, i) => i !== index))}
                className="text-xs text-red-500 hover:text-red-700 px-1"
                title="Remove rule"
                aria-label={`Remove ${label.toLowerCase()}`}
              >✕</button>
            </div>
            {dqValueKind(op) !== 'none' && (
              <RuleValue
                key={dqValueKind(op)}
                label={label}
                op={op}
                value={rule.value}
                textColumn={textColumnOf(rule.column)}
                onValue={(value) => setValue(index, value)}
              />
            )}
            {problem && <div className={NOTE}>{problem}</div>}
            {unread.length > 0 && (
              <div className={`flex flex-wrap items-center gap-2 ${NOTE}`}>
                <span>Not read by this step: {unread.join(', ')}.</span>
                <button
                  type="button"
                  onClick={() => replace(index, withoutKeys(rule, unread))}
                  className={NOTE_BUTTON}
                  aria-label={`${label}: remove ${unread.join(', ')}`}
                >Remove</button>
              </div>
            )}
          </div>
        );
      })}
      <button
        type="button"
        onClick={() => onChange([...rules, { column: columns[0] || '', op: 'not_null' }])}
        className="text-xs text-pipe-600 hover:text-pipe-800 self-start"
      >+ Add rule</button>
    </div>
  );
}
