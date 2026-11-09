import { describe, expect, it } from 'vitest';
import {
  API_KEY_PLACEHOLDER,
  buildCommand,
  buildRequestBody,
  isValidPath,
  pathFromName,
  publishedUrl,
  type TriggerParameter,
} from '../utils/triggerCommand';

const PARAMETERS: TriggerParameter[] = [
  { name: 'region', type: 'string', required: true },
  { name: 'min_amount', type: 'int', default: 100 },
  { name: 'rate', type: 'float', default: '1.5' },
  { name: 'dry_run', type: 'bool', default: 'true' },
  { name: 'options', type: 'json', default: '{"a":1}' },
  { name: 'note', type: 'string', default: 'hello' },
];

const ORIGIN = 'http://localhost:8001';

/** The JSON a shell would hand to curl, read back out of the command. */
function bodyOf(command: string): unknown {
  const start = command.indexOf("-d '") + 4;
  const end = command.lastIndexOf("'");
  return JSON.parse(command.slice(start, end).replace(/'\\''/g, "'"));
}

describe('publishedUrl', () => {
  it('joins the origin and the path without doubling slashes', () => {
    expect(publishedUrl('http://localhost:8001/', '/orders/run')).toBe('http://localhost:8001/api/published/orders/run');
  });
});

describe('buildRequestBody', () => {
  it('uses each default, as the declared type', () => {
    expect(buildRequestBody(PARAMETERS)).toEqual({
      region: 'your value',
      min_amount: 100,
      rate: 1.5,
      dry_run: true,
      options: { a: 1 },
      note: 'hello',
    });
  });

  it('gives a required value with no default something of the right type', () => {
    expect(buildRequestBody([
      { name: 'a', type: 'int', required: true },
      { name: 'b', type: 'bool', required: true },
      { name: 'c', type: 'json', required: true },
    ])).toEqual({ a: 0, b: false, c: {} });
  });

  it('sends an optional value with no default as null, which the server reads as not supplied', () => {
    expect(buildRequestBody([{ name: 'a', type: 'string' }])).toEqual({ a: null });
  });

  it('keeps a falsy default instead of treating it as missing', () => {
    expect(buildRequestBody([
      { name: 'a', type: 'int', default: 0 },
      { name: 'b', type: 'bool', default: false },
    ])).toEqual({ a: 0, b: false });
  });

  it('leaves out a default the server has to work out for itself', () => {
    expect(buildRequestBody([
      { name: 'run_date', type: 'string', default: '${utcnow:%Y-%m-%d}' },
      { name: 'region', type: 'string', default: 'EU' },
    ])).toEqual({ region: 'EU' });
  });
});

describe('buildCommand as curl', () => {
  it('has the URL, the key placeholder and the body', () => {
    const command = buildCommand({ origin: ORIGIN, path: 'orders-run', requireApiKey: true, parameters: PARAMETERS });
    expect(command).toContain("curl -X POST 'http://localhost:8001/api/published/orders-run'");
    expect(command).toContain(`-H 'X-API-Key: ${API_KEY_PLACEHOLDER}'`);
    expect(command).toContain("-H 'Content-Type: application/json'");
    expect(bodyOf(command)).toEqual(buildRequestBody(PARAMETERS));
  });

  it('uses the real key when one was just created', () => {
    const command = buildCommand({ origin: ORIGIN, path: 'p', requireApiKey: true, apiKey: 'fpk_synthetic' });
    expect(command).toContain("-H 'X-API-Key: fpk_synthetic'");
    expect(command).not.toContain(API_KEY_PLACEHOLDER);
  });

  it('has no key header when the endpoint needs none', () => {
    const command = buildCommand({ origin: ORIGIN, path: 'p', requireApiKey: false, apiKey: 'fpk_synthetic' });
    expect(command).not.toContain('X-API-Key');
    expect(command).not.toContain('fpk_synthetic');
  });

  it('leaves out the body and its header when there is nothing to send', () => {
    const command = buildCommand({ origin: ORIGIN, path: 'p', requireApiKey: true, parameters: [] });
    expect(command).not.toContain('-d ');
    expect(command).not.toContain('Content-Type');
  });

  it('continues every line but the last, so a pasted command runs whole', () => {
    const lines = buildCommand({ origin: ORIGIN, path: 'p', requireApiKey: true, parameters: [] }).split('\n');
    expect(lines).toHaveLength(2);
    expect(lines[0].endsWith(' \\')).toBe(true);
    expect(lines[1].endsWith('\\')).toBe(false);
  });

  it('survives a single quote in a value', () => {
    const parameters = [{ name: 'customer', type: 'string', default: "O'Brien" }];
    const command = buildCommand({ origin: ORIGIN, path: 'p', requireApiKey: false, parameters });
    expect(command).toContain(`O'\\''Brien`);
    expect(bodyOf(command)).toEqual({ customer: "O'Brien" });
  });
});

describe('buildCommand for PowerShell', () => {
  it('uses the native command with the same URL, key and body', () => {
    const command = buildCommand({
      origin: ORIGIN, path: 'orders-run', requireApiKey: true, parameters: [{ name: 'region', default: 'EU' }], shell: 'powershell',
    });
    expect(command).toContain("Invoke-RestMethod -Method Post -Uri 'http://localhost:8001/api/published/orders-run'");
    expect(command).toContain(`-Headers @{ 'X-API-Key' = '${API_KEY_PLACEHOLDER}' }`);
    expect(command).toContain("-ContentType 'application/json'");
    expect(command).toContain('"region": "EU"');
    expect(command).not.toContain('curl');
  });

  it('doubles a single quote, the way PowerShell escapes one', () => {
    const command = buildCommand({
      origin: ORIGIN, path: 'p', requireApiKey: false, parameters: [{ name: 'customer', default: "O'Brien" }], shell: 'powershell',
    });
    expect(command).toContain("O''Brien");
  });
});

describe('pathFromName', () => {
  it.each([
    ['Daily Orders', 'daily-orders'],
    ['  Daily   Orders (EU) ', 'daily-orders-eu'],
    ['Café Sales', 'cafe-sales'],
    ['Webhook: Order Event → Managed Table', 'webhook-order-event-managed-table'],
    ['***', ''],
  ])('%s', (name, expected) => {
    expect(pathFromName(name)).toBe(expected);
  });

  it('always gives a path the backend accepts, or nothing', () => {
    for (const name of ['Daily Orders', 'x'.repeat(300), '-leading', 'trailing-', 'a/b']) {
      const path = pathFromName(name);
      expect(path === '' || isValidPath(path)).toBe(true);
    }
  });
});

describe('isValidPath', () => {
  it.each(['a', 'orders-run', 'orders/run', 'v1.2/run_now', 'a~b'])('accepts %s', (path) => {
    expect(isValidPath(path)).toBe(true);
  });

  it.each(['', '/a', 'a/', 'a b', '../up', 'a/../b', 'a//b', '-a', 'x?y=1'])('refuses %s', (path) => {
    expect(isValidPath(path)).toBe(false);
  });
});
