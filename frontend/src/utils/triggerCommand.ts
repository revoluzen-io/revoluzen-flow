/**
 * The command another application runs to start a published pipeline.
 *
 * Kept apart from the Triggers page so the text a user pastes into a terminal
 * can be tested on its own. A wrong quote here only shows up when someone runs
 * the command, and by then it looks like the product does not work.
 */

import { isStoredSecret } from './storedSecrets';

export interface TriggerParameter {
  name: string;
  type?: string;
  default?: unknown;
  description?: string;
  required?: boolean;
}

export type CommandShell = 'curl' | 'powershell';

export const API_KEY_PLACEHOLDER = 'YOUR_API_KEY';
export const SIGNING_SECRET_PLACEHOLDER = 'YOUR_SIGNING_SECRET';

/** Shown in the body for a required text value that has no default to offer. */
const TEXT_PLACEHOLDER = 'your value';

/** How an endpoint's callers prove they may start its pipeline, in words. */
export function callerCheckLabel(endpoint: { require_api_key?: unknown; require_signature?: unknown }): string {
  const key = !!endpoint.require_api_key;
  const signature = !!endpoint.require_signature;
  if (key && signature) return 'Key and signature';
  if (signature) return 'Signature';
  if (key) return 'API key';
  return 'No check';
}

export function publishedUrl(origin: string, path: string): string {
  const base = origin.replace(/\/+$/, '');
  return `${base}/api/published/${path.replace(/^\/+/, '')}`;
}

/** A URL path from a pipeline's name: "Daily Orders (EU)" becomes "daily-orders-eu". */
export function pathFromName(name: string): string {
  return (name || '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80)
    .replace(/-+$/, '');
}

/** Same rule the backend applies. Each part starts with a letter or digit. */
export function isValidPath(path: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9._~-]*(\/[A-Za-z0-9][A-Za-z0-9._~-]*)*$/.test(path) && path.length <= 200;
}

/**
 * A default written with a system placeholder such as `${utcnow:%Y-%m-%d}`
 * belongs to the server. Sent back from a caller it would be a literal string,
 * so it stays out of the body and the server's default applies. A stored
 * secret, which reaches the browser only as dots, is the same.
 */
function isServerDefault(value: unknown): boolean {
  return isStoredSecret(value) || (typeof value === 'string' && value.includes('${'));
}

/** A parameter's value in the example body, as its declared type. */
function exampleValue(param: TriggerParameter): unknown {
  const type = (param.type || 'string').toLowerCase();
  const raw = param.default;
  const hasDefault = raw !== null && raw !== undefined && raw !== '';
  if (type === 'int' || type === 'float') {
    if (hasDefault && Number.isFinite(Number(raw))) return Number(raw);
    return param.required ? 0 : null;
  }
  if (type === 'bool') {
    if (hasDefault) return raw === true || String(raw).toLowerCase() === 'true';
    return param.required ? false : null;
  }
  if (type === 'json') {
    if (hasDefault) {
      if (typeof raw !== 'string') return raw;
      try { return JSON.parse(raw); } catch { return raw; }
    }
    return param.required ? {} : null;
  }
  if (hasDefault) return String(raw);
  return param.required ? TEXT_PLACEHOLDER : null;
}

/** The JSON body: every declared parameter at its default. */
export function buildRequestBody(parameters: TriggerParameter[]): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  for (const param of parameters || []) {
    if (isServerDefault(param.default)) continue;
    body[param.name] = exampleValue(param);
  }
  return body;
}

