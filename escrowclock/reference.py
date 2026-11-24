"""In-process serial reference runtime with persisted idempotency records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import model
from .errors import EscrowError
from .protocol import (
    error_envelope,
    replay_envelope,
    request_hash,
    success_envelope,
)


class SerialReference:
    """A one-at-a-time reference implementation.

    Requests are processed in the exact order in which the test harness calls
    :meth:`dispatch`.  Failed domain requests are nevertheless retained in
    :attr:`records`, mirroring SQLite's ``request_records`` table.
    """

    def __init__(self) -> None:
        self.state = model.initial_state()
        self.records: dict[str, dict[str, Any]] = {}

    def dispatch(self, command: dict[str, Any]) -> dict[str, Any]:
        request_id = command.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id required")
        digest = request_hash(command)

        existing = self.records.get(request_id)
        if existing is not None:
            if existing["request_hash"] != digest:
                err = {
                    "code": "IDEMPOTENCY_KEY_CONFLICT",
                    "message": "request_id was already used with different arguments",
                    "details": {"request_id": request_id},
                    "retryable": False,
                }
                return error_envelope(err, request_id)
            return replay_envelope(existing["response"])

        try:
            next_state, event = model.apply_command(self.state, command)
            receipt = model.build_receipt(command, event, next_state)
            response = success_envelope(receipt)
        except EscrowError as exc:
            response = error_envelope(exc, request_id)
            self.records[request_id] = {
                "request_id": request_id,
                "request_hash": digest,
                "command": command["command"],
                "status": "rejected",
                "error_code": exc.code,
                "event_seq": None,
                "response": response,
            }
            return deepcopy(response)

        self.state = next_state
        self.records[request_id] = {
            "request_id": request_id,
            "request_hash": digest,
            "command": command["command"],
            "status": "applied",
            "error_code": None,
            "event_seq": event["seq"],
            "response": response,
        }
        return deepcopy(response)

    def state_hash(self) -> str:
        return model.state_hash(self.state)
