"""Source Watch — detect that a connector's upstream API changed.

The gap this fills
------------------
Nothing else in the codebase can notice a vendor changing their API.

`cat.py` replays recorded cassettes. That proves our parsing, pagination and
cursor logic still work against *the responses we recorded* — it cannot
detect that the vendor has since changed those responses, because it never
talks to the vendor. The 14-day expiry on a CAT result (`_cat_green` in
`api/cert_matrix.py`) is a hedge against exactly that blindness: it assumes
rot after two weeks rather than observing it. Useful, but it is a timer, not
a sensor.

`connectors/ci/live_smoke.yml` would be a real sensor, and its allow-list is
empty, so no live vendor call happens anywhere today.

So: a connector can break in production and the first party to find out is
the customer whose pipeline failed. This module exists to make us find out
first, on a daily cadence.

Four signals, cheapest first
----------------------------
1. ``api_version`` — the version we are pinned to, read out of the runtime
   manifest's ``base_url`` (``https://api.airtable.com/v0/{base_id}`` → ``v0``),
   compared against what the vendor currently advertises at an
   *unauthenticated* probe endpoint. This is the signal that answers "the
   vendor shipped a new API version" directly.

2. ``spec`` — fetch the vendor's published OpenAPI/JSON description, and
   check that every endpoint path our streams declare is still present in it.
   A path that disappears from the spec is a breaking change with our name on
   it. Fingerprint the whole document too, so we notice *any* movement.

3. ``changelog`` — fetch the vendor's API changelog and fingerprint it. Low
   precision by design: it cannot tell us what changed, only that the vendor
   published something. That is still a day-one warning to go look.

4. ``deprecation`` — RFC 8594 ``Deprecation`` / ``Sunset`` headers and
   ``Warning``, harvested passively from responses to calls the product
   already makes (see ``rest_framework._record_source_signals``). The vendor
   is telling us directly; we only had to read it.

Signals 1–3 need no vendor account, which is why they are worth having: they
run unattended, daily, for every connector that declares where to look.
Signal 4 needs no account either — it rides on traffic that already happens.

Sovereignty
-----------
This module never runs as a side effect of using the product. Probing is
outbound traffic to a third party, and a local-first tool does not make those
calls because it felt like it. ``run_source_watch`` is invoked by the daily
maintainer workflow (`.github/workflows/source-watch.yml`) and by an operator
who asks for it explicitly. Signal 4 is the exception that proves the rule:
it adds no request at all, it reads headers off responses the pipeline
already received.

Offline by default in tests
---------------------------
Every network call goes through an injected ``fetch`` callable. The default
implementation reuses the SSRF-guarded opener from ``rest_framework`` — probe
URLs come from manifests, and manifests can be contributed, so they are
untrusted input and get the same treatment as any other manifest URL. Tests
pass their own fetcher and touch no network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# A fetcher returns (status_code, headers, body_text). Anything that raises is
# reported as a probe_error signal rather than taking the whole run down: one
# vendor having an outage must not blind us to the other 65 connectors.
Fetcher = Callable[[str], "tuple[int, dict[str, str], str]"]

_SEVERITY_ORDER = {"info": 0, "warn": 1, "critical": 2}

# Matches the API version segment in a base_url:
#   https://api.airtable.com/v0/{base_id}        -> v0
#   https://api.stripe.com/v1                    -> v1
#   https://xx.salesforce.com/services/data/v59.0 -> v59.0
#   https://graph.microsoft.com/beta             -> beta
_VERSION_IN_URL = re.compile(r"/(v\d+(?:\.\d+)?|beta|alpha)(?:/|$)", re.IGNORECASE)

# Headers vendors use to state an API version — read both ways round: as a
# *request* header a manifest sends (which is the pin in force) and as a
# *response* header the vendor echoes (which is what they actually served).
# Lower-case; every lookup is case-insensitive.
_VERSION_HEADERS = (
    "x-github-api-version",
    "x-github-api-version-selected",
    "stripe-version",
    "x-shopify-api-version",
    "x-ms-graph-version",
    "apigw-api-version",
    "x-api-version",
    "api-version",
)


@dataclass(frozen=True)
class SourceSignal:
    """One observation about a connector's upstream.

    `pinned` vs `observed` is the whole point: a signal is only actionable if
    it says what we are built against *and* what the vendor is doing now.
    """

    connector_id: str
    kind: str          # api_version | spec | changelog | deprecation | probe_error
    severity: str      # info | warn | critical
    summary: str
    pinned: str | None = None
    observed: str | None = None
    # Full digest of whatever was fetched, when the signal came from
    # fingerprinting a document. Kept separate from `observed` — which holds a
    # short form for humans — because the baseline must compare full digests.
    # Storing the truncated display value would make every subsequent run
    # report "changed" against a prefix that can never equal a full hash.
    fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Paths ────────────────────────────────────────────────────────────────────


def _manifests_dir() -> Path:
    return Path(__file__).resolve().parent / "manifests"


def _ci_dir() -> Path:
    return Path(__file__).resolve().parent / "ci"


def baseline_path() -> Path:
    """Fingerprints from the last run — what "changed" is measured against."""
    return _ci_dir() / "source_watch_baseline.json"


def status_path() -> Path:
    """Latest run's signals, committed so drift is visible in git history."""
    return _ci_dir() / "source_watch_status.json"


