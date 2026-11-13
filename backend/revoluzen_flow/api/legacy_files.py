"""Helpers the File step's settings use for mainframe files and PGP.

* ``POST /api/legacy-files/copybook-layout``: read a pasted copybook and
  return its fields (position, length, type, column name), or the line that
  could not be read. Only pasted text is read here, never a server path.
* ``POST /api/legacy-files/pgp-key-check``: describe the key in a saved PGP
  credential (fingerprint, names, which key encrypts or signs) and, for
  decrypting or signing, whether the stored passphrase unlocks it. Purposes:
  ``decrypt``, ``encrypt``, ``verify`` (the sender's public key) and ``sign``
  (your private key). No key material or passphrase is ever returned.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from revoluzen_flow.auth.deps import current_workspace_id, require_auth

router = APIRouter(prefix="/api/legacy-files", tags=["legacy-files"])

_read = Depends(require_auth)
_MAX_COPYBOOK = 512 * 1024


def _workspace(request: Request) -> str:
    try:
        return current_workspace_id(request)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "workspace resolve failed") from exc


@router.post("/copybook-layout")
async def copybook_layout(body: dict[str, Any], _user=_read) -> dict[str, Any]:
    from revoluzen_flow.legacy_files.copybook import CopybookError, Options, compile_copybook

    text = str(body.get("copybook") or "")
    if len(text) > _MAX_COPYBOOK:
        raise HTTPException(413, "The copybook is larger than 512 KB.")
    options = Options(
        code_page=str(body.get("code_page") or "cp037"),
        underscores=str(body.get("column_names") or "underscores").lower() != "as_written",
    )
    rules = body.get("redefines_rules") or []
    try:
        layout = compile_copybook(text, options, rules if isinstance(rules, list) else [])
    except (CopybookError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **layout.describe()}


@router.post("/pgp-key-check")
async def pgp_key_check(
    body: dict[str, Any],
    _user=_read,
    workspace_id: str = Depends(_workspace),
) -> dict[str, Any]:
    from revoluzen_flow.main import app_state
    from revoluzen_flow.openpgp import PgpError, Signer, summary
    from revoluzen_flow.openpgp.credentials import (
        decryption_keys, encryption_keys, load_credential, signing_keys, verification_keys,
    )
    from revoluzen_flow.openpgp.keys import secret_key_usable, unlock
    from revoluzen_flow.openpgp.signatures import signing_key

    purpose = str(body.get("purpose") or "decrypt").lower()
    credential_id = str(body.get("credential_id") or "")
    try:
        config = load_credential(app_state, credential_id, workspace_id)
        if purpose == "encrypt":
            keys = encryption_keys(config)
            for key in keys:
                key.encryption_key()  # raises when nothing can encrypt to it
            return {"ok": True, "purpose": "encrypt", "keys": summary(keys)}
        if purpose == "verify":
            keys = verification_keys(config)
            problems = []
            for key in keys:
                try:
                    signing_key(key)
                except PgpError as exc:
                    problems.append(str(exc))
            if len(problems) == len(keys):
                raise PgpError(problems[0])
            return {"ok": True, "purpose": "verify", "keys": summary(keys)}
        if purpose == "sign":
            keys, passphrase = signing_keys(config)
            Signer(keys, passphrase)  # finds the signing key and unlocks it
            return {"ok": True, "purpose": "sign", "keys": summary(keys), "unlocks": True}
        keys, passphrase = decryption_keys(config)
        unlocked = 0
        for key in keys:
            for packet in key.all_packets:
                if packet.secret_body is not None and packet.can_encrypt and secret_key_usable(packet):
                    unlock(packet, passphrase)
                    unlocked += 1
        if not unlocked:
            raise PgpError("The private key has no part that can decrypt files (no encryption subkey with its secret).")
        return {"ok": True, "purpose": "decrypt", "keys": summary(keys), "unlocks": True}
    except PgpError as exc:
        return {"ok": False, "error": str(exc)}
