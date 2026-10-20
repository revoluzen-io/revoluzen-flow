"""Source Watch — upstream-change detection for connectors.

Everything here runs offline through an injected fetcher. The module's job is
to make network calls, so tests that made them would be slow, flaky, and
dependent on a vendor's mood.

Several of these are regression tests for bugs found by pointing the detector
at the vendors' real published specs. They are called out individually,
because each one would have made the whole feature worse than useless: a
monitor that cries wolf daily gets muted, and a muted monitor is indistinguish-
able from no monitor while looking like due diligence.
"""

from __future__ import annotations

import json

import pytest

from revoluzen_flow.connectors import source_watch as sw


# ── Fake fetcher ─────────────────────────────────────────────────────────────


def fetcher(routes: dict[str, tuple[int, dict[str, str], str]]):
    """Build a fetcher over a {url: (status, headers, body)} table.

    A URL that is not in the table raises, which is how a vendor outage is
    simulated — the real `default_fetcher` raises on connection failure too.
    """

    def _fetch(url: str):
        if url not in routes:
            raise ConnectionError(f"no route for {url}")
        return routes[url]

    return _fetch


# ── Path normalisation ───────────────────────────────────────────────────────


def test_normalize_path_collapses_parameter_names():
    """Our parameter names and the vendor's are both arbitrary.

    REGRESSION: our GitHub manifest declares `/users/{owner}/repos`; GitHub's
    own spec calls it `/users/{username}/repos`. Comparing raw strings marked
    every GitHub stream as deleted from the vendor's API.
    """
    assert sw.normalize_path("/users/{owner}/repos") == "/users/{}/repos"
    assert sw.normalize_path("/users/{username}/repos") == "/users/{}/repos"
    assert sw.normalize_path("/repos/{owner}/{repo}/issues") == "/repos/{}/{}/issues"


def test_normalize_path_ignores_trailing_slash():
    assert sw.normalize_path("/customers/") == "/customers"
    assert sw.normalize_path("/") == "/"


def test_declared_paths_joins_the_base_url_prefix():
    """REGRESSION: the version segment lives in `base_url`, not the stream path.

    Stripe's manifest pairs `https://api.stripe.com/v1` with a stream path of
    `/customers`, while Stripe's published spec lists `/v1/customers`.
    Comparing the stream path alone matched nothing, so every connector
    reported all of its endpoints missing — a critical alert, every run,
    forever.
    """
    manifest = {
        "base_url": "https://api.stripe.com/v1",
        "streams": [{"name": "customers", "path": "/customers"}],
    }
    assert sw.declared_paths(manifest) == ["/v1/customers"]


def test_declared_paths_strips_templating_from_the_base_url():
    """`https://api.airtable.com/v0/{base_id}` contributes `/v0/{}`."""
    manifest = {
        "base_url": "https://api.airtable.com/v0/{base_id}",
        "streams": [{"name": "records", "path": "/{table_name}"}],
    }
    assert sw.declared_paths(manifest) == ["/v0/{}/{}"]


def test_declared_paths_tolerates_a_path_missing_its_leading_slash():
    manifest = {"base_url": "https://x.test/v2", "streams": [{"path": "things"}]}
    assert sw.declared_paths(manifest) == ["/v2/things"]


# ── Pinned version resolution ────────────────────────────────────────────────


def test_pinned_version_prefers_an_explicit_declaration():
    manifest = {
        "source_watch": {"api_version": "2026-01-01"},
        "headers": {"X-GitHub-Api-Version": "2022-11-28"},
        "base_url": "https://x.test/v9",
    }
    assert sw.pinned_api_version(manifest) == "2026-01-01"


def test_pinned_version_reads_the_request_header_that_enforces_it():
    """The header the manifest actually sends is the version in force.

    Reading the pin from there rather than a hand-copied field means the
    watched value cannot silently diverge from the one being used.
    """
    manifest = {
        "headers": {"X-GitHub-Api-Version": "2022-11-28"},
        "base_url": "https://api.github.com",
    }
    assert sw.pinned_api_version(manifest) == "2022-11-28"


@pytest.mark.parametrize(
    "base_url,expected",
    [
        ("https://api.stripe.com/v1", "v1"),
        ("https://api.airtable.com/v0/{base_id}", "v0"),
        ("https://x.salesforce.com/services/data/v59.0", "v59.0"),
        ("https://graph.microsoft.com/beta", "beta"),
        ("https://api.example.com", None),
        ("", None),
    ],
)
def test_pinned_version_falls_back_to_the_base_url_segment(base_url, expected):
    assert sw.pinned_api_version({"base_url": base_url}) == expected


# ── Version probe ────────────────────────────────────────────────────────────


