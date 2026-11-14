"""Secrets typed straight into a pipeline step's own settings.

A step can hold its own password, key or signed URL instead of linking a saved
connection: Send Email's ``smtp_pass``, a Slack ``webhook_url``, an
``Authorization`` header. Those settings are part of the pipeline definition,
which is copied into every version, the Bin and every run record. Until
November 2026 each copy held the secret as typed, and every API response that
returned a pipeline sent it back to the browser.

How they are kept now:

* **Stored encrypted.** ``WorkflowStore.save`` is the one place a pipeline is
  written, and it encrypts them (``ENC:v1:``). The pipeline object stays
  encrypted from then on, so run records are encrypted without any of their
  writers having to know.
* **Read only when a step runs.** ``BaseNode.__init__`` decrypts a private copy
  for the node. The only other reader is the pipeline fingerprint, which stays
  over the real value so that versions saved before encryption still verify.
* **Never sent back.** API responses show ``SECRET_PLACEHOLDER``
  (``security/step_secret_routes.py``). The editor sends that back unless the
  person typed something new, and ``fill_stored_secrets`` restores the stored
  value.
* **Left out of templates**, which exist to be shared.

Which settings count: the explicit list below. The wider rule used for saved
credentials treats ``key`` and every ``*_key`` as secret, which in a step would
catch ``merge_key`` and ``join_key``. That wider rule is used only inside the
settings that are bags of named credentials, such as ``headers``.

A value holding ``{{ }}`` or ``${ }`` is a reference resolved when the step
runs, so it stays readable. A secret inside free text (SQL, a URL with
``user:pass@``, a request body) cannot be told apart and is not covered.

A pipeline parameter's default value is kept the same way, when the parameter's
name says it holds a secret or the person switched it on (``secret``). It is
the one thing decrypted before the node is built, in ``engine/parameters.py``,
because ``Bearer ${param.api_token}`` needs the text. What a run was given for
such a parameter is encrypted on the run record and shown as the placeholder.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterator

from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER
from revoluzen_flow.credentials.sensitive_fields import name_says_secret
from revoluzen_flow.security.encryptor import _ENC_PREFIX

STEP_SECRET_SETTINGS = frozenset({
    "password", "smtp_pass", "smtp_password", "auth_pass", "auth_token", "auth_value",
    "bearer_token", "api_key", "apikey", "api_key_value", "app_key", "embedding_api_key",
    "token", "secret", "secret_key", "secret_access_key", "aws_secret_access_key",
    "private_key", "private_key_pem", "passphrase", "account_key", "sas_token",
    "connection_string", "hmac_secret", "sasl_password", "webhook_url",
    "credentials_json", "service_account_json", "client_secret", "access_token",
    "refresh_token", "session_token", "personal_access_token", "integration_token",
    "api_token", "consumer_secret", "token_secret", "bot_token", "developer_token",
    "signing_secret",
})

# Settings that hold several named values, some of which are credentials.
_BAG_SETTINGS = frozenset({
    "headers", "storage_options", "credentials", "credentials_json", "service_account_json",
})
_BAG_SECRET_NAMES = frozenset({"authorization", "proxy_authorization", "cookie", "set_cookie"})

# Which pipeline parameters hold a secret by name. A person names these freely
# (`db_password`, `stripeApiKey`), so whole words are matched: `max_tokens` and
# a `merge_key` stay readable. frontend/src/utils/storedSecrets.ts mirrors this
# to lock the switch in the Parameters dialog, and tests/test_step_secrets.py
# holds the two to the same lists.
_PARAMETER_SECRET_WORDS = frozenset({
    "password", "passwd", "pwd", "passphrase", "secret", "token", "authorization", "credentials",
})
_PARAMETER_SECRET_LAST_WORDS = frozenset({"pass", "pw"})
_PARAMETER_SECRET_ENDINGS = (
    "apikey", "apikeyvalue", "privatekey", "privatekeypem", "accesskey", "accountkey", "appkey",
    "authkey", "authvalue", "signingkey", "encryptionkey", "masterkey", "hmackey", "sshkey", "sharedkey",
    "connectionstring", "credentialsjson", "serviceaccountjson", "webhookurl",
)
_PARAMETER_NOT_SECRET_LAST_WORDS = frozenset({
    "url", "uri", "endpoint", "name", "id", "type", "field", "column", "path", "ref", "file", "env",
    "header", "param", "limit", "count", "size", "ttl", "expiry", "budget", "length", "version",
})


def parameter_name_says_secret(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    words = [w for w in re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).lower()) if w]
    if not words:
        return False
    if "".join(words).endswith(_PARAMETER_SECRET_ENDINGS):
        return True
    if words[-1] in _PARAMETER_NOT_SECRET_LAST_WORDS:
        return False
    return words[-1] in _PARAMETER_SECRET_LAST_WORDS or any(w in _PARAMETER_SECRET_WORDS for w in words)


def _parameter_field(parameter: Any, field: str) -> Any:
    return parameter.get(field) if isinstance(parameter, dict) else getattr(parameter, field, None)


def is_secret_parameter(parameter: Any) -> bool:
    """Whether a parameter's default is kept as a secret: switched on, or by name."""
    return bool(_parameter_field(parameter, "secret")) or parameter_name_says_secret(
        _parameter_field(parameter, "name"),
    )


