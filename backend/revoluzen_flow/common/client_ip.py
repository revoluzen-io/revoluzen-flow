"""Resolve the real client IP, with opt-in trust for a reverse proxy.

Background (2026-10-23 audit)
-----------------------------
Every caller in this codebase reads ``request.client.host`` directly. That
is the safe default -- ``X-Forwarded-For`` is caller-supplied and trusting
it blindly lets anyone forge their own address -- but it is wrong in the
one deployment shape the product documents for self-hosting: behind nginx,
Traefik, a k8s ingress or any load balancer.

Three things break there, and all three are the same bug:

* **Login throttling** (``api/auth.py``) keys on ``request.client.host``.
  Behind a proxy every request carries the proxy's address, so the
  per-attacker throttle becomes one global throttle: a single attacker
  trips it and locks out every legitimate user. The brute-force control
  turns into a denial-of-service lever.
* **Audit attribution.** Every ``rbac_denied`` / login row records the
  proxy's IP, so the audit trail cannot distinguish two users, which is
  most of the point of recording an IP.
* **The loopback-only gate.** ``local_hardening._is_local_request`` treats
  a loopback peer as trusted, and ``POST /api/system/shutdown`` relies on
  it. A reverse proxy on the same host -- the normal deployment -- makes
  *every* request appear to come from 127.0.0.1, so that endpoint becomes
  a remote unauthenticated shutdown. (The function's own docstring already
  claimed it honoured forwarded headers "only when the immediate peer is
  loopback"; the code never did.)

The rule implemented here
-------------------------
``X-Forwarded-For`` is honoured **only** when the immediate TCP peer is an
address the operator has explicitly declared as a trusted proxy, via::

    REVOLUZEN_FLOW_TRUSTED_PROXIES=10.0.0.0/8,127.0.0.1

Empty (the default) means no proxy is trusted and the peer address is used
verbatim -- identical to today's behaviour, so nothing changes for anyone
who does not opt in.

When the peer *is* trusted, the client is the right-most address in the
chain that is not itself a trusted proxy. Walking from the right is what
makes this safe: an attacker can prepend as many fake hops as they like on
the left, but they cannot remove the real ones the proxies appended.

``via_proxy`` reports whether the request actually arrived through a
trusted proxy, which is what the loopback gate needs: a request that was
forwarded is not local, however loopback its peer looks.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Iterable, NamedTuple

__all__ = ["ClientAddress", "resolve_client", "client_ip", "trusted_proxy_networks"]

_ENV = "REVOLUZEN_FLOW_TRUSTED_PROXIES"
_LOOPBACK = frozenset({"127.0.0.1", "::1"})


class ClientAddress(NamedTuple):
    """The caller's address and how much we know about the path it took."""

    ip: str
    #: True when the immediate peer was a trusted proxy, so `ip` came from
    #: a forwarded header rather than from the socket.
    via_proxy: bool


def _parse_networks(raw: str) -> tuple[ipaddress._BaseNetwork, ...]:
    nets: list[ipaddress._BaseNetwork] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            # strict=False so "10.0.0.5/8" is accepted as the /8 it means.
            nets.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            # A typo in an operator's env var must not take the server
            # down, and must not silently widen trust either -- skipping
            # the entry is the fail-closed choice.
            continue
    return tuple(nets)


def trusted_proxy_networks(raw: str | None = None) -> tuple[ipaddress._BaseNetwork, ...]:
    """Parse the trusted-proxy allowlist. Read from the env when not given.

    Deliberately read per call rather than cached at import: the test
    suite flips this between cases, and the cost is parsing a short string.
    """
    if raw is None:
        raw = os.environ.get(_ENV, "")
    return _parse_networks(raw)


def _is_trusted(addr: str, nets: Iterable[ipaddress._BaseNetwork]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def _split_forwarded(value: str) -> list[str]:
    """Split an X-Forwarded-For value into bare addresses, left to right."""
    out: list[str] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        # Strip a port from "1.2.3.4:5678", but leave bare IPv6 alone --
        # "::1" is all colons and has no port.
        if part.startswith("["):
            close = part.find("]")
            if close > 1:
                part = part[1:close]
        elif part.count(":") == 1:
            part = part.split(":", 1)[0]
        out.append(part)
    return out


def resolve_client(request, *, trusted: Iterable[ipaddress._BaseNetwork] | None = None) -> ClientAddress:
    """Return the caller's :class:`ClientAddress` for ``request``.

    ``request`` is anything with ``.client`` and ``.headers`` -- a Starlette
    Request in production, a stub in tests.
    """
    client = getattr(request, "client", None)
    peer = getattr(client, "host", "") or ""
    nets = tuple(trusted) if trusted is not None else trusted_proxy_networks()

    if not peer:
        return ClientAddress("", False)
    if not nets or not _is_trusted(peer, nets):
        # No proxy trust configured, or this peer is not one of them.
        # The socket address is the only thing we can believe.
        return ClientAddress(peer, False)

    try:
        forwarded = request.headers.get("X-Forwarded-For", "")
    except Exception:
        forwarded = ""
    if not forwarded:
        # A trusted proxy that sent no XFF -- treat the peer as the client
        # rather than inventing one.
        return ClientAddress(peer, False)

    hops = _split_forwarded(forwarded)
    # Walk right to left past every hop we ourselves trust. What remains is
    # the first address we did not place there. Anything further left was
    # supplied by the caller and is not evidence of anything.
    for hop in reversed(hops):
        if not _is_trusted(hop, nets):
            return ClientAddress(hop, True)
    # Every hop was a trusted proxy (a proxy chain talking to itself).
    return ClientAddress(hops[0] if hops else peer, True)


def client_ip(request) -> str:
    """Just the address — the common case."""
    return resolve_client(request).ip


def is_loopback_client(request) -> bool:
    """True iff the caller is genuinely on this machine.

    A request relayed by a trusted proxy is never loopback, whatever the
    socket says — that is the whole point of the ``via_proxy`` flag. This
    is what keeps a same-host nginx from turning a loopback-only endpoint
    into a public one.
    """
    addr = resolve_client(request)
    if addr.via_proxy:
        return False
    return addr.ip in _LOOPBACK