# ── Manifest reading ─────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def runtime_manifests() -> dict[str, dict[str, Any]]:
    """{connector_id: v1 runtime manifest}.

    The v1 manifests are the ones that carry `base_url` and per-stream `path`,
    which is what drift is measured against. The `.v2.json` certification
    manifests describe capability and schema, not HTTP, so they are not useful
    here.
    """
    out: dict[str, dict[str, Any]] = {}
    d = _manifests_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        if path.name.endswith(".v2.json"):
            continue
        try:
            m = _load_json(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("source-watch: unreadable manifest %s (%s)", path.name, exc)
            continue
        cid = str(m.get("id") or path.stem)
        out[cid] = m
    return out


def pinned_api_version(manifest: dict[str, Any]) -> str | None:
    """The API version this connector is actually built against.

    Three sources, in order of how authoritative they are:

    1. An explicit ``source_watch.api_version``, when a maintainer has stated
       it. Last resort for vendors whose version is neither sent nor in the URL.

    2. A version **request** header the manifest already sends. The GitHub
       manifest sets ``X-GitHub-Api-Version: 2022-11-28`` on every call, so
       that is the version the vendor serves us — reading the pin from the
       header that enforces it means the watched value cannot quietly diverge
       from the one in use, which a hand-copied field would.

    3. The version segment of ``base_url`` (``.../v1`` → ``v1``). Coarse, but
       it gives most connectors this signal with no manifest edit at all,
       which matters across dozens of them.
    """
    watch = manifest.get("source_watch")
    if isinstance(watch, dict) and watch.get("api_version"):
        return str(watch["api_version"])

    headers = manifest.get("headers")
    if isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() in _VERSION_HEADERS and v:
                return str(v)

    m = _VERSION_IN_URL.search(str(manifest.get("base_url") or ""))
    return m.group(1) if m else None


def normalize_path(path: str) -> str:
    """Reduce a path template to a shape comparable against an OpenAPI spec.

    Two mismatches make naive string comparison useless, and both were found
    against the vendors' real published specs rather than guessed at:

    1. **Parameter names differ.** Our GitHub manifest reads
       ``/users/{owner}/repos``; GitHub's own spec calls the same endpoint
       ``/users/{username}/repos``. The name inside the braces is the author's
       choice on both sides and carries no meaning for this comparison, so
       every placeholder collapses to ``{}``.

    2. **Trailing slashes** are inconsistent between vendors.

    Without this, every path of every connector reports as missing from its
    vendor's spec — a critical alert on every run, which is the fastest way to
    get a monitoring system muted and ignored.
    """
    p = re.sub(r"\{[^}]*\}", "{}", str(path).strip())
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def declared_paths(manifest: dict[str, Any]) -> list[str]:
    """Full, normalised endpoint paths this connector depends on.

    The path a stream declares is relative to ``base_url``, and ``base_url``
    frequently carries the API version segment: Stripe's manifest pairs
    ``https://api.stripe.com/v1`` with a stream path of ``/customers``, while
    Stripe's published spec lists that endpoint as ``/v1/customers``. Compare
    the stream path alone and nothing ever matches, so the prefix from
    ``base_url`` is joined back on here.
    """
    from urllib.parse import urlparse

    base = urlparse(str(manifest.get("base_url") or "")).path or ""
    base = base.rstrip("/")
    # Strip templating from the base too — `https://api.airtable.com/v0/{base_id}`
    # contributes `/v0/{}`, not a literal customer identifier.
    base = re.sub(r"\{[^}]*\}", "{}", base)

    out: list[str] = []
    for stream in manifest.get("streams") or []:
        if not isinstance(stream, dict) or not stream.get("path"):
            continue
        rel = str(stream["path"])
        if not rel.startswith("/"):
            rel = "/" + rel
        out.append(normalize_path(base + rel))
    return out


# ── Fetching ─────────────────────────────────────────────────────────────────


def default_fetcher(url: str) -> tuple[int, dict[str, str], str]:
    """SSRF-guarded GET, stdlib only.

    Reuses `rest_framework`'s guarded opener so a manifest cannot point a
    probe at link-local metadata or an internal host. Imported lazily to keep
    this module importable in contexts where the REST framework is not needed.
    """
    import urllib.request

    from ..security.ssrf import API_SOURCE_ALLOW_PRIVATE_ENV, check_url
    from .rest_framework import _GUARDED_OPENER  # local import — see docstring

    check_url(url, allow_private_env=API_SOURCE_ALLOW_PRIVATE_ENV)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json, text/html;q=0.8, */*;q=0.5"},
    )
    with _GUARDED_OPENER.open(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return int(resp.status), dict(resp.headers.items()), body


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Probes ───────────────────────────────────────────────────────────────────


def _extract(payload: Any, headers: dict[str, str], probe: dict[str, Any]) -> Any:
    """Pull what the vendor advertises out of a probe response.

    Returns the raw value, which may be a **list** of supported versions or a
    single version string — the distinction drives severity, see
    `probe_api_version`.

    Shapes:
      {"from": "header", "name": "X-GitHub-Api-Version"}
      {"from": "json", "pointer": ["versions", -1, "version"]}
      {"from": "json", "pointer": []}    -> the whole document
    """
    src = str(probe.get("from") or "header").lower()
    if src == "header":
        name = str(probe.get("name") or "")
        # Header lookup must be case-insensitive; vendors are inconsistent and
        # a case mismatch would silently report "no version advertised".
        for k, v in headers.items():
            if k.lower() == name.lower():
                return str(v)
        return None
    if src == "json":
        cur = payload
        for key in probe.get("pointer") or []:
            try:
                cur = cur[key] if isinstance(key, int) else cur[str(key)]
            except (KeyError, IndexError, TypeError):
                return None
        return cur
    return None


def parse_spec(body: str) -> Any:
    """Parse an API description that may be JSON or YAML.

    Roughly half the vendors who publish a spec publish only YAML — Asana,
    Intercom and Microsoft Graph among the ones this catalog connects to. JSON
    is tried first because it is the common case and much faster; PyYAML is a
    core dependency already used for exactly this in
    `connectors/openapi_source.py`.

    Returns `None` for anything that is not a mapping, which degrades the
    check to fingerprint-only: movement is still detected, but not which
    endpoint moved.

    The not-a-mapping guard is load-bearing rather than defensive. YAML
    parses an arbitrary document — an HTML error page, say — as a plain scalar
    string without complaint, so a vendor serving a 200-with-error-page would
    otherwise flow on as if it were a parsed spec.
    """
    parsed: Any = None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        try:
            import yaml  # local import — pyyaml is a core dep

            # safe_load, not load: a spec is third-party input, and full YAML
            # can construct arbitrary Python objects.
            parsed = yaml.safe_load(body)
        except Exception as exc:  # noqa: BLE001
            logger.debug("source-watch: spec is neither JSON nor YAML (%s)", exc)
            return None

    return parsed if isinstance(parsed, dict) else None


def spec_base_prefix(spec: dict[str, Any]) -> str:
    """The path prefix a spec's own paths are relative to.

    Vendors split evenly on this and get it from different fields, so both
    have to be read or half the connectors mismatch:

    * **Swagger 2** puts it in ``basePath``. Slack's spec has
      ``basePath: "/api"`` with paths like ``/users.list``; Mailchimp's has
      ``/3.0`` with ``/lists``.
    * **OpenAPI 3** puts it in ``servers[].url``, and the paths already carry
      whatever remains. Twilio's server is ``https://api.twilio.com`` with
      paths like ``/2010-04-01/Accounts.json``; Stripe's is
      ``https://api.stripe.com/`` with ``/v1/account``.

    Getting this wrong is not a cosmetic problem. Prepending our own
    ``base_url`` prefix unconditionally made Slack and Mailchimp report every
    endpoint as deleted, while omitting it made Stripe do the same — a daily
    critical on a healthy connector either way.
    """
    from urllib.parse import urlparse

    base = spec.get("basePath")
    if not base:
        servers = spec.get("servers") or []
        if servers and isinstance(servers[0], dict):
            base = urlparse(str(servers[0].get("url") or "")).path
    base = str(base or "").rstrip("/")
    return base if base != "/" else ""


def spec_path_set(spec: Any) -> set[str]:
    """Every endpoint a spec declares, as full normalised paths."""
    if not isinstance(spec, dict):
        return set()
    prefix = spec_base_prefix(spec)
    out: set[str] = set()
    for p in (spec.get("paths") or {}):
        p = str(p)
        if not p.startswith("/"):
            p = "/" + p
        out.add(normalize_path(prefix + p))
    return out


def probe_api_version(
    cid: str, manifest: dict[str, Any], fetch: Fetcher
) -> list[SourceSignal]:
    watch = manifest.get("source_watch") or {}
    probe = watch.get("version_probe")
    pinned = pinned_api_version(manifest)
    if not isinstance(probe, dict) or not probe.get("url"):
        return []

    try:
        status, headers, body = fetch(str(probe["url"]))
    except Exception as exc:  # noqa: BLE001
        return [SourceSignal(cid, "probe_error", "warn",
                             f"version probe failed: {type(exc).__name__}: {exc}")]
    if status >= 400:
        return [SourceSignal(cid, "probe_error", "warn",
                             f"version probe returned HTTP {status}")]

    try:
        payload = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    raw = _extract(payload, headers, probe)
    if raw is None or raw == []:
        return [SourceSignal(cid, "probe_error", "info",
                             "version probe succeeded but advertised no version")]

    # A list means the vendor publishes the whole SUPPORTED SET, which is the
    # only shape that can distinguish the two situations that matter:
    #
    #   * a newer version exists and ours is still supported — worth knowing,
    #     not yet a problem
    #   * our version is no longer in the set — we are on an unsupported API
    #
    # Collapsing those into "newest != ours → critical" was wrong, and wrong
    # in the expensive direction. GitHub lists ["2026-03-10", "2022-11-28"]:
    # both are supported, ours among them, nothing is broken. Alerting
    # critical on that is the cry-wolf failure this module is supposed to
    # avoid — and a critical that fires on a healthy connector trains everyone
    # to ignore the one that fires on a broken one.
    if isinstance(raw, list):
        supported = [str(v) for v in raw if v is not None]
        newest = supported[0] if supported else None
        if not pinned:
            return [SourceSignal(cid, "api_version", "info",
                                 f"vendor supports {len(supported)} versions, "
                                 f"newest {newest!r}; this connector pins none",
                                 observed=newest)]
        if not any(pinned.strip().lower() == s.strip().lower() for s in supported):
            return [SourceSignal(
                cid, "api_version", "critical",
                f"this connector is pinned to API version {pinned!r}, which the "
                f"vendor no longer lists as supported (supported: "
                f"{', '.join(supported)})",
                pinned=pinned, observed=newest,
            )]
        if newest and newest.strip().lower() != pinned.strip().lower():
            return [SourceSignal(
                cid, "api_version", "warn",
                f"vendor has released API version {newest!r}; this connector "
                f"pins {pinned!r}, which is still supported",
                pinned=pinned, observed=newest,
            )]
        return [SourceSignal(cid, "api_version", "info",
                             f"pinned to the newest supported version ({pinned})",
                             pinned=pinned, observed=newest)]

    # A scalar tells us what the vendor serves *now* but nothing about whether
    # our version is still accepted, so a mismatch cannot be graded critical
    # on this evidence alone.
    observed = str(raw)
    if pinned and observed.strip().lower() != pinned.strip().lower():
        return [SourceSignal(
            cid, "api_version", "warn",
            f"vendor advertises API version {observed!r}; this connector is "
            f"pinned to {pinned!r} (support for it not confirmed by this probe)",
            pinned=pinned, observed=observed,
        )]
    return [SourceSignal(cid, "api_version", "info",
                         f"API version unchanged ({observed})",
                         pinned=pinned, observed=observed)]


def probe_spec(
    cid: str, manifest: dict[str, Any], fetch: Fetcher, baseline: dict[str, Any]
) -> list[SourceSignal]:
    """Fetch the vendor's API description; check our paths still exist in it."""
    watch = manifest.get("source_watch") or {}
    url = watch.get("spec_url")
    if not url:
        return []

    try:
        status, _headers, body = fetch(str(url))
    except Exception as exc:  # noqa: BLE001
        return [SourceSignal(cid, "probe_error", "warn",
                             f"spec fetch failed: {type(exc).__name__}: {exc}")]
    if status >= 400:
        return [SourceSignal(cid, "probe_error", "warn",
                             f"spec fetch returned HTTP {status}")]

    signals: list[SourceSignal] = []
    fp = _fingerprint(body)
    prior = (baseline.get(cid) or {}).get("spec_sha256")

    # A missing path is the actionable finding — it names the stream that is
    # about to break. Report it whether or not the fingerprint moved, because
    # a first run has no fingerprint to compare against.
    spec_paths = spec_path_set(parse_spec(body))

    if spec_paths:
        missing = [p for p in declared_paths(manifest) if p not in spec_paths]
        if missing:
            signals.append(SourceSignal(
                cid, "spec", "critical",
                "endpoints this connector reads are absent from the vendor's "
                f"published spec: {', '.join(sorted(missing))}",
                observed=f"{len(spec_paths)} paths in spec",
            ))

    if prior and prior != fp:
        signals.append(SourceSignal(
            cid, "spec", "warn",
            "vendor's published API spec changed since the last run",
            pinned=prior[:12], observed=fp[:12], fingerprint=fp,
        ))
    elif not prior:
        signals.append(SourceSignal(
            cid, "spec", "info", "spec fingerprint recorded for the first time",
            observed=fp[:12], fingerprint=fp,
        ))
    else:
        signals.append(SourceSignal(
            cid, "spec", "info", "spec unchanged",
            observed=fp[:12], fingerprint=fp,
        ))
    return signals


def probe_changelog(
    cid: str, manifest: dict[str, Any], fetch: Fetcher, baseline: dict[str, Any]
) -> list[SourceSignal]:
    watch = manifest.get("source_watch") or {}
    url = watch.get("changelog_url")
    if not url:
        return []

    try:
        status, _headers, body = fetch(str(url))
    except Exception as exc:  # noqa: BLE001
        return [SourceSignal(cid, "probe_error", "warn",
                             f"changelog fetch failed: {type(exc).__name__}: {exc}")]
    if status >= 400:
        return [SourceSignal(cid, "probe_error", "warn",
                             f"changelog fetch returned HTTP {status}")]

    fp = _fingerprint(body)
    prior = (baseline.get(cid) or {}).get("changelog_sha256")
    if prior and prior != fp:
        return [SourceSignal(
            cid, "changelog", "warn",
            f"vendor published changelog activity since the last run — review {url}",
            pinned=prior[:12], observed=fp[:12], fingerprint=fp,
        )]
    if not prior:
        return [SourceSignal(cid, "changelog", "info",
                             "changelog fingerprint recorded for the first time",
                             observed=fp[:12], fingerprint=fp)]
    return [SourceSignal(cid, "changelog", "info", "no changelog activity",
                         observed=fp[:12], fingerprint=fp)]


# ── Passive detection: the vendor telling us, on traffic we already make ─────
#
# The probes above are polite: they ask, on a schedule, without credentials.
# This one asks nothing. Every REST response the product already receives may
# carry the vendor's own deprecation notice, and until now we read past it.
#
# RFC 8594 defines `Deprecation` and `Sunset`; RFC 9111 keeps `Warning`
# around; and most vendors that version their API echo the serving version in
# a header. Harvesting those from live responses is the earliest warning
# available anywhere — earlier than a changelog, because it is addressed to
# this caller specifically, about the endpoint it just called.
#
# It is also the only detector that works for a connector whose vendor
# publishes no spec and no changelog, and for a customer's own private or
# internal API, which no scheduled probe of ours could ever know about.
#
# Constraints this must respect:
#   * Never raise. A malformed header must not fail a customer's pipeline.
#   * Never make a request. The whole point is that it is free.
#   * Bounded memory. A long-running instance makes millions of calls, so the
#     sink keeps one entry per (host, header-set) rather than per response.

_DEPRECATION_HEADERS = ("deprecation", "sunset", "warning")

# {host: {"deprecation": ..., "sunset": ..., "warning": ..., "version": ...,
#         "first_seen": iso, "last_seen": iso, "observations": int}}
_OBSERVED: dict[str, dict[str, Any]] = {}

_OBSERVED_HOST_CAP = 256


def observe_response(url: str, headers: dict[str, str]) -> None:
    """Record any upstream-change signal a live response carried.

    Called from `rest_framework._http_request`, which every framework fetch
    funnels through. Silent and allocation-light on the overwhelmingly common
    case where the vendor said nothing.
    """
    try:
        lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        found: dict[str, Any] = {}

        for name in _DEPRECATION_HEADERS:
            if lowered.get(name):
                found[name] = lowered[name][:500]
        for name in _VERSION_HEADERS:
            if lowered.get(name):
                found["version"] = lowered[name][:120]
                found["version_header"] = name
                break

        if not found:
            return

        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "unknown").lower()

        now = datetime.now(timezone.utc).isoformat()
        entry = _OBSERVED.get(host)
        if entry is None:
            # Bound the sink. Dropping new hosts rather than evicting old ones
            # keeps the longest-standing deprecation notices, which are the
            # ones closest to their sunset date.
            if len(_OBSERVED) >= _OBSERVED_HOST_CAP:
                return
            entry = {"first_seen": now, "observations": 0}
            _OBSERVED[host] = entry
        entry.update(found)
        entry["last_seen"] = now
        entry["observations"] = int(entry.get("observations", 0)) + 1
    except Exception:  # noqa: BLE001 — must never break a running pipeline
        return


def observed_signals() -> list[SourceSignal]:
    """Passive observations, as signals comparable to the probe results."""
    out: list[SourceSignal] = []
    for host, e in sorted(_OBSERVED.items()):
        notice = " · ".join(
            f"{h.title()}: {e[h]}" for h in _DEPRECATION_HEADERS if e.get(h)
        )
        if notice:
            # `Sunset` carries a date: the vendor has committed to switching
            # this off. That is the most urgent thing this module can report.
            severity = "critical" if e.get("sunset") else "warn"
            out.append(SourceSignal(
                host, "deprecation", severity,
                f"{host} returned a deprecation notice on live traffic — {notice}",
                observed=e.get("version"),
            ))
        elif e.get("version"):
            out.append(SourceSignal(
                host, "api_version", "info",
                f"{host} served API version {e['version']} "
                f"(via {e.get('version_header')})",
                observed=e["version"],
            ))
    return out


def reset_observations() -> None:
    """Clear the passive sink. For tests and for an operator-triggered reset."""
    _OBSERVED.clear()


# ── Runner ───────────────────────────────────────────────────────────────────


def _read_baseline() -> dict[str, Any]:
    p = baseline_path()
    if not p.is_file():
        return {}
    try:
        data = _load_json(p)
        return data.get("connectors", {}) if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def watched_connectors() -> list[str]:
    """Connectors that declare where to look. The rest cannot be watched.

    Deliberately not "all connectors": a connector with no `source_watch`
    block is unwatched, and saying so is more useful than pretending a silent
    connector is a healthy one.
    """
    return sorted(
        cid for cid, m in runtime_manifests().items()
        if isinstance(m.get("source_watch"), dict) and m["source_watch"]
    )


def run_source_watch(
    *,
    connector_ids: Iterable[str] | None = None,
    fetch: Fetcher | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Probe every watched connector and summarise what moved.

    Returns the status document. `worst_severity` is what CI gates on;
    `critical` means a connector is pinned to something the vendor has moved
    past, or reads an endpoint their own spec no longer lists.
    """
    fetch = fetch or default_fetcher
    manifests = runtime_manifests()
    baseline = _read_baseline()

    targets = (
        [c for c in connector_ids if c in manifests]
        if connector_ids is not None
        else watched_connectors()
    )

    signals: list[SourceSignal] = []
    new_baseline: dict[str, Any] = dict(baseline)

    for cid in targets:
        manifest = manifests[cid]
        watch = manifest.get("source_watch") or {}
        if not isinstance(watch, dict) or not watch:
            continue

        produced: list[SourceSignal] = []
        produced.extend(probe_api_version(cid, manifest, fetch))
        produced.extend(probe_spec(cid, manifest, fetch, baseline))
        produced.extend(probe_changelog(cid, manifest, fetch, baseline))
        signals.extend(produced)

        # Re-fingerprint for next time, from this connector's signals only. A
        # probe that failed produces no fingerprint, so its previous baseline
        # entry survives untouched — otherwise a single vendor outage would
        # wipe the comparison point and the next real change would read as a
        # harmless "first run".
        entry = dict(new_baseline.get(cid) or {})
        for s in produced:
            if s.kind == "spec" and s.fingerprint:
                entry["spec_sha256"] = s.fingerprint
            elif s.kind == "changelog" and s.fingerprint:
                entry["changelog_sha256"] = s.fingerprint
            elif s.kind == "api_version" and s.observed:
                entry["api_version"] = s.observed
        if entry:
            new_baseline[cid] = entry

    worst = "info"
    for s in signals:
        if _SEVERITY_ORDER[s.severity] > _SEVERITY_ORDER[worst]:
            worst = s.severity

    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "watched": len(targets),
        "unwatched": sorted(set(manifests) - set(watched_connectors())),
        "worst_severity": worst,
        "signals": [s.as_dict() for s in signals],
    }

    if write:
        _ci_dir().mkdir(parents=True, exist_ok=True)
        status_path().write_text(
            json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        baseline_path().write_text(
            json.dumps(
                {"updated_at": status["generated_at"], "connectors": new_baseline},
                indent=2, sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
    return status