def _setting_key(name: Any) -> str:
    """A setting or header name in one spelling: ``X-Api-Key`` -> ``x_api_key``."""
    return name.strip().lower().replace("-", "_").replace(" ", "_") if isinstance(name, str) else ""


def is_step_secret_setting(name: Any) -> bool:
    return _setting_key(name) in STEP_SECRET_SETTINGS


def _bag_entry_is_secret(name: Any) -> bool:
    key = _setting_key(name)
    return key in _BAG_SECRET_NAMES or name_says_secret(key)


def _bag_slots(bag: Any) -> Iterator[tuple[Any, Any]]:
    if isinstance(bag, dict):
        for name, value in bag.items():
            if isinstance(value, (dict, list)):
                yield from _bag_slots(value)
            elif _bag_entry_is_secret(name):
                yield bag, name
    elif isinstance(bag, list):
        # The HTTP Request form keeps headers as [{key, value}] rows.
        for row in bag:
            if not isinstance(row, dict):
                continue
            label = row.get("key", row.get("name"))
            if "value" in row and isinstance(label, str):
                if _bag_entry_is_secret(label):
                    yield row, "value"
            else:
                yield from _bag_slots(row)


def _secret_slots(params: Any) -> list[tuple[Any, Any]]:
    """Every (holder, key) in a step's settings that holds a secret by name."""
    slots: list[tuple[Any, Any]] = []
    if not isinstance(params, dict):
        return slots
    for name, value in params.items():
        key = _setting_key(name)
        if key in _BAG_SETTINGS and isinstance(value, (dict, list)):
            slots.extend(_bag_slots(value))
        elif key in STEP_SECRET_SETTINGS:
            slots.append((params, name))
    return slots


def _is_ciphertext(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_ENC_PREFIX)


def _is_typed_secret(value: Any) -> bool:
    """Text still as the person typed it: not empty, not encrypted, not the
    placeholder, and not a reference resolved when the step runs."""
    return (
        isinstance(value, str)
        and value.strip() != ""
        and not value.startswith(_ENC_PREFIX)
        and value != SECRET_PLACEHOLDER
        and "{{" not in value
        and "${" not in value
    )


def _holds_ciphertext(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith(_ENC_PREFIX)
    if isinstance(value, dict):
        return any(_holds_ciphertext(item) for item in value.values())
    if isinstance(value, list):
        return any(_holds_ciphertext(item) for item in value)
    return False


def _map_strings(value: Any, change: Callable[[str], Any]) -> Any:
    """A copy of ``value`` with ``change`` applied to every string in it."""
    if isinstance(value, dict):
        return {key: _map_strings(item, change) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_strings(item, change) for item in value]
    return change(value) if isinstance(value, str) else value


def current_encryptor():
    """The always-on encryptor the credential and connection stores use.

    None only when REVOLUZEN_FLOW_DISABLE_ENCRYPTION=1 (test-only) or before
    startup has populated app_state; secrets are then kept as typed, the same
    fallback as those stores.
    """
    try:
        from revoluzen_flow.main import app_state
    except Exception:  # noqa: BLE001 - no app in this process
        return None
    return app_state.get("encryptor")


# ── Encrypting ───────────────────────────────────────────────────────────


def seal_step_params(params: Any, encrypt: Callable[[str], str]) -> int:
    """Encrypt, in place, each typed secret in one step's settings. Returns how
    many were encrypted; a value already encrypted is left exactly as it is."""
    sealed = 0
    for holder, slot in _secret_slots(params):
        if _is_typed_secret(holder[slot]):
            holder[slot] = encrypt(holder[slot])
            sealed += 1
    return sealed


def seal_dumped_steps(steps: Any, encrypt: Callable[[str], str]) -> int:
    """``seal_step_params`` for the step dicts of a stored pipeline."""
    sealed = 0
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict):
            sealed += seal_step_params(step.get("params"), encrypt)
    return sealed


