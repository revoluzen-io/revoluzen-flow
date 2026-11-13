/**
 * XML bodies and replies in the API Explorer.
 *
 * The server does the real work (parsing, rows, filling values in); these are
 * the small questions the page has to answer as you type.
 */

/** The `{name}` placeholders in a body, in order, each once. The server fills in the same names. */
export function bodyPlaceholders(body: string): string[] {
  return Array.from(new Set(Array.from(body.matchAll(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g), match => match[1])));
}

export function isWellFormedXml(text: string): boolean {
  if (!text.trim()) return true;
  try {
    return !new DOMParser().parseFromString(text, 'application/xml').querySelector('parsererror');
  } catch {
    return false;
  }
}

type Step = { deep: boolean; name: string; index?: number };

function parseSteps(path: string): Step[] | null {
  const text = path.trim();
  const source = text.startsWith('/') ? text : `//${text}`;
  const steps: Step[] = [];
  let position = 0;
  while (position < source.length) {
    const match = /^(\/\/?)([^/[\]]+)(?:\[(\d+)\])?/.exec(source.slice(position));
    if (!match) return null;
    const name = match[2].trim();
    steps.push({
      deep: match[1] === '//',
      // Prefixes are ignored, as they are in a row path. `@ns:id` -> `@id`.
      name: name.startsWith('@') ? `@${name.slice(1).split(':').pop()}` : name.split(':').pop() as string,
      index: match[3] ? Number(match[3]) : undefined,
    });
    position += match[0].length;
  }
  return steps.length ? steps : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Children of an element, one entry each, with a repeated element's list opened out. */
function childrenOf(node: unknown): [string, unknown][] {
  if (!isRecord(node)) return [];
  return Object.entries(node).flatMap(([key, value]): [string, unknown][] => {
    if (key === '#text') return [];
    return Array.isArray(value) ? value.map(item => [key, item] as [string, unknown]) : [[key, value]];
  });
}

function collect(node: unknown, step: Step, into: unknown[]): void {
  for (const [key, value] of childrenOf(node)) {
    if (step.name === '*' ? !key.startsWith('@') : key === step.name) into.push(value);
    if (step.deep && !key.startsWith('@')) collect(value, step, into);
  }
}

/** An element's own text, when that is all a check can sensibly mean by its value. */
function plainValue(value: unknown): unknown {
  if (value === null) return '';
  if (isRecord(value) && Object.prototype.hasOwnProperty.call(value, '#text')) return value['#text'];
  return value;
}

/**
 * Read a value from an XML reply with a path such as `/orders/order[2]/sku`,
 * `//sku` or `/orders/order/@id`.
 *
 * Only this small grammar is understood and nothing is evaluated, so a hostile
 * path can only fail to find anything. One match gives that value; several give
 * a list, and `matches` is always the full list, for counting.
 */
export function resolveXmlPath(data: unknown, path: string): { found: boolean; value: unknown; matches: unknown[] } {
  const none = { found: false, value: undefined, matches: [] as unknown[] };
  const steps = path ? parseSteps(path) : null;
  if (!steps || data === null || data === undefined) return none;
  let current: unknown[] = [data];
  for (const step of steps) {
    current = current.flatMap(node => {
      const found: unknown[] = [];
      collect(node, step, found);
      return step.index ? found.slice(step.index - 1, step.index) : found;
    });
  }
  if (!current.length) return none;
  const matches = current.map(plainValue);
  return { found: true, value: matches.length === 1 ? matches[0] : matches, matches };
}
