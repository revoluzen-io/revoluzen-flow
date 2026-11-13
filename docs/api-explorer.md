# API Explorer

Insights > API Explorer (`#author`) is where you test a live API endpoint and, if
you want one, draft a connector from it. It replaced the separate "Author" and
"Gallery" tabs: six links to public specs are a starting point, not a
destination, and "Gallery" implied a catalogue of shipped connectors.

For the connectors Revoluzen Flow already ships, see **Connections** (to use one) and
the **Trust** page (certification tiers per connector). The Explorer does not
list them.

## The three steps

The page states these on arrival, and the step rail navigates between them.

### 1. Choose an endpoint

Four ways in, all in the **Start here** card:

- **Start from a known API** — six curated references (Stripe, GitHub, Slack,
  Twilio, Plaid, DigitalOcean). *Load specification* downloads the public spec
  from a server-side allowlist and discovers searchable endpoints. It does not
  call the vendor's account APIs, generate a manifest or save a connection.
- **Paste or upload a specification** — OpenAPI JSON or YAML, or a SOAP WSDL,
  parsed server-side.
- **Load a WSDL by its address** — for a SOAP service. If the WSDL needs a
  sign-in, choose one under Authentication first; it is sent with that download
  and any files the WSDL imports from the same site, and nowhere else.
- **Type any URL** — straight into the request bar; no specification needed.

Choosing a SOAP operation fills in the address, the `SOAPAction` and
`Content-Type` headers, and an envelope with a `{name}` placeholder for each
value. Fill the values in below the body. An element marked `<!--Optional:-->`
is left out when it has no value. The envelope stays editable: add a
`wsse:Security` header by hand if the service needs one, using `{username}` and
`{password}` placeholders. Very large operations are trimmed to the required
fields and some optional ones, and the page says so.

WSDL 1.1 is supported, including SOAP 1.1 and 1.2 and imported schema files.
WSDL 2.0 is not; almost no service publishes it.

Remote JSON imports allow up to 32 MB; YAML and pasted/uploaded specifications
retain the 2 MB parser limit. Downloads have a 30-second network deadline,
verified TLS, pinned validated DNS addresses, and no redirects or forwarded app
credentials. Discovery returns up to 5,000 operations and reports when the limit
is reached. A spec's declared authentication is not evidence of live access.

### 2. Test it

Fill path placeholders, authentication, headers, query parameters and an optional
body, then send **one** request. POST/PUT/PATCH/DELETE require confirmation.
There are no retries and no automatic pagination.

The body can be **JSON, XML, Form or Text**. The choice sets the `Content-Type`
header unless you set one yourself, and a warning appears if XML is not
well-formed.

The reply is shown for what it is:

| Reply | How it is shown |
|---|---|
| JSON | Collapsible tree with a key/value filter, a record table, a structure listing |
| XML | Indented text, a table of the rows at a **row path** (the repeating element is chosen for you and you can pick another), a structure listing with attributes and counts |
| SOAP fault | The fault's code and message in red above the reply, whatever the HTTP status |
| CSV or tab-separated | The text, and a table |
| HTML | The page's source and title. It is never opened as a page |
| Plain text | The text |
| Binary | What the file is and how big, instead of unreadable characters |

**Tests** asserts on the response: status code, response time, a header, a value
at a JSON path, a value at an XML path such as `/orders/order[1]/sku`, `//sku` or
`/orders/order/@id`, or body text. Checks are declarative structs the engine
interprets — never evaluated code — and run on each send. An incomplete check is
skipped, not failed.

### 3. Generate a connector

Three possible inputs, and they are not equivalent:

| Source | Endpoint | Result |
|---|---|---|
| Imported OpenAPI spec | `/connectors/author/from-openapi` | v2 cert manifest **and** a v1 runtime manifest → savable as a Beta connector |
| An XML or SOAP request you just tried | `/connectors/author/from-request` | A v1 runtime manifest that repeats the request and reads rows from the row path you chose → savable as a Beta connector |
| A JSON response you just tested | `/connectors/author/from-samples` | v2 manifest only → review/export, not savable |

Only the runtime manifest is loadable by the SaaS Connector node, so the
sample-derived path offers no Save button. Saving requires an admin account and
takes effect without a restart. Review the generated authentication per endpoint
before saving; a draft is a starting point, not a certified connector.

A connector made from a request never holds a secret. Sign-in becomes fields on
the pipeline step, which a saved credential can fill. Headers and query
parameters that look like secrets are dropped and reported, and a password typed
into an envelope becomes a field. Each `{name}` in the body becomes a field too,
with what you typed as its default. A request that failed, or that came back with
a fault, cannot be made into a connector until you have a working reply.

**Create connection** hands the endpoint to the REST connection form. The handoff
is one-shot and memory-only: it transfers the endpoint without its query, the
auth method and API key *name* — not passwords, tokens, custom headers, query
values or the body. Enter credentials in the connection setup. A REST connection
reads XML too: in the Source step, **Row Path (XML)** says which element is one row.

## Boundaries

- Five authentication choices: none, Basic, Bearer, API key header, API key
  query. OAuth grant acquisition, cookie sessions and saved-credential selection
  are not implemented here.
- Network requests require developer rank. URLs are DNS-validated and connections
  pinned to a validated IP; TLS verifies the original hostname. Redirects are not
  followed. TLS verification cannot be disabled.
- Private targets are blocked unless the server sets
  `REVOLUZEN_FLOW_API_SOURCE_ALLOW_PRIVATE=1`. Link-local metadata, multicast, reserved
  and unspecified addresses remain blocked.
- Response bodies are bounded at 256 KB; JSON arrays and XML rows are sampled to
  100 entries. Sensitive field and element names and supplied auth values are
  redacted. Responses may still contain business or personal data; nothing is
  persisted by the Explorer.
- XML that declares its own entities is refused and shown as text, because
  entities can be used to exhaust memory or read files. Nothing outside the
  document is ever fetched while reading it.
- Credentials and request history live in the page only, for the session. Nothing
  is written to disk.
- A successful response validates that one request — not pagination, other
  endpoints, API-wide permissions or an ETL workload. Failed HTTP responses
  remain inspectable.
- SOAP is reached through a connector made here. There is no SOAP connection type
  on the Connections page.
- The backend must be running a build that registers
  `/api/connectors/author/explorer/{request,discover,reference,wsdl}`.
