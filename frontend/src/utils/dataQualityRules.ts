/**
 * Data Quality rules: the value each op needs, and how the editor's text boxes
 * map onto it.
 *
 * The node (backend/revoluzen_flow/nodes/quality.py) reads a rule as
 * `{ column, op, value }`. What `value` must be depends on the op:
 *
 *   not_null, is_null            no value
 *   eq, ne, gt, lt, gte, lte     one value, a number or a string
 *   regex                        a pattern, always a string
 *   min_length, max_length       a whole number
 *   in, not_in                   a list of values
 *   between                      [low, high]
 *
 * A value of the wrong shape does not fail loudly. "10,100" typed for `between`
 * is read as BETWEEN '1' AND '0' and drops every row; "a,b" typed for `in`
 * matches only the text "a,b". So the editor keeps the text a person types in
 * its boxes and stores what that text means. The built-in templates use the
 * same shapes (see templates/catalog.ts).
 */

export const DQ_OPS: string[] = [
  'not_null', 'is_null', 'eq', 'ne', 'gt', 'lt', 'gte', 'lte',
  'in', 'not_in', 'regex', 'between', 'min_length', 'max_length',
];

/** How many values an op takes, and in what form. */
export type DqValueKind = 'none' | 'one' | 'list' | 'range';

export function dqValueKind(op: string | undefined): DqValueKind {
  switch (op || 'not_null') {
    case 'not_null':
    case 'is_null':
      return 'none';
    case 'in':
    case 'not_in':
      return 'list';
    case 'between':
      return 'range';
    default:
      return 'one';
  }
}

export interface DqTextOptions {
  /**
   * The column holds text. Numeric-looking text then stays text ("123" is a
   * status code, not a number): DuckDB compares a text column with a string,
   * and refuses to compare it with a number.
   */
  textColumn?: boolean;
}

/** Whether a DuckDB column type (VARCHAR, VARCHAR(20), TEXT, ...) holds text. */
export function isTextType(columnType: string | undefined | null): boolean {
  return !!columnType && /^(?:VARCHAR|TEXT|STRING|CHAR|BPCHAR|NVARCHAR|NCHAR|CHARACTER)\b/i.test(columnType.trim());
}

const PLAIN_NUMBER = /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/;

/**
 * The number a text stands for, or null when it is text. Only plain decimals
 * count: "007", "02134", "1e3", "1,000", ".5" and numbers too big to hold
 * exactly stay text, so a code or an id is never rewritten into a different
 * number.
 */
export function numberFromText(text: string): number | null {
  const t = text.trim();
  if (!PLAIN_NUMBER.test(t)) return null;
  const n = Number(t);
  if (!Number.isFinite(n)) return null;
  if (Number.isInteger(n) && !Number.isSafeInteger(n)) return null;
  return n === 0 ? 0 : n; // no -0
}

/**
 * The value to store for what is typed in an op's boxes: one text for a single
 * value or a list, two (low, high) for between, none for the ops without one.
 *
 * Numbers are stored as numbers when the text is a number. A list or a range is
 * all numbers or all text, never a mix: `[1, 'b']` means nothing to a column.
 */
export function valueFromTexts(op: string | undefined, texts: string[], opts: DqTextOptions = {}): unknown {
  const asText = !!opts.textColumn;
  switch (dqValueKind(op)) {
    case 'none':
      return undefined;
    case 'range': {
      const low = (texts[0] ?? '').trim();
      const high = (texts[1] ?? '').trim();
      const l = numberFromText(low);
      const h = numberFromText(high);
      return !asText && l !== null && h !== null ? [l, h] : [low, high];
    }
    case 'list': {
      const items = (texts[0] ?? '').split(',').map((s) => s.trim()).filter((s) => s !== '');
      const numbers: number[] = [];
      for (const item of items) {
        const n = numberFromText(item);
        if (n === null) break;
        numbers.push(n);
      }
      return !asText && items.length > 0 && numbers.length === items.length ? numbers : items;
    }
    default: {
      const text = texts[0] ?? '';
      if (op === 'regex') return text; // a pattern is always text
      const n = asText ? null : numberFromText(text);
      return n !== null ? n : text;
    }
  }
}

const textOf = (v: unknown): string => (v === undefined || v === null ? '' : String(v));

/** The text boxes' content for a stored value: the inverse of valueFromTexts. */
export function textsFromValue(op: string | undefined, value: unknown): string[] {
  switch (dqValueKind(op)) {
    case 'none':
      return [];
    case 'range':
      return Array.isArray(value) ? [textOf(value[0]), textOf(value[1])] : [textOf(value), ''];
    default:
      return [Array.isArray(value) ? value.map(textOf).join(', ') : textOf(value)];
  }
}

/**
 * The value to store when a rule's op is switched. A value carries over between
 * ops that take the same kind (eq -> gt, in -> not_in), a single value becomes a
 * one-item list and a list gives its first item to a single value. Anything else
 * starts empty. Ops without a value drop it.
 */