def seal_dumped_workflow(workflow: Any, encrypt: Callable[[str], str]) -> int:
    """Encrypt the typed secrets in a stored pipeline: its steps and the
    defaults of its secret parameters. Returns how many were encrypted."""
    if not isinstance(workflow, dict):
        return 0
    sealed = seal_dumped_steps(workflow.get("steps"), encrypt)
    parameters = workflow.get("parameters")
    for parameter in parameters if isinstance(parameters, list) else []:
        if (
            isinstance(parameter, dict)
            and is_secret_parameter(parameter)
            and _is_typed_secret(parameter.get("default"))
        ):
            parameter["default"] = encrypt(parameter["default"])
            sealed += 1
    return sealed


def seal_workflow(workflow) -> None:
    """Encrypt, in place, every typed secret in a pipeline's steps and the
    defaults of its secret parameters."""
    waiting = [
        step for step in workflow.steps
        if any(_is_typed_secret(holder[slot]) for holder, slot in _secret_slots(step.params))
    ]
    parameters = [
        p for p in workflow.parameters or [] if is_secret_parameter(p) and _is_typed_secret(p.default)
    ]
    if not waiting and not parameters:
        return
    encryptor = current_encryptor()
    if encryptor is None:
        return
    for step in waiting:
        seal_step_params(step.params, encryptor.encrypt_value)
    for parameter in parameters:
        parameter.default = encryptor.encrypt_value(parameter.default)


# ── Putting a kept secret back ───────────────────────────────────────────


def _find_placeholders(value: Any, path: tuple, found: list) -> None:
    """Collect (holder, key, path) for every placeholder under ``value``. A
    {key, value} row is found again by its key, because rows can be reordered."""
    if isinstance(value, dict):
        for key, item in value.items():
            here = path + (key,)
            if item == SECRET_PLACEHOLDER:
                found.append((value, key, here))
            else:
                _find_placeholders(item, here, found)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if (
                isinstance(item, dict)
                and item.get("value") == SECRET_PLACEHOLDER
                and isinstance(item.get("key"), str)
            ):
                found.append((item, "value", path + (("row", item["key"]),)))
            elif item == SECRET_PLACEHOLDER:
                found.append((value, index, path + (index,)))
            else:
                _find_placeholders(item, path + (index,), found)


def _read_path(params: Any, path: tuple) -> Any:
    node = params
    for part in path:
        if isinstance(part, tuple):
            if not isinstance(node, list):
                return None
            node = next(
                (row.get("value") for row in node if isinstance(row, dict) and row.get("key") == part[1]),
                None,
            )
        elif isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and isinstance(part, int) and part < len(node):
            node = node[part]
        else:
            return None
    return node


def fill_stored_secrets(workflow, store, workspace_id: str | None) -> None:
    """Replace each placeholder in ``workflow`` with the stored value.

    Looks for the same step and setting in the newest version that has it, then
    older ones, so restoring a version whose step was later deleted still
    works. Only versions in ``workspace_id`` are read. A placeholder with
    nothing stored behind it is dropped, so it can never be saved as the secret
    or used as a password: the person sees an empty setting to fill in.

    The same dots typed into an ordinary setting, say as the replacement value
    in a masking step, are just text. They are only swapped for a value stored
    encrypted, and never dropped.

    A parameter's default follows the same rules, found by the parameter's name.
    """
    waiting_parameters = {
        p.name: p for p in workflow.parameters or [] if p.default == SECRET_PLACEHOLDER
    }
    waiting: dict[str, list] = {}
    for step in workflow.steps:
        found: list = []
        _find_placeholders(step.params, (), found)
        if found:
            by_name = {(id(holder), slot) for holder, slot in _secret_slots(step.params)}
            waiting[step.id] = [
                (holder, key, path, (id(holder), key) in by_name) for holder, key, path in found
            ]
    if not waiting and not waiting_parameters:
        return

    def _keeps(value: Any, secret_by_name: bool) -> bool:
        if _is_ciphertext(value):
            return True
        return secret_by_name and isinstance(value, str) and value != "" and value != SECRET_PLACEHOLDER

    for version in store.stored_versions_newest_first(workflow.id, workspace_id or "default"):
        for name in list(waiting_parameters):
            parameter = waiting_parameters[name]
            value = version["parameters"].get(name)
            if _keeps(value, is_secret_parameter(parameter)):
                parameter.default = value
                del waiting_parameters[name]
        for step_id in list(waiting):
            stored = version["steps"].get(step_id)
            if stored is None:
                continue
            left = []
            for holder, key, path, secret_by_name in waiting[step_id]:
                value = _read_path(stored, path)
                if _keeps(value, secret_by_name):
                    holder[key] = value
                else:
                    left.append((holder, key, path, secret_by_name))
            if left:
                waiting[step_id] = left
            else:
                del waiting[step_id]
        if not waiting and not waiting_parameters:
            return
    for parameter in waiting_parameters.values():
        if is_secret_parameter(parameter):
            parameter.default = None
    for found in waiting.values():
        for holder, key, path, secret_by_name in found:
            if not secret_by_name:
                continue
            if isinstance(holder, dict) and not isinstance(path[-1], tuple):
                del holder[key]
            else:
                holder[key] = ""


