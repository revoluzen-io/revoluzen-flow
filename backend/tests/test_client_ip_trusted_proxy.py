"""Trusted-proxy client-IP resolution (2026-10-23 audit).

Three real defects motivated this, all from reading ``request.client.host``
directly while deployed behind a load balancer:

  * the login throttle keyed every request to the proxy's address, so one
    attacker locked out every user;
  * audit rows all recorded the proxy, losing attribution;
  * ``_is_local_request`` saw a same-host nginx as loopback, which made the
    loopback-only ``POST /api/system/shutdown`` remotely reachable.

The invariants worth protecting are: trust nothing by default, never let a
caller-supplied header widen trust, and never call a forwarded request
local.
"""

from __future__ import annotations

import pytest

from revoluzen_flow.common.client_ip import (
    ClientAddress,
    client_ip,
    is_loopback_client,
    resolve_client,
    trusted_proxy_networks,
)


class _Headers(dict):
    def get(self, key, default=""):      # case-insensitive, like Starlette
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _Client:
    def __init__(self, host): self.host = host


class _Req:
    def __init__(self, peer, **headers):
        self.client = _Client(peer) if peer is not None else None
        self.headers = _Headers(headers)


# ── default posture: trust nothing ──────────────────────────────────────

def test_no_trusted_proxies_means_header_is_ignored(monkeypatch):
    monkeypatch.delenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", raising=False)
    req = _Req("203.0.113.9", **{"X-Forwarded-For": "1.2.3.4"})
    assert resolve_client(req) == ClientAddress("203.0.113.9", False)


def test_spoofed_header_from_an_untrusted_peer_is_ignored(monkeypatch):
    """The attack this guards: anyone can send X-Forwarded-For."""
    monkeypatch.setenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", "10.0.0.0/8")
    req = _Req("203.0.113.9", **{"X-Forwarded-For": "10.0.0.1, 127.0.0.1"})
    assert client_ip(req) == "203.0.113.9"


def test_missing_client_is_empty_not_an_error():
    assert resolve_client(_Req(None)) == ClientAddress("", False)


# ── with a trusted proxy ────────────────────────────────────────────────

def test_trusted_proxy_yields_the_forwarded_client(monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", "10.0.0.0/8")
    req = _Req("10.0.0.7", **{"X-Forwarded-For": "198.51.100.23"})
    assert resolve_client(req) == ClientAddress("198.51.100.23", True)


def test_rightmost_untrusted_hop_wins(monkeypatch):
    """A caller may prepend fake hops on the left; it cannot remove the
    real ones our proxies appended on the right."""
    monkeypatch.setenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", "10.0.0.0/8")
    req = _Req("10.0.0.7", **{
        "X-Forwarded-For": "1.1.1.1, 198.51.100.23, 10.0.0.3",
    })
    assert client_ip(req) == "198.51.100.23"


def test_trusted_proxy_without_a_header_falls_back_to_the_peer(monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", "10.0.0.0/8")
    assert resolve_client(_Req("10.0.0.7")) == ClientAddress("10.0.0.7", False)


def test_ports_are_stripped(monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", "10.0.0.0/8")
    req = _Req("10.0.0.7", **{"X-Forwarded-For": "198.51.100.23:51234"})
    assert client_ip(req) == "198.51.100.23"


def test_bracketed_ipv6_survives(monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", "10.0.0.0/8")
    req = _Req("10.0.0.7", **{"X-Forwarded-For": "[2001:db8::1]:443"})
    assert client_ip(req) == "2001:db8::1"


# ── config robustness ───────────────────────────────────────────────────

def test_a_malformed_entry_is_dropped_not_widened(monkeypatch):
    """A typo must not become 'trust everything', and must not crash."""
    monkeypatch.setenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", "not-an-ip, 10.0.0.0/8, ")
    nets = trusted_proxy_networks()
    assert len(nets) == 1
    req = _Req("203.0.113.9", **{"X-Forwarded-For": "1.2.3.4"})
    assert client_ip(req) == "203.0.113.9"


def test_host_bits_are_tolerated(monkeypatch):
    monkeypatch.setenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", "10.0.0.5/8")
    assert client_ip(_Req("10.1.2.3", **{"X-Forwarded-For": "9.9.9.9"})) == "9.9.9.9"


# ── the loopback gate (POST /api/system/shutdown) ───────────────────────

def test_plain_loopback_is_local(monkeypatch):
    monkeypatch.delenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", raising=False)
    assert is_loopback_client(_Req("127.0.0.1")) is True
    assert is_loopback_client(_Req("::1")) is True


def test_remote_peer_is_not_local(monkeypatch):
    monkeypatch.delenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", raising=False)
    assert is_loopback_client(_Req("203.0.113.9")) is False


def test_same_host_proxy_does_not_make_a_remote_caller_local(monkeypatch):
    """The shutdown bypass. nginx on 127.0.0.1 forwarding a public request
    must not be mistaken for someone sitting at the machine."""
    monkeypatch.setenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", "127.0.0.1")
    req = _Req("127.0.0.1", **{"X-Forwarded-For": "203.0.113.9"})
    assert resolve_client(req).via_proxy is True
    assert is_loopback_client(req) is False


def test_untrusted_peer_cannot_forge_its_way_to_local(monkeypatch):
    monkeypatch.delenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", raising=False)
    req = _Req("203.0.113.9", **{"X-Forwarded-For": "127.0.0.1"})
    assert is_loopback_client(req) is False


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1"])
def test_loopback_without_proxies_configured_is_unchanged(monkeypatch, peer):
    """Default install: identical to the old peer-only check."""
    monkeypatch.delenv("REVOLUZEN_FLOW_TRUSTED_PROXIES", raising=False)
    req = _Req(peer, **{"X-Forwarded-For": "203.0.113.9"})
    assert is_loopback_client(req) is True