function singleQuotedForPosix(text: string): string {
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

function singleQuotedForPowerShell(text: string): string {
  return `'${text.replace(/'/g, `''`)}'`;
}

export interface CommandOptions {
  origin: string;
  path: string;
  requireApiKey: boolean;
  /** The real key, only when it was created in this browser session. */
  apiKey?: string | null;
  /** Each call must carry a signature (Standard Webhooks headers). */
  requireSignature?: boolean;
  /** The real signing secret, only when it was made in this browser session. */
  signingSecret?: string | null;
  parameters?: TriggerParameter[];
  shell?: CommandShell;
}

/**
 * A command that signs its call: a fresh message id and the time, then an
 * HMAC-SHA256 of `<id>.<time>.<body>` with the secret's bytes (the part after
 * `whsec_`, base64-decoded), sent as `webhook-signature: v1,<base64>`.
 *
 * The body is sent byte for byte as it was signed, so it is kept on one line:
 * `--data-binary` and a byte array leave it exactly as it is.
 */
function buildSignedCommand(options: CommandOptions, url: string, body: Record<string, unknown>): string {
  const { requireApiKey, apiKey, signingSecret, shell = 'curl' } = options;
  const key = apiKey || API_KEY_PLACEHOLDER;
  const secret = signingSecret || SIGNING_SECRET_PLACEHOLDER;
  const json = JSON.stringify(body);

  if (shell === 'powershell') {
    const headers = [
      `'webhook-id' = $id`,
      `'webhook-timestamp' = $ts`,
      `'webhook-signature' = "v1,$sig"`,
      ...(requireApiKey ? [`'X-API-Key' = ${singleQuotedForPowerShell(key)}`] : []),
    ];
    return [
      `$secret = ${singleQuotedForPowerShell(secret)}`,
      `$body = ${singleQuotedForPowerShell(json)}`,
      `$id = 'msg_' + [guid]::NewGuid().ToString('N')`,
      `$ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()`,
      `$hmac = [System.Security.Cryptography.HMACSHA256]::new([Convert]::FromBase64String($secret.Substring(6)))`,
      `$sig = [Convert]::ToBase64String($hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("$id.$ts.$body")))`,
      [
        `Invoke-RestMethod -Method Post -Uri ${singleQuotedForPowerShell(url)}`,
        `-ContentType 'application/json; charset=utf-8'`,
        `-Headers @{ ${headers.join('; ')} }`,
        `-Body ([Text.Encoding]::UTF8.GetBytes($body))`,
      ].join(' `\n  '),
    ].join('\n');
  }

  const lines = [
    `curl -X POST ${singleQuotedForPosix(url)}`,
    `-H "webhook-id: $ID"`,
    `-H "webhook-timestamp: $TS"`,
    `-H "webhook-signature: v1,$SIG"`,
    ...(requireApiKey ? [`-H ${singleQuotedForPosix(`X-API-Key: ${key}`)}`] : []),
    `-H 'Content-Type: application/json'`,
    `--data-binary "$BODY"`,
  ];
  return [
    `SECRET=${singleQuotedForPosix(secret)}`,
    `BODY=${singleQuotedForPosix(json)}`,
    `ID="msg_$(openssl rand -hex 12)"`,
    `TS="$(date +%s)"`,
    `KEY="$(printf '%s' "\${SECRET#whsec_}" | openssl base64 -d -A | od -An -tx1 | tr -d ' \\n')"`,
    `SIG="$(printf '%s' "$ID.$TS.$BODY" | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$KEY" -binary | openssl base64 -A)"`,
    lines.join(' \\\n  '),
  ].join('\n');
}

/**
 * The full command. `curl` runs in a macOS or Linux terminal and in Git Bash.
 *
 * PowerShell gets its own native form, because `curl` there is an alias for a
 * different command on Windows PowerShell, and JSON with double quotes does not
 * survive being passed to `curl.exe`. A pasted curl line simply errors, which
 * is not what someone trying this out should see.
 */
export function buildCommand(options: CommandOptions): string {
  const { origin, path, requireApiKey, apiKey, parameters = [], shell = 'curl' } = options;
  const url = publishedUrl(origin, path);
  const key = apiKey || API_KEY_PLACEHOLDER;
  const body = buildRequestBody(parameters);
  if (options.requireSignature) return buildSignedCommand(options, url, body);
  // Indented to sit under the flag that holds it. JSON.stringify escapes any
  // line break inside a value, so the only ones here are between keys.
  const json = Object.keys(body).length > 0 ? JSON.stringify(body, null, 2).replace(/\n/g, '\n  ') : '';

  if (shell === 'powershell') {
    const lines = [`Invoke-RestMethod -Method Post -Uri ${singleQuotedForPowerShell(url)}`];
    if (requireApiKey) lines.push(`-Headers @{ 'X-API-Key' = ${singleQuotedForPowerShell(key)} }`);
    if (json) {
      lines.push(`-ContentType 'application/json'`);
      lines.push(`-Body ${singleQuotedForPowerShell(json)}`);
    }
    return lines.join(' `\n  ');
  }

  const lines = [`curl -X POST ${singleQuotedForPosix(url)}`];
  if (requireApiKey) lines.push(`-H ${singleQuotedForPosix(`X-API-Key: ${key}`)}`);
  if (json) {
    lines.push(`-H 'Content-Type: application/json'`);
    lines.push(`-d ${singleQuotedForPosix(json)}`);
  }
  return lines.join(' \\\n  ');
}