def prepare_incoming_workflow(workflow, get_store: Callable[[], Any], workspace_id: str | None) -> None:
    """For a pipeline the editor sent to run or probe without saving: restore
    kept secrets, and encrypt new ones so the run record never holds them.
    ``get_store`` is only called when there is a kept secret to look up."""
    found: list = []
    for step in workflow.steps:
        _find_placeholders(step.params, (), found)
    if found or any(p.default == SECRET_PLACEHOLDER for p in workflow.parameters or []):
        fill_stored_secrets(workflow, get_store(), workspace_id)
    seal_workflow(workflow)


# ── Reading ──────────────────────────────────────────────────────────────


def open_step_params(params: Any) -> Any:
    """A decrypted copy of a step's settings for the node about to run, or the
    same object when there is nothing to change.

    A placeholder still in a secret setting (a route that does not restore
    kept secrets, such as a Copilot draft) becomes empty, so the dots can
    never be sent to another system as if they were the password.
    """
    encrypted = _holds_ciphertext(params)
    if not encrypted and not any(h[s] == SECRET_PLACEHOLDER for h, s in _secret_slots(params)):
        return params
    encryptor = current_encryptor() if encrypted else None
    if encrypted and encryptor is None:
        raise ValueError(
            "This step has a stored password or key, but encryption is switched "
            "off on this server, so it cannot be read. Enter it again and save."
        )

    def _open(value: str) -> str:
        if not _is_ciphertext(value):
            return value
        try:
            return encryptor.decrypt_value(value)
        except Exception:  # noqa: BLE001 - never put the value in the message
            raise ValueError(
                "A password or key stored in this step cannot be read on this "
                "server, because it was saved with a different master key. Open "
                "the step, enter it again and save."
            ) from None

    opened = _map_strings(params, _open)
    for holder, slot in _secret_slots(opened):
        if holder[slot] == SECRET_PLACEHOLDER:
            holder[slot] = ""
    return opened


def open_parameter_value(value: Any) -> Any:
    """The readable form of a parameter's default, or of a per-run value being
    replayed. Called only as a run's parameters are resolved."""
    if not _is_ciphertext(value):
        return value
    encryptor = current_encryptor()
    if encryptor is None:
        raise ValueError(
            "its stored value cannot be read because encryption is switched off "
            "on this server. Enter it again and save."
        )
    try:
        return encryptor.decrypt_value(value)
    except Exception:  # noqa: BLE001 - never put the value in the message
        raise ValueError(
            "its stored value cannot be read on this server, because it was saved "
            "with a different master key. Open Parameters, enter it again and save."
        ) from None


def open_parameters_for_hash(parameters: Any) -> None:
    """Prepare a pipeline's dumped parameters for its fingerprint: each encrypted
    default becomes the value itself, and the ``secret`` switch is left out
    while it is off, so pipelines saved before the switch existed still match."""
    if not isinstance(parameters, list):
        return
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        if not parameter.get("secret"):
            parameter.pop("secret", None)
        default = parameter.get("default")
        if _is_ciphertext(default):
            encryptor = current_encryptor()
            if encryptor is None:
                continue
            try:
                parameter["default"] = encryptor.decrypt_value(default)
            except Exception:  # noqa: BLE001
                continue


def open_steps_for_hash(steps: Any) -> None:
    """Swap, in the dumped steps of a pipeline, each encrypted value for the
    value itself, so a fingerprint does not depend on how a secret is stored.
    A value this server cannot read stays as it is, and the check it feeds
    reports a mismatch."""
    if not isinstance(steps, list) or not _holds_ciphertext(steps):
        return
    encryptor = current_encryptor()
    if encryptor is None:
        return

    def _open(value: str) -> str:
        if not _is_ciphertext(value):
            return value
        try:
            return encryptor.decrypt_value(value)
        except Exception:  # noqa: BLE001
            return value

    for step in steps:
        if isinstance(step, dict) and isinstance(step.get("params"), dict):
            step["params"] = _map_strings(step["params"], _open)


