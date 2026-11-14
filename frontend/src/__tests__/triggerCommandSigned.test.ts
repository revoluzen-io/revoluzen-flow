/**
 * The command for an endpoint that needs signed calls. It has to sign the
 * exact bytes it sends, with the secret's decoded bytes, in the Standard
 * Webhooks headers the server checks (triggers/webhook_signing.py).
 */
import { describe, expect, it } from 'vitest';
import {
  API_KEY_PLACEHOLDER,
  SIGNING_SECRET_PLACEHOLDER,
  buildCommand,
  callerCheckLabel,
} from '../utils/triggerCommand';

const base = {
  origin: 'https://flow.example.com',
  path: 'hooks/orders',
  requireApiKey: false,
  requireSignature: true,
  parameters: [{ name: 'region', type: 'string', default: 'EU' }],
};

describe('a signed call in a terminal', () => {
  const command = buildCommand({ ...base, shell: 'curl' });

  it('signs the id, the time and the body with the decoded secret', () => {
    expect(command).toContain(`SECRET='${SIGNING_SECRET_PLACEHOLDER}'`);
    expect(command).toContain(`BODY='{"region":"EU"}'`);
    expect(command).toContain('ID="msg_$(openssl rand -hex 12)"');
    expect(command).toContain('TS="$(date +%s)"');
    expect(command).toContain(`printf '%s' "\${SECRET#whsec_}" | openssl base64 -d -A`);
    expect(command).toContain(`printf '%s' "$ID.$TS.$BODY" | openssl dgst -sha256 -mac HMAC -macopt "hexkey:$KEY" -binary | openssl base64 -A`);
  });

  it('sends the three headers and the body byte for byte', () => {
    expect(command).toContain(`curl -X POST 'https://flow.example.com/api/published/hooks/orders'`);
    expect(command).toContain('-H "webhook-id: $ID"');
    expect(command).toContain('-H "webhook-timestamp: $TS"');
    expect(command).toContain('-H "webhook-signature: v1,$SIG"');
    expect(command).toContain('--data-binary "$BODY"');
    expect(command).not.toContain('X-API-Key');
  });

  it('holds a secret made in this visit, and a key when both are needed', () => {
    const both = buildCommand({ ...base, requireApiKey: true, apiKey: 'fpk_x', signingSecret: 'whsec_abc=', shell: 'curl' });
    expect(both).toContain(`SECRET='whsec_abc='`);
    expect(both).toContain(`-H 'X-API-Key: fpk_x'`);
    const placeholder = buildCommand({ ...base, requireApiKey: true, shell: 'curl' });
    expect(placeholder).toContain(`X-API-Key: ${API_KEY_PLACEHOLDER}`);
  });
});

describe('a signed call in PowerShell', () => {
  const command = buildCommand({ ...base, shell: 'powershell', signingSecret: "whsec_it's" });

  it('computes the same signature with .NET', () => {
    expect(command).toContain(`$secret = 'whsec_it''s'`);
    expect(command).toContain(`$body = '{"region":"EU"}'`);
    expect(command).toContain('[System.Security.Cryptography.HMACSHA256]::new([Convert]::FromBase64String($secret.Substring(6)))');
    expect(command).toContain('$hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("$id.$ts.$body"))');
  });

  it('sends the signed bytes and the three headers', () => {
    expect(command).toContain(`-Headers @{ 'webhook-id' = $id; 'webhook-timestamp' = $ts; 'webhook-signature' = "v1,$sig" }`);
    expect(command).toContain('-Body ([Text.Encoding]::UTF8.GetBytes($body))');
    expect(command).toContain(`-ContentType 'application/json; charset=utf-8'`);
  });
});

describe('an endpoint without signing', () => {
  it('keeps the plain command', () => {
    const command = buildCommand({ ...base, requireSignature: false, requireApiKey: true, shell: 'curl' });
    expect(command).not.toContain('webhook-signature');
    expect(command).toContain('X-API-Key');
  });
});

describe('how callers prove who they are', () => {
  it('says it in words', () => {
    expect(callerCheckLabel({ require_api_key: 1, require_signature: 0 })).toBe('API key');
    expect(callerCheckLabel({ require_api_key: 0, require_signature: 1 })).toBe('Signature');
    expect(callerCheckLabel({ require_api_key: true, require_signature: true })).toBe('Key and signature');
    expect(callerCheckLabel({ require_api_key: 0 })).toBe('No check');
  });
});
