"""Request hashing and stable JSON response envelopes."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .canonical import canonical_bytes, sha256_hex
from .errors import EscrowError

RECEIPT_VERSION = 1


def request_hash(command: dict[str, Any]) -> str:
    """Hash a command without its idempotency key.

    The crash directive is execution-only and is removed by the CLI before a
    command reaches this function.
    """

    body = {k: v for k, v in command.items() if k != "request_id"}
    return sha256_hex(canonical_bytes(body))


def success_envelope(receipt: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "replayed": False, "receipt": receipt}


def error_envelope(
    error: EscrowError | dict[str, Any], request_id: str | None = None
) -> dict[str, Any]:
    if isinstance(error, EscrowError):
        err = error.to_dict()
    else:
        err = deepcopy(error)
    if request_id is not None:
        err.setdefault("request_id", request_id)
    return {"ok": False, "replayed": False, "error": err}


def replay_envelope(stored: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(stored)
    result["replayed"] = True
    return result