GH_VERSIONS = "https://api.github.com/versions"


def _gh_manifest(pinned: str = "2022-11-28") -> dict:
    return {
        "base_url": "https://api.github.com",
        "headers": {"X-GitHub-Api-Version": pinned},
        "source_watch": {
            "version_probe": {"url": GH_VERSIONS, "from": "json", "pointer": [0]}
        },
        "streams": [],
    }


def test_version_probe_flags_a_newer_vendor_version_as_critical():
    """The signal the whole feature exists for."""
    f = fetcher({GH_VERSIONS: (200, {}, json.dumps(["2026-03-10", "2022-11-28"]))})
    signals = sw.probe_api_version("github", _gh_manifest(), f)

    assert len(signals) == 1
    s = signals[0]
    assert (s.kind, s.severity) == ("api_version", "critical")
    assert s.pinned == "2022-11-28"
    assert s.observed == "2026-03-10"
    # Both versions must appear — an alert that omits either is not actionable.
    assert "2026-03-10" in s.summary and "2022-11-28" in s.summary


def test_version_probe_is_quiet_when_the_version_still_matches():
    f = fetcher({GH_VERSIONS: (200, {}, json.dumps(["2022-11-28"]))})
    (s,) = sw.probe_api_version("github", _gh_manifest(), f)
    assert (s.kind, s.severity) == ("api_version", "info")


def test_version_probe_reads_a_response_header_case_insensitively():
    """Vendors are inconsistent about header casing.

    A case-sensitive lookup would report "advertised no version" and the real
    mismatch would never surface.
    """
    manifest = {
        "base_url": "https://api.github.com",
        "source_watch": {
            "api_version": "2022-11-28",
            "version_probe": {
                "url": GH_VERSIONS,
                "from": "header",
                "name": "X-GitHub-Api-Version-Selected",
            },
        },
    }
    f = fetcher({GH_VERSIONS: (200, {"x-github-api-version-selected": "2026-03-10"}, "")})
    (s,) = sw.probe_api_version("github", manifest, f)
    assert s.severity == "critical"
    assert s.observed == "2026-03-10"


def test_version_probe_reports_an_outage_as_a_warning_not_a_breakage():
    """A vendor being down is not a vendor having changed their API."""
    (s,) = sw.probe_api_version("github", _gh_manifest(), fetcher({}))
    assert (s.kind, s.severity) == ("probe_error", "warn")


def test_version_probe_reports_an_http_error_as_a_probe_error():
    f = fetcher({GH_VERSIONS: (503, {}, "")})
    (s,) = sw.probe_api_version("github", _gh_manifest(), f)
    assert (s.kind, s.severity) == ("probe_error", "warn")
    assert "503" in s.summary


def test_version_probe_is_skipped_when_no_probe_is_declared():
    assert sw.probe_api_version("x", {"base_url": "https://x.test/v1"}, fetcher({})) == []


def test_version_probe_handles_a_pointer_that_does_not_resolve():
    manifest = _gh_manifest()
    manifest["source_watch"]["version_probe"]["pointer"] = ["nope", "missing"]
    f = fetcher({GH_VERSIONS: (200, {}, json.dumps(["2026-03-10"]))})
    (s,) = sw.probe_api_version("github", manifest, f)
    assert (s.kind, s.severity) == ("probe_error", "info")


# ── Spec probe ───────────────────────────────────────────────────────────────


SPEC_URL = "https://spec.test/openapi.json"


def _spec_manifest() -> dict:
    return {
        "base_url": "https://api.stripe.com/v1",
        "source_watch": {"spec_url": SPEC_URL},
        "streams": [{"name": "customers", "path": "/customers"}],
    }


def _spec_body(paths: list[str]) -> str:
    return json.dumps({"paths": {p: {"get": {}} for p in paths}})


def test_spec_probe_flags_an_endpoint_that_left_the_vendors_spec():
    """A path we read disappearing from the vendor's own spec is breaking."""
    f = fetcher({SPEC_URL: (200, {}, _spec_body(["/v1/charges"]))})
    signals = sw.probe_spec("stripe", _spec_manifest(), f, {})

    critical = [s for s in signals if s.severity == "critical"]
    assert len(critical) == 1
    assert "/v1/customers" in critical[0].summary


def test_spec_probe_matches_our_paths_against_the_vendors_naming():
    """End-to-end of both normalisations, against GitHub's real path shape."""
    manifest = {
        "base_url": "https://api.github.com",
        "source_watch": {"spec_url": SPEC_URL},
        "streams": [{"name": "repos", "path": "/users/{owner}/repos"}],
    }
    f = fetcher({SPEC_URL: (200, {}, _spec_body(["/users/{username}/repos"]))})
    signals = sw.probe_spec("github", manifest, f, {})
    assert [s for s in signals if s.severity == "critical"] == []