# ── Showing ──────────────────────────────────────────────────────────────


def mask_step_params(params: Any) -> Any:
    """A copy of a step's settings fit to show: anything encrypted, and any
    secret still as typed, replaced by the placeholder."""
    if not isinstance(params, dict):
        return params
    masked = _map_strings(params, lambda v: SECRET_PLACEHOLDER if _is_ciphertext(v) else v)
    for holder, slot in _secret_slots(masked):
        if _is_typed_secret(holder[slot]):
            holder[slot] = SECRET_PLACEHOLDER
    return masked


def mask_steps(steps: Any) -> Any:
    """``mask_step_params`` for a list of step dicts."""
    if not isinstance(steps, list):
        return steps
    return [
        {**step, "params": mask_step_params(step["params"])}
        if isinstance(step, dict) and isinstance(step.get("params"), dict) else step
        for step in steps
    ]


def mask_parameters(parameters: Any) -> Any:
    """Copies of a pipeline's dumped parameters fit to show: an encrypted
    default, or a secret default still as typed, replaced by the placeholder."""
    if not isinstance(parameters, list):
        return parameters
    out: list = []
    for parameter in parameters:
        if isinstance(parameter, dict):
            default = parameter.get("default")
            if _is_ciphertext(default) or (is_secret_parameter(parameter) and _is_typed_secret(default)):
                parameter = {**parameter, "default": SECRET_PLACEHOLDER}
        out.append(parameter)
    return out


def _secret_value_names(values: dict, parameters: Any) -> set:
    declared = {_parameter_field(p, "name") for p in parameters or [] if is_secret_parameter(p)}
    return {name for name in values if name in declared or parameter_name_says_secret(name)}


def mask_parameter_values(values: Any, parameters: Any) -> Any:
    """Per-run parameter values fit to show or send: anything encrypted, and any
    secret parameter's value still as typed, replaced by the placeholder."""
    if not isinstance(values, dict):
        return values
    secret = _secret_value_names(values, parameters)
    return {
        name: SECRET_PLACEHOLDER if _is_ciphertext(value) or (name in secret and _is_typed_secret(value)) else value
        for name, value in values.items()
    }


def seal_parameter_values(values: Any, parameters: Any, encrypt: Callable[[str], str] | None = None) -> Any:
    """Per-run parameter values fit to keep with a run: each secret parameter's
    value encrypted, so a replay can still use it. Returns ``values`` itself
    when there is nothing to encrypt."""
    if not isinstance(values, dict) or not values:
        return values
    typed = [name for name in _secret_value_names(values, parameters) if _is_typed_secret(values[name])]
    if not typed:
        return values
    if encrypt is None:
        encryptor = current_encryptor()
        if encryptor is None:
            return values
        encrypt = encryptor.encrypt_value
    return {**values, **{name: encrypt(values[name]) for name in typed}}


def keep_stored_parameter_values(incoming: Any, stored: Any, parameters: Any) -> Any:
    """Parameter values being saved over earlier ones, as on a schedule.

    The dots stand for the value already stored. With nothing stored behind
    them they are dropped, so the pipeline's own default applies. Dots given
    for an ordinary parameter are just text.
    """
    if not isinstance(incoming, dict):
        return incoming
    before = stored if isinstance(stored, dict) else {}
    secret = _secret_value_names(incoming, parameters)
    kept: dict = {}
    for name, value in incoming.items():
        if value == SECRET_PLACEHOLDER and (name in secret or _is_ciphertext(before.get(name))):
            if before.get(name) not in (None, "", SECRET_PLACEHOLDER):
                kept[name] = before[name]
            continue
        kept[name] = value
    return kept


def _blanked_params(params: dict) -> tuple[dict, int]:
    blanked = 0

    def _empty_stored(value: str) -> str:
        nonlocal blanked
        if _is_ciphertext(value) or value == SECRET_PLACEHOLDER:
            blanked += 1
            return ""
        return value

    copy = _map_strings(params, _empty_stored)
    for holder, slot in _secret_slots(copy):
        if _is_typed_secret(holder[slot]):
            holder[slot] = ""
            blanked += 1
    return copy, blanked


def blank_step_secrets(steps: Any) -> tuple[Any, int]:
    """Copies of step dicts with every secret emptied, and how many there were.
    A template is for sharing, so it never carries one."""
    if not isinstance(steps, list):
        return steps, 0
    out: list = []
    total = 0
    for step in steps:
        if isinstance(step, dict) and isinstance(step.get("params"), dict):
            params, blanked = _blanked_params(step["params"])
            total += blanked
            step = {**step, "params": params}
        out.append(step)
    return out, total
