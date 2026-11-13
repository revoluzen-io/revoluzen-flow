/**
 * Stored secrets in saved connections and credentials.
 *
 * The server never sends a stored password or key back to the browser: it
 * sends SECRET_PLACEHOLDER instead (backend: connections/inline_secrets.py
 * and credentials/sensitive_fields.py). A form that sends the placeholder
 * back unchanged keeps the stored value; any other value replaces it.
 */

/** What the API returns in place of a stored password or key. */
export const SECRET_PLACEHOLDER = '\u2022'.repeat(8);

export function isStoredSecret(value: unknown): boolean {
  return value === SECRET_PLACEHOLDER;
}

/** How a credential field name is saved: trimmed, lower case, spaces as underscores. */
export function credentialFieldKey(name: string): string {
  return name.trim().toLowerCase().replace(/ /g, '_');
}

// Credential field names the server always treats as secret, whatever the
// "sensitive" switch says (backend: credentials/sensitive_fields.py,
// name_says_secret). tests/test_credential_secrets.py holds the two rules
// to the same answers. Were they to drift, the server would still protect
// everything it protects; the form would only leave the switch unlocked.
const ALWAYS_SECRET = new Set(['webhook_url', 'service_account_json', 'custom_header_value', 'ssl_key_pem']);
const SECRET_WORDS = /password|passwd|passphrase|secret|token|apikey|api_key|private_key|connection_string|credentials_json/;
const NOT_SECRET_ENDING = /_(id|url|uri|name|type|endpoint|path|header|param)$/;

export function nameSaysSecret(name: string): boolean {
  const key = credentialFieldKey(name);
  if (!key) return false;
  if (ALWAYS_SECRET.has(key)) return true;
  if (NOT_SECRET_ENDING.test(key)) return false;
  return key === 'key' || key.endsWith('_key') || SECRET_WORDS.test(key);
}

// Pipeline parameter names the server keeps as secrets, whatever the switch in
// the Parameters dialog says (backend: ir/step_secrets.py,
// parameter_name_says_secret). Whole words, so max_tokens and merge_key stay
// readable. tests/test_step_secrets.py holds the two to the same lists.
const PARAMETER_SECRET_WORDS = new Set(['password', 'passwd', 'pwd', 'passphrase', 'secret', 'token', 'authorization', 'credentials']);
const PARAMETER_SECRET_LAST_WORDS = new Set(['pass', 'pw']);
const PARAMETER_SECRET_ENDINGS = /(apikey|apikeyvalue|privatekey|privatekeypem|accesskey|accountkey|appkey|authkey|authvalue|signingkey|encryptionkey|masterkey|hmackey|sshkey|sharedkey|connectionstring|credentialsjson|serviceaccountjson|webhookurl)$/;
const PARAMETER_NOT_SECRET_LAST_WORDS = new Set(['url', 'uri', 'endpoint', 'name', 'id', 'type', 'field', 'column', 'path', 'ref', 'file', 'env', 'header', 'param', 'limit', 'count', 'size', 'ttl', 'expiry', 'budget', 'length', 'version']);

export function parameterNameSaysSecret(name: string): boolean {
  const words = name.replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  if (words.length === 0) return false;
  if (PARAMETER_SECRET_ENDINGS.test(words.join(''))) return true;
  const last = words[words.length - 1];
  if (PARAMETER_NOT_SECRET_LAST_WORDS.has(last)) return false;
  return PARAMETER_SECRET_LAST_WORDS.has(last) || words.some((word) => PARAMETER_SECRET_WORDS.has(word));
}

/** Whether a declared pipeline parameter keeps its default as a secret. */
export function isSecretParameter(parameter: { name: string; secret?: boolean }): boolean {
  return !!parameter.secret || parameterNameSaysSecret(parameter.name);
}