def test_spec_probe_warns_when_the_document_changed():
    baseline = {"stripe": {"spec_sha256": "a" * 64}}
    f = fetcher({SPEC_URL: (200, {}, _spec_body(["/v1/customers"]))})
    signals = sw.probe_spec("stripe", _spec_manifest(), f, baseline)
    assert [s.severity for s in signals if s.kind == "spec"] == ["warn"]


def test_spec_probe_is_quiet_when_the_document_is_byte_identical():
    body = _spec_body(["/v1/customers"])
    import hashlib

    fp = hashlib.sha256(body.encode()).hexdigest()
    signals = sw.probe_spec(
        "stripe", _spec_manifest(), fetcher({SPEC_URL: (200, {}, body)}),
        {"stripe": {"spec_sha256": fp}},
    )
    assert [s.severity for s in signals if s.kind == "spec"] == ["info"]


def test_spec_signals_carry_the_full_digest_not_the_display_prefix():
    """REGRESSION: the baseline must store a comparable hash.

    `observed` holds a 12-character prefix so the status file reads well. An
    earlier version stored that prefix as the baseline, and since a 12-char
    prefix can never equal a 64-char digest, every later run reported the spec
    as changed — a permanent false alarm.
    """
    f = fetcher({SPEC_URL: (200, {}, _spec_body(["/v1/customers"]))})
    (spec_signal,) = [
        s for s in sw.probe_spec("stripe", _spec_manifest(), f, {}) if s.kind == "spec"
    ]
    assert len(spec_signal.observed) == 12
    assert len(spec_signal.fingerprint) == 64
    assert spec_signal.fingerprint.startswith(spec_signal.observed)


# ── Changelog probe ──────────────────────────────────────────────────────────


FEED = "https://vendor.test/changelog/feed"


def test_changelog_probe_warns_on_new_vendor_activity():
    manifest = {"source_watch": {"changelog_url": FEED}}
    f = fetcher({FEED: (200, {}, "<rss>brand new entry</rss>")})
    (s,) = sw.probe_changelog("v", manifest, f, {"v": {"changelog_sha256": "b" * 64}})
    assert (s.kind, s.severity) == ("changelog", "warn")
    assert FEED in s.summary  # the alert must say where to look


def test_changelog_probe_is_skipped_when_undeclared():
    assert sw.probe_changelog("v", {"source_watch": {}}, fetcher({}), {}) == []


# ── Passive detection ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_sink():
    sw.reset_observations()
    yield
    sw.reset_observations()


def test_sunset_header_is_critical_because_the_vendor_committed_to_a_date():
    sw.observe_response(
        "https://api.vendor.test/v1/things",
        {"Deprecation": "true", "Sunset": "Sat, 31 Oct 2026 23:59:59 GMT"},
    )
    (s,) = sw.observed_signals()
    assert (s.kind, s.severity) == ("deprecation", "critical")
    assert "api.vendor.test" == s.connector_id
    assert "31 Oct 2026" in s.summary


def test_deprecation_without_a_sunset_date_is_a_warning():
    sw.observe_response("https://api.vendor.test/v1/x", {"Deprecation": "true"})
    (s,) = sw.observed_signals()
    assert s.severity == "warn"


def test_a_served_version_header_is_recorded_as_information():
    sw.observe_response("https://api.github.com/x", {"X-GitHub-Api-Version": "2026-03-10"})
    (s,) = sw.observed_signals()
    assert (s.kind, s.severity) == ("api_version", "info")
    assert s.observed == "2026-03-10"


def test_an_ordinary_response_records_nothing():
    sw.observe_response("https://api.vendor.test/v1/x", {"Content-Type": "application/json"})
    assert sw.observed_signals() == []


def test_observe_response_never_raises_on_hostile_input():
    """This sits on the hot path of every connector fetch.

    A malformed header, or a header value that is not a string, must never be
    the reason a customer's pipeline fails.
    """
    for url, headers in [
        ("not a url", {"Deprecation": "true"}),
        ("https://x.test", {None: "true"}),          # type: ignore[dict-item]
        ("https://x.test", {"Sunset": object()}),    # type: ignore[dict-item]
        ("", {}),
    ]:
        sw.observe_response(url, headers)  # must not raise


def test_the_passive_sink_is_bounded():
    """A long-running instance makes millions of calls; memory must not grow."""
    for i in range(sw._OBSERVED_HOST_CAP + 50):
        sw.observe_response(f"https://host{i}.test/x", {"Deprecation": "true"})
    assert len(sw._OBSERVED) == sw._OBSERVED_HOST_CAP


