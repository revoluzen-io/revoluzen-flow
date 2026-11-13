"""Keeps secrets typed into pipeline steps out of API responses.

A stored pipeline holds those secrets encrypted (``ir/step_secrets.py``), and
many routes return a pipeline or part of one. Rather than rely on each of them
to remember, these route classes replace every encrypted value in the finished
response body with the placeholder the editor knows how to send back.

Only encrypted values are touched, so a value a request has just sent, such as
an unsaved draft, is never lost. Set with ``APIRouter(route_class=...)``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from fastapi import Request, Response
from fastapi.routing import APIRoute

from revoluzen_flow.connections.inline_secrets import SECRET_PLACEHOLDER
from revoluzen_flow.ir.step_secrets import mask_parameter_values, mask_parameters, mask_steps
from revoluzen_flow.security.encryptor import _ENC_PREFIX

_PREFIX = _ENC_PREFIX.encode("ascii")
_TOKEN = re.compile(re.escape(_PREFIX) + rb"[A-Za-z0-9_\-=]*")
_MASK = SECRET_PLACEHOLDER.encode("utf-8")
_SNAPSHOT_KEY = b'"workflow_snapshot"'


class MaskStepSecretsRoute(APIRoute):
    """Replaces every encrypted value in a response body with the placeholder."""

    @staticmethod
    def clean_body(body: bytes) -> bytes:
        return _TOKEN.sub(_MASK, body) if _PREFIX in body else body

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()
        clean = self.clean_body

        async def masked(request: Request) -> Response:
            response = await handler(request)
            body = getattr(response, "body", None)
            if isinstance(body, (bytes, bytearray)) and body:
                cleaned = clean(bytes(body))
                if cleaned != body:
                    response.body = cleaned
                    response.headers["content-length"] = str(len(cleaned))
            return response

        return masked


def _mask_run_record(record: dict) -> None:
    snapshot = record.get("workflow_snapshot")
    parameters = None
    if isinstance(snapshot, dict):
        parameters = snapshot.get("parameters")
        snapshot["steps"] = mask_steps(snapshot.get("steps"))
        snapshot["parameters"] = mask_parameters(parameters)
    metadata = record.get("metadata")
    if isinstance(metadata, dict) and "parameter_values" in metadata:
        metadata["parameter_values"] = mask_parameter_values(
            metadata["parameter_values"], parameters if isinstance(parameters, list) else [],
        )


def _mask_snapshots(node: Any) -> None:
    if isinstance(node, dict):
        if "workflow_snapshot" in node:
            _mask_run_record(node)
        for key, value in node.items():
            if key not in ("workflow_snapshot", "metadata"):
                _mask_snapshots(value)
    elif isinstance(node, list):
        for item in node:
            _mask_snapshots(item)


class MaskRunSnapshotsRoute(MaskStepSecretsRoute):
    """Also masks secrets by name in run records.

    The startup pass encrypts old run records a batch at a time, so one can
    still hold a secret as typed for a while after an upgrade.
    """

    @staticmethod
    def clean_body(body: bytes) -> bytes:
        if _SNAPSHOT_KEY in body:
            try:
                payload = json.loads(body)
            except ValueError:
                pass
            else:
                _mask_snapshots(payload)
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return MaskStepSecretsRoute.clean_body(body)
