"""Typed domain and infrastructure errors."""

from __future__ import annotations

from typing import Any


class EscrowError(Exception):
    code = "ESCROW_ERROR"
    exit_code = 2
    retryable = False

    def __init__(self, message: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


class InvalidCommand(EscrowError):
    code = "INVALID_COMMAND"


class NotFound(EscrowError):
    code = "NOT_FOUND"


class IdempotencyConflict(EscrowError):
    code = "IDEMPOTENCY_KEY_CONFLICT"
    exit_code = 2


class MissingPredecessor(EscrowError):
    code = "MISSING_PREDECESSOR"


class NaturalPersonOverlap(EscrowError):
    code = "NATURAL_PERSON_OVERLAP"


class PostWindowInactive(EscrowError):
    code = "POST_WINDOW_INACTIVE"


class AgentNotPostHolder(EscrowError):
    code = "AGENT_NOT_POST_HOLDER"


class AuthorizationStale(EscrowError):
    code = "AUTHORIZATION_STALE"


class QuotaExhausted(EscrowError):
    code = "QUOTA_EXHAUSTED"


class ObjectNotAuthorized(EscrowError):
    code = "OBJECT_NOT_AUTHORIZED"


class AlreadyTerminal(EscrowError):
    code = "AUTHORIZATION_ALREADY_TERMINAL"


class ReclamationIrreversible(EscrowError):
    code = "RECLAMATION_IRREVERSIBLE"


class EventTimeOrder(EscrowError):
    code = "EVENT_TIME_OUT_OF_ORDER"


class FixtureError(EscrowError):
    code = "FIXTURE_ERROR"


class ReplayIntegrityError(EscrowError):
    code = "REPLAY_INTEGRITY_ERROR"
    exit_code = 70


class DeterministicCrash(Exception):
    """Raised in tests to simulate a process crash at a known point."""

    def __init__(self, point: str):
        super().__init__(f"deterministic crash injected at {point}")
        self.point = point
        self.exit_code = 86
