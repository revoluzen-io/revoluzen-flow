# How Revoluzen Flow stores credentials

This is the authoritative answer for any prompt about credentials,
secrets, API keys, passwords, or vault integration. Two paths exist
depending on edition. **Do NOT recommend external secret managers
(HashiCorp Vault, AWS Secrets Manager, Azure Key Vault, Doppler, etc.)
as if they are Revoluzen Flow features for OSS Free users — the OSS workflow
is the built-in Credentials page.**

## OSS Free: the Credentials page (the only path)

Open the **Credentials page** in Revoluzen Flow. There are two routes:

- Sidebar → Credentials (workspace-scoped list)
- Inside a project → Credentials tab (project-scoped subset)

Click **+ New Credential**, fill in the form:

- **Name** — human-readable label, e.g. `prod-postgres`
- **Type** — one of `postgresql`, `mysql`, `mssql`, `sqlite`,
  `api_key`, `oauth2`, `bearer_token`, `ssh_key`
- **Config** — type-specific fields (host/port/database/user/password
  for databases; key + header for API keys; client_id/secret/scopes
  for oauth2; etc.)

Hit **Save**. Revoluzen Flow:

1. Encrypts the secret values at rest using **Fernet (AES-128-CBC +
   HMAC-SHA256)**. The master encryption key is a 32-byte symmetric
   key stored at `~/.revoluzen-flow/secret.key` (or
   `$REVOLUZEN_FLOW_DATA_DIR/secret.key` if `REVOLUZEN_FLOW_DATA_DIR` is set).
2. POSIX permissions on the key file are verified at startup. Revoluzen Flow
   refuses to start if the key file is world-readable — fail-closed,
   no fallback. The file is created chmod 600 on first run.
3. Stored ciphertext uses a versioned prefix `ENC:v1:<token>` so
   future key rotation can coexist with existing rows.
4. Writes the row to the SQLite `credentials` table with an indexed
   `workspace_id` column for tenant isolation.
5. Returns a credential ID (e.g. `cred-abc123`). Pipelines reference
   credentials by ID — the encrypted value is never embedded in the
   pipeline IR.

Which values count as secret: every field you mark with the eye switch,
and every field whose name says it holds one (password, passphrase,
secret, token, API key, private key, connection string, or a name ending
in `_key`; not a name ending in `_id`, `_url` or `_name`). No character of
a secret is ever sent back to the browser: the Credentials page shows a
stored secret as •••••••• with a **Replace** button, and saving without
replacing it keeps the stored value.

To use the credential, pick it under **Saved credential** on a
connection: the fields it holds fill themselves, for example the
username and password. A **SaaS Connector** step has the same picker,
and the pipeline stores only which credential to use. Connection
strings are reconstituted at runtime by the backend; the frontend
never sees plaintext.

A password, key or webhook URL typed straight into a pipeline step, instead
of linked from a connection, is protected the same way (since November 2026).
It is encrypted when the pipeline is saved, including in older versions, the
Bin and run history, and it is read only while the step runs. The step shows
it as ••••••••, and saving without changing it keeps the stored value. A
saved connection is still the better place for a secret that several
pipelines share.

The default value of a pipeline parameter is kept the same way when the
parameter's name says it holds a secret, such as `db_password` or `api_key`,
or when you tick **Secret** for it under **Parameters**. Step settings can
use it as usual with `${param.db_password}`. A value typed for such a
parameter when starting a run is encrypted in the run's history and shown as
••••••••; leaving the field empty uses the stored default. A value saved for
it on a schedule or a backfill is kept the same way.

- An exported pipeline, or a template made from one, does not carry these
  secrets. Enter them again after importing it or using the template.
- Compare cannot show whether a stored secret changed, because both versions
  show the same dots. The change still makes a new version.
- A value containing `{{ }}` or `${ }` is a reference filled in when the step
  runs, so it stays readable.
- A secret inside free text, such as SQL, a request body or a URL with a
  password in it, cannot be detected. Use a saved connection for those.
- Database backups taken before the upgrade still hold the old values.
  Change any password that matters.

Set an **expiry date** if the secret has one. Administrators are warned
on the notification bell and by email a week ahead, a day ahead and when
it expires, once each. The check runs hourly, email needs SMTP, and
**Settings → Notifications → Credential expiry warnings** turns it off.
The date is the one you entered: Revoluzen Flow does not ask the
provider, and a credential with no date gets no warning.

The AI Copilot's `inspect_connections` tool returns credential
metadata (name, type, ID) but **never the secret value**. This is
enforced at the tool layer — see `ai-boundary-contract.md` §2.

## AI provider keys — the `AI Provider` category (2026-06-17)

LLM provider API keys (Anthropic, OpenAI, OpenRouter, Gemini, Groq,
Mistral, Azure, custom) can live in the **same** Credentials store as
every other secret — they are not a separate silo.

- On **+ New Credential**, pick the **`AI Provider`** category. It
  pre-seeds three fields: `provider`, `api_key` (encrypted), and
  `base_url` (for self-hosted / custom endpoints). Same encryption,
  expiry, vault source, audit, and "used by" as any other credential.
