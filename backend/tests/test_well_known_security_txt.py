"""`/.well-known/security.txt` — RFC 9116 disclosure pointer.

Two things can break this without anyone noticing, because nothing in the
product reads it — only scanners and researchers do:

1. The route disappears, or stops being registered ahead of the frontend
   catch-all mount at "/", and the SPA swallows the path. A reporter then
   gets an HTML page where they expected a contact address.
2. The URLs drift. They are built from ``app_meta`` precisely so a repo move
   re-points them; if someone hardcodes them back, the next rename leaves a
   researcher mailing a dead address.

These assert the body a researcher actually receives, and that the path is
still registered on the app.
"""
from __future__ import annotations

import asyncio

import pytest

from revoluzen_flow import app_meta


def _body() -> str:
    from revoluzen_flow.main import security_txt

    return asyncio.run(security_txt()).body.decode("utf-8")


def test_route_is_registered_on_the_app():
    """Guards against the SPA catch-all swallowing the path."""
    from revoluzen_flow.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/.well-known/security.txt" in paths


def test_required_rfc9116_fields_present():
    body = _body()
    # Contact and Expires are the two REQUIRED fields in RFC 9116.
    assert "Contact: " in body
    assert "Expires: " in body


def test_urls_track_app_meta_so_a_repo_move_repoints_them():
    body = _body()
    repo = app_meta.repo_url()
    assert f"Contact: {repo}/security/advisories/new" in body
    assert f"Policy: {repo}/blob/main/SECURITY.md" in body


def test_expires_is_in_the_future_and_zulu():
    from datetime import datetime, timezone

    line = next(ln for ln in _body().splitlines() if ln.startswith("Expires: "))
    value = line.split("Expires: ", 1)[1]
    # RFC 9116 wants an ISO-8601 instant; scanners reject a past date.
    assert value.endswith("Z")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed > datetime.now(timezone.utc)


def test_served_as_plain_text_not_html():
    """An HTML response means the SPA answered instead of this handler."""
    from revoluzen_flow.main import security_txt

    resp = asyncio.run(security_txt())
    assert resp.media_type.startswith("text/plain")