def test_repeat_observations_of_one_host_collapse_to_a_single_entry():
    for _ in range(25):
        sw.observe_response("https://api.vendor.test/v1/x", {"Deprecation": "true"})
    assert len(sw._OBSERVED) == 1
    assert sw._OBSERVED["api.vendor.test"]["observations"] == 25


# ── Runner ───────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_ci(tmp_path, monkeypatch):
    """Redirect the baseline/status files so a run cannot touch the real ones."""
    monkeypatch.setattr(sw, "_ci_dir", lambda: tmp_path)
    return tmp_path


def test_run_writes_a_status_and_a_baseline(isolated_ci, monkeypatch):
    monkeypatch.setattr(sw, "runtime_manifests", lambda: {"stripe": _spec_manifest()})
    f = fetcher({SPEC_URL: (200, {}, _spec_body(["/v1/customers"]))})

    status = sw.run_source_watch(fetch=f, write=True)

    assert status["worst_severity"] == "info"
    assert status["watched"] == 1
    assert sw.status_path().is_file()

    baseline = json.loads(sw.baseline_path().read_text(encoding="utf-8"))
    stored = baseline["connectors"]["stripe"]["spec_sha256"]
    assert len(stored) == 64, "baseline must store the full digest"


def test_worst_severity_is_the_highest_signal_present(isolated_ci, monkeypatch):
    monkeypatch.setattr(sw, "runtime_manifests", lambda: {"github": _gh_manifest()})
    f = fetcher({GH_VERSIONS: (200, {}, json.dumps(["2026-03-10"]))})
    status = sw.run_source_watch(fetch=f, write=False)
    assert status["worst_severity"] == "critical"


def test_a_vendor_outage_leaves_the_previous_baseline_intact(isolated_ci, monkeypatch):
    """REGRESSION: an outage must not reset the comparison point.

    If a failed fetch wiped the stored fingerprint, the next successful run
    would report "recorded for the first time" — so a real change arriving
    during or right after an outage would be silently absorbed.
    """
    monkeypatch.setattr(sw, "runtime_manifests", lambda: {"stripe": _spec_manifest()})
    body = _spec_body(["/v1/customers"])
    sw.run_source_watch(fetch=fetcher({SPEC_URL: (200, {}, body)}), write=True)
    first = json.loads(sw.baseline_path().read_text(encoding="utf-8"))
    kept = first["connectors"]["stripe"]["spec_sha256"]

    # Now the vendor is unreachable.
    status = sw.run_source_watch(fetch=fetcher({}), write=True)
    assert status["worst_severity"] == "warn"

    after = json.loads(sw.baseline_path().read_text(encoding="utf-8"))
    assert after["connectors"]["stripe"]["spec_sha256"] == kept


def test_unwatched_connectors_are_named_rather_than_assumed_healthy(
    isolated_ci, monkeypatch
):
    """Silence from a connector nobody watches is not good news."""
    monkeypatch.setattr(
        sw, "runtime_manifests",
        lambda: {"stripe": _spec_manifest(), "legacy": {"base_url": "https://l.test/v1"}},
    )
    status = sw.run_source_watch(
        fetch=fetcher({SPEC_URL: (200, {}, _spec_body(["/v1/customers"]))}), write=False
    )
    assert status["unwatched"] == ["legacy"]


# ── Shipped manifests ────────────────────────────────────────────────────────


def test_shipped_watch_blocks_are_well_formed():
    """Every declared probe must be usable by the engine.

    A typo'd `from`, or a probe with no URL, would make a connector look
    watched while reporting nothing — the worst outcome available.
    """
    watched = sw.watched_connectors()
    assert watched, "expected at least one connector to declare source_watch"

    for cid in watched:
        watch = sw.runtime_manifests()[cid]["source_watch"]
        probe = watch.get("version_probe")
        if probe is not None:
            assert probe.get("url"), f"{cid}: version_probe without a url"
            assert probe.get("from") in ("header", "json"), f"{cid}: bad probe source"
            if probe["from"] == "header":
                assert probe.get("name"), f"{cid}: header probe without a name"
            else:
                assert probe.get("pointer"), f"{cid}: json probe without a pointer"
        for key in ("spec_url", "changelog_url"):
            if watch.get(key):
                assert str(watch[key]).startswith("https://"), f"{cid}: {key} not https"


def test_every_watched_connector_resolves_a_pinned_version():
    """A version probe with nothing to compare against cannot alert."""
    for cid in sw.watched_connectors():
        manifest = sw.runtime_manifests()[cid]
        if (manifest.get("source_watch") or {}).get("version_probe"):
            assert sw.pinned_api_version(manifest), f"{cid}: probe but no pinned version"