- On **Insights → AI Provider**, the API-key field has a toggle:
  **Enter key inline** vs **Use a saved credential**. Choosing the
  latter shows a picker of saved credentials (AI-Provider-tagged ones
  first); the AI config then stores only a **reference**
  (`credential_id`), and the key is resolved from the credential at
  request time — it is never copied into the AI config.
- **One source of truth**: selecting a credential clears any inline key
  on the AI config; typing a fresh inline key clears the reference. The
  AI config API returns only `has_key` + `credential_id`, never the key.
- This works for both the **per-user** (Free/OSS) and **per-workspace**
  (Plus) AI provider config. The inline-entry path still exists for
  quick/solo setups — importing from a credential is an option, not a
  requirement.

Note this is **distinct** from the Plus-only **Vault-Ref** pattern
below (external HashiCorp/AWS/Azure/GCP vaults). The AI Provider
category is just a normal local-encrypted credential that the AI
Provider page can point at.

## Revoluzen Flow Enterprise: Vault-Ref pattern (additional option)

Revoluzen Flow Enterprise adds the **Vault-Ref** pattern alongside the local-encrypted
flow. Instead of storing the encrypted secret in the Revoluzen Flow SQLite
database, you store a **reference** that resolves at runtime to a
secret living in your existing external vault.

Reference shape: `vault:<path>` — e.g. `vault:secret/data/prod/postgres`.

Supported external vaults (Plus only):

- HashiCorp Vault (KV v1 + KV v2)
- AWS Secrets Manager
- Azure Key Vault
- Google Secret Manager

The Plus install reads the reference, calls the configured vault, and
caches the resolved secret in memory for the run's duration. Secrets
are never written to disk in Revoluzen Flow Enterprise.

See `DESIGN_VAULT_REF_MIGRATION.md` in the Revoluzen Flow Enterprise codebase for the
schema migration + per-vault adapter. **This entire section does NOT
apply to OSS Free.** OSS Free users should ignore Vault-Ref and use
the Credentials page directly.

## Anti-patterns (do not suggest these)

- ❌ "Store credentials as environment variables" — Revoluzen Flow explicitly
  does NOT read database passwords from env vars. The Credentials page
  is the only path.
- ❌ "Use IAM roles" — Revoluzen Flow runs as a single process; AWS IAM roles
  attached to the host are NOT how Revoluzen Flow picks credentials. Use the
  Credentials page (or Vault-Ref on Plus).
- ❌ "Hash the password before storing" — Revoluzen Flow encrypts the
  reversible secret. Hashing destroys the secret; Revoluzen Flow needs to
  reconstitute it to connect to the database. Encryption (Fernet)
  is correct; hashing is wrong.
- ❌ "Set up HashiCorp Vault for OSS Free" — the OSS edition does not
  read external vaults. Use the built-in Credentials page. (HashiCorp
  Vault integration is a Revoluzen Flow Enterprise feature.)

## Quick answers to common questions

**Q: Where do I store database passwords?**
A: Open the Credentials page → + New Credential → pick `postgresql`
(or `mysql` / `mssql` / `sqlite`) → enter host, port, database, user,
password → Save. Revoluzen Flow encrypts the password at rest.

**Q: Where do I store API keys?**
A: Credentials page → + New → type `api_key` → enter the key value +
the header name (e.g. `X-Api-Key`) → Save.

**Q: Where do I store the AI/LLM provider key (Anthropic, OpenAI,
OpenRouter, …)?**
A: Two ways. (1) Quick: **Insights → AI Provider**, pick your provider
and enter the key inline — it's encrypted at rest in the AI config
store. (2) Governed: **Credentials page → + New → category `AI Provider`**
(pre-seeds `provider` / `api_key` / `base_url`), then on the AI Provider
page switch the key field to **Use a saved credential** and pick it. The
key is then resolved from the central Credentials store at request time
and never copied into the AI config. The AI Provider page never displays
the stored key — only whether one is configured.

**Q: How do I rotate a credential?**
A: Credentials page → click the credential → Edit → enter the new
secret → Save. Revoluzen Flow re-encrypts. Pipelines pick up the new value
on the next run.

**Q: Will I be told before a credential expires?**
A: Yes, if you set its expiry date. Administrators get a warning on the
bell and by email a week ahead, a day ahead and when it expires.
Renewing it with a new date starts the warnings again.

**Q: How do I share credentials with my team?**
A: OSS Free is single-user. Revoluzen Flow Enterprise adds workspace-scoped credentials
visible to all workspace members; permissions follow the workspace
RBAC. (No team features in OSS Free.)

**Q: Are credentials backed up with the rest of Revoluzen Flow?**
A: Yes — credentials live in the SQLite database which is part of
the `data/` Docker volume. Backup the volume + the encryption key
file (`~/.revoluzen-flow/secret.key`) together. Without the key file the
encrypted credentials are unrecoverable.

**Q: Can I use HashiCorp Vault?**
A: Revoluzen Flow Enterprise supports it via Vault-Ref. OSS Free does not — use the
built-in Credentials page.