export function valueForOpChange(
  fromOp: string | undefined,
  toOp: string | undefined,
  value: unknown,
  opts: DqTextOptions = {},
): unknown {
  const to = dqValueKind(toOp);
  const from = dqValueKind(fromOp);
  if (to === 'none') return undefined;
  let texts: string[] = to === 'range' ? ['', ''] : [''];
  if (from === to || (from === 'one' && to === 'list')) {
    texts = textsFromValue(fromOp, value);
  } else if (from === 'list' && to === 'one') {
    texts = [(textsFromValue(fromOp, value)[0] ?? '').split(',')[0].trim()];
  }
  return valueFromTexts(toOp, texts, opts);
}

/** The same value read again for another kind of column (text or not). */
export function valueForColumnType(op: string | undefined, value: unknown, opts: DqTextOptions = {}): unknown {
  return valueFromTexts(op, textsFromValue(op, value), opts);
}

const asNumber = (v: unknown): number | null =>
  typeof v === 'number' ? v : typeof v === 'string' ? numberFromText(v) : null;

const isBlank = (v: unknown): boolean => v === undefined || v === null || (typeof v === 'string' && v.trim() === '');

/** A list or an object: not a single value. */
const isContainer = (v: unknown): boolean => typeof v === 'object' && v !== null;

/** A whole number of 0 or more, as a number or as digits in a string (what the node's `int()` reads). */
const isWholeNumber = (v: unknown): boolean =>
  typeof v === 'number' ? Number.isInteger(v) && v >= 0 : typeof v === 'string' ? /^[0-9]+$/.test(v.trim()) : false;

/**
 * Why the node cannot run a rule with this op and value, in a few words for the
 * person editing it; null when it can.
 *
 * The node (dq_rule_problems in backend/revoluzen_flow/nodes/quality.py) stops
 * the run on each of these. A rule it could not use used to compile to TRUE and
 * pass every row without a word. Both are held to one table of cases,
 * __tests__/fixtures/dqRuleCases.json, so they cannot disagree.
 */
export function valueProblem(op: string | undefined, value: unknown): string | null {
  switch (op || 'not_null') {
    case 'eq':
    case 'ne':
    case 'gt':
    case 'lt':
    case 'gte':
    case 'lte':
      if (value === undefined || value === null) {
        return 'Enter a value to compare with (use is_null to look for an empty value).';
      }
      return isContainer(value) ? 'Enter one value, not a list.' : null;
    case 'in':
    case 'not_in':
      if (Array.isArray(value)) {
        if (value.length === 0) return 'Enter at least one value.';
        return value.some((x) => x === undefined || x === null || isContainer(x)) ? 'A value in the list is empty.' : null;
      }
      return isBlank(value) || isContainer(value) ? 'Enter at least one value.' : null;
    case 'between':
      if (!Array.isArray(value)) {
        if (isBlank(value)) return 'Enter the low and high limits.';
        return typeof value === 'string'
          ? 'Saved as plain text, which the check cannot read. Type both limits again.'
          : 'Enter both limits.';
      }
      return value.length !== 2 || value.some((x) => isBlank(x) || isContainer(x)) ? 'Enter both limits.' : null;
    case 'regex':
      if (typeof value === 'string') return value === '' ? 'Enter a pattern.' : null;
      return isBlank(value) ? 'Enter a pattern.' : 'The pattern must be text.';
    case 'min_length':
    case 'max_length':
      return isWholeNumber(value) ? null : 'Enter a whole number.';
    default:
      return null;
  }
}

/**
 * A note about a rule that runs but is probably not what was meant: a range the
 * wrong way round, or a list saved as one piece of text (which still compares
 * with that one text).
 */
export function ruleNote(rule: { op?: string; value?: unknown }): string | null {
  const v = rule.value;
  switch (dqValueKind(rule.op)) {
    case 'range': {
      if (!Array.isArray(v) || v.length !== 2) return null;
      const low = asNumber(v[0]);
      const high = asNumber(v[1]);
      return low !== null && high !== null && low > high ? 'Low is above high, so no row can pass.' : null;
    }
    case 'list':
      return typeof v === 'string' && v.includes(',')
        ? 'Saved as one piece of text. Type the list again to split it.'
        : null;
    default:
      return null;
  }
}

/** The one line shown under a rule in the panel: what stops it from running, else a note. */
export function ruleProblem(rule: { op?: string; value?: unknown }): string | null {
  return valueProblem(rule.op, rule.value) ?? ruleNote(rule);
}

export interface DqRuleIssue {
  /** error: the node stops the run. warning: it runs, but is probably not what was meant. */
  level: 'error' | 'warning';
  message: string;
}

/**
 * Everything wrong with a Data Quality step's rules, for the check before Run
 * (validateWorkflow.ts). One entry per rule that has a problem, numbered from 1,
 * with all of that rule's problems in it. A missing or empty list is not
 * reported here: the required-field check says that.
 */
export function dqRuleIssues(rules: unknown): DqRuleIssue[] {
  if (!rules) return [];
  if (!Array.isArray(rules)) {
    return [{ level: 'error', message: 'The rules must be a list, each with a column, a check and a value.' }];
  }
  const issues: DqRuleIssue[] = [];
  rules.forEach((rule, index) => {
    const number = index + 1;
    if (!rule || typeof rule !== 'object' || Array.isArray(rule)) {
      issues.push({
        level: 'error',
        message: `Rule ${number} is not a rule: it needs a column, a check and, for most checks, a value.`,
      });
      return;
    }
    const r = rule as Record<string, unknown>;
    const column = typeof r.column === 'string' ? r.column.trim() : '';
    const op = r.op || 'not_null';
    const where = `Rule ${number} (${[column, String(op)].filter(Boolean).join(' ')})`;
    const problems: string[] = [];
    if (!column) problems.push('Choose a column.');
    if (typeof op !== 'string' || !DQ_OPS.includes(op)) {
      problems.push(`"${String(op)}" is not one of the checks (${DQ_OPS.join(', ')}).`);
    } else {
      const problem = valueProblem(op, r.value);
      if (problem) problems.push(problem);
    }
    if (problems.length > 0) {
      // The rule cannot run, so what it would do as written is not said; the keys the step ignores are.
      const unread = ignoredRuleKeysNote(r, false);
      issues.push({ level: 'error', message: `${where}: ${[...problems, ...(unread ? [unread] : [])].join(' ')}` });
      return;
    }
    const notes = [ruleNote({ op: String(op), value: r.value }), ignoredRuleKeysNote(r)].filter(Boolean);
    if (notes.length > 0) issues.push({ level: 'warning', message: `${where}: ${notes.join(' ')}` });
  });
  return issues;
}

/**
 * Keys the step does not read. A rule is read as { column, op, value } and the
 * step's own settings as the ones the node declares (`default_params` in
 * backend/revoluzen_flow/nodes/quality.py). Anything else is ignored without a
 * word, and the old templates were full of it: { column, check: 'between', min, max }
 * ran as a plain not-null, and `on_failure: 'fail'` left the step dropping rows.
 * Keys that start with an underscore belong to the editor and the engine.
 */
export const DQ_RULE_KEYS: readonly string[] = ['column', 'op', 'value'];
export const DQ_PARAM_KEYS: readonly string[] = ['rules', 'mode', 'quality_threshold', 'include_score', 'include_profile'];

const unreadKeys = (target: unknown, read: readonly string[]): string[] =>
  target && typeof target === 'object' && !Array.isArray(target)
    ? Object.entries(target as Record<string, unknown>)
        .filter(([key, value]) => value !== undefined && !key.startsWith('_') && !read.includes(key))
        .map(([key]) => key)
        .sort()
    : [];

/** The keys of this rule the step does not read. */
export const ignoredRuleKeys = (rule: unknown): string[] => unreadKeys(rule, DQ_RULE_KEYS);

/** The settings of this step the step does not read. */
export const ignoredParamKeys = (params: unknown): string[] => unreadKeys(params, DQ_PARAM_KEYS);

/** on_fail, on_failure, onFailure: what people wrote when they meant `mode`. */
export const isFailureSettingKey = (key: string): boolean => /^on_?fail(ure)?$/i.test(key);

const quoted = (keys: string[]): string => {
  const q = keys.map((k) => `"${k}"`);
  return q.length <= 1 ? q.join('') : `${q.slice(0, -1).join(', ')} and ${q[q.length - 1]}`;
};

/**
 * A sentence about the keys of a rule the step does not read: which they are, and
 * where each belongs, for the keys the old templates used. With `runsAsWritten`
 * it also says what the rule does without them: with no `op` it only checks that
 * the column is not empty. Null when the rule has no such key.
 */
export function ignoredRuleKeysNote(rule: unknown, runsAsWritten = true): string | null {
  const keys = ignoredRuleKeys(rule);
  if (keys.length === 0) return null;
  const parts = [
    `${quoted(keys)} ${keys.length === 1 ? 'is' : 'are'} not read by this step, which reads only column, op and value.`,
  ];
  if (keys.some((k) => ['check', 'rule', 'type'].includes(k))) parts.push('The check goes in "op".');
  if (keys.some((k) => ['min', 'max', 'pattern', 'values'].includes(k))) {
    parts.push('Its value goes in "value" ([low, high] for between).');
  }
  if (keys.includes('columns')) parts.push('The column goes in "column".');
  if (runsAsWritten && !(rule as Record<string, unknown>).op) {
    parts.push('With no "op" the rule only checks that the column is not empty.');
  }
  return parts.join(' ');
}

/** One warning for each setting of the step that it does not read, for the check before Run. */
export function ignoredParamIssues(params: unknown): Array<DqRuleIssue & { field: string }> {
  const mode = (params as Record<string, unknown> | null | undefined)?.mode;
  return ignoredParamKeys(params).map((key) =>
    isFailureSettingKey(key)
      ? {
          level: 'warning' as const,
          field: 'mode',
          message:
            `"${key}" is not read by this step. What happens to a failing row is set by "mode" (drop, fail, tag, reject or split).` +
            (mode ? '' : ' With no "mode" the step drops failing rows.'),
        }
      : {
          level: 'warning' as const,
          field: key,
          message: `"${key}" is not read by this step, which reads rules, mode, quality_threshold, include_score and include_profile.`,
        },
  );
}
