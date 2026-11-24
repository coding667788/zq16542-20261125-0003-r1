"""Pure, small-state serial reference for escrowclock.

This module has no SQLite, threading, clock, or randomness dependencies.  Every
successful command produces one canonical event and the next state.  SQLite is
responsible for durable compare-and-commit/idempotency; tests compare its
replayed state with :class:`SerialReference`.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .canonical import GENESIS_HASH, sha256_hex
from .errors import (
    AuthorizationStale,
    EventTimeOrder,
    InvalidCommand,
    MissingPredecessor,
    NaturalPersonOverlap,
    ReclamationIrreversible,
)

SCHEMA_VERSION = 1


def initial_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "head_seq": 0,
        "head_hash": GENESIS_HASH,
        "persons": {},
        "agents": {},
        "posts": {},
        "policies": [],
        "applications": {},
        "tickets": {},
        "approvals": {},
        "claims": {},
        "claim_order": [],
        "cancellations": {},
        "reclamations": {},
    }


def state_hash(state: dict[str, Any]) -> str:
    return sha256_hex(state)


# ---------------------------------------------------------------------------
# Event construction and folding
# ---------------------------------------------------------------------------


def build_event(
    state: dict[str, Any],
    event_type: str,
    event_time: int | None,
    request_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    seq = int(state["head_seq"]) + 1
    unsigned = {
        "seq": seq,
        "event_id": f"evt-{seq:08d}",
        "event_type": event_type,
        "event_time": event_time,
        "request_id": request_id,
        "payload": payload,
        "prev_hash": state["head_hash"],
    }
    event_hash = sha256_hex(unsigned)
    return {**unsigned, "event_hash": event_hash}


def reduce_event(
    state: dict[str, Any], event: dict[str, Any], verify: bool = True
) -> dict[str, Any]:
    if verify:
        expected_seq = int(state["head_seq"]) + 1
        if int(event["seq"]) != expected_seq:
            raise ValueError(f"event seq mismatch: expected {expected_seq}, got {event['seq']}")
        if event["prev_hash"] != state["head_hash"]:
            raise ValueError(f"prev_hash mismatch at seq {event['seq']}")
        unsigned = {k: v for k, v in event.items() if k != "event_hash"}
        if sha256_hex(unsigned) != event["event_hash"]:
            raise ValueError(f"event hash mismatch at seq {event['seq']}")

    nxt = deepcopy(state)
    nxt["head_seq"] = int(event["seq"])
    nxt["head_hash"] = event["event_hash"]
    _reduce_payload(nxt, event)
    return nxt


def _reduce_payload(state: dict[str, Any], event: dict[str, Any]) -> None:
    et = event["event_type"]
    p = event["payload"]
    seq = int(event["seq"])

    if et == "NaturalPersonRegistered":
        state["persons"][p["person_id"]] = {
            "person_id": p["person_id"],
            "label": p.get("label", p["person_id"]),
            "agent_ids": [],
        }

    elif et == "AgentRegistered":
        state["agents"][p["agent_id"]] = {
            "agent_id": p["agent_id"],
            "person_id": p["person_id"],
            "label": p.get("label", p["agent_id"]),
        }
        person = state["persons"].get(p["person_id"])
        if person is not None and p["agent_id"] not in person["agent_ids"]:
            person["agent_ids"].append(p["agent_id"])

    elif et == "PostWindowRegistered":
        post = state["posts"].setdefault(
            p["post_id"], {"post_id": p["post_id"], "windows": []}
        )
        post["windows"].append(
            {
                "agent_id": p["agent_id"],
                "valid_from": p["valid_from"],
                "valid_to": p["valid_to"],
            }
        )

    elif et == "PolicyApproved":
        state["policies"].append(
            {
                "policy_id": p["policy_id"],
                "title": p.get("title", p["policy_id"]),
                "rule": p.get("rule", {"effect": "none"}),
                "approved_seq": seq,
                "approved_at": event["event_time"],
            }
        )

    elif et == "ApplicationCreated":
        state["applications"][p["application_id"]] = {
            "application_id": p["application_id"],
            "owner_person_id": p["owner_person_id"],
            "ticket_id": p["ticket_id"],
            "approval_id": p["approval_id"],
            "required_posts": list(p["required_posts"]),
            "release_post_id": p["release_post_id"],
            "object_ids": list(p["object_ids"]),
            "max_claims": p["max_claims"],
            "valid_from": p["valid_from"],
            "valid_to": p["valid_to"],
            "status": "awaiting_signatures",
            "created_seq": seq,
            "created_at": event["event_time"],
            "signatures": [],
        }
        state["tickets"][p["ticket_id"]] = {
            "ticket_id": p["ticket_id"],
            "application_id": p["application_id"],
            "approval_id": p["approval_id"],
            "status": "signing",
        }
        state["approvals"][p["approval_id"]] = {
            "approval_id": p["approval_id"],
            "application_id": p["application_id"],
            "ticket_id": p["ticket_id"],
            "release_post_id": p["release_post_id"],
            "object_ids": list(p["object_ids"]),
            "max_claims": p["max_claims"],
            "used_claims": 0,
            "valid_from": p["valid_from"],
            "valid_to": p["valid_to"],
            "status": "pending",
            "issued_seq": None,
            "issued_at": None,
        }

    elif et == "TicketIssuanceRecorded":
        app = state["applications"][p["application_id"]]
        app["signatures"].append(
            {
                "stage": p["stage_index"],
                "post_id": p["post_id"],
                "agent_id": p["agent_id"],
                "person_id": p["person_id"],
                "at": event["event_time"],
                "seq": seq,
                "event_id": event["event_id"],
                "event_hash": event["event_hash"],
            }
        )
        if p.get("issued"):
            app["status"] = "issued"
            ticket = state["tickets"][app["ticket_id"]]
            approval = state["approvals"][app["approval_id"]]
            ticket["status"] = "issued"
            approval["status"] = "active"
            approval["issued_seq"] = seq
            approval["issued_at"] = event["event_time"]

    elif et == "ClaimTaken":
        approval = state["approvals"][p["approval_id"]]
        approval["used_claims"] = int(p["claim_number"])
        claim = {
            "claim_id": p["claim_id"],
            "approval_id": p["approval_id"],
            "application_id": p["application_id"],
            "ticket_id": p["ticket_id"],
            "object_id": p["object_id"],
            "post_id": p["post_id"],
            "agent_id": p["agent_id"],
            "person_id": p["person_id"],
            "recipient_agent_id": p["recipient_agent_id"],
            "recipient_person_id": p["recipient_person_id"],
            "at": event["event_time"],
            "seq": seq,
            "claim_number": p["claim_number"],
            "policy_id": p.get("policy_id"),
            "policy_seq": p.get("policy_seq"),
            "policy_rule": p.get("policy_rule"),
            "event_id": event["event_id"],
            "event_hash": event["event_hash"],
        }
        state["claims"][p["claim_id"]] = claim
        state["claim_order"].append(p["claim_id"])

    elif et == "AuthorizationCanceled":
        approval = state["approvals"][p["approval_id"]]
        approval["status"] = "canceled"
        app = state["applications"].get(approval["application_id"])
        ticket = state["tickets"].get(approval["ticket_id"])
        if app is not None:
            app["status"] = "canceled"
        if ticket is not None:
            ticket["status"] = "canceled"
        state["cancellations"][p["approval_id"]] = {
            "cancellation_id": p.get("cancellation_id", event["request_id"]),
            "approval_id": p["approval_id"],
            "canceled_by_person_id": p["canceled_by_person_id"],
            "at": event["event_time"],
            "effective_seq": seq,
            "claims_at_cancellation": approval["used_claims"],
            "request_id": event["request_id"],
        }

    elif et == "ComplianceReclamationRecorded":
        approval = state["approvals"][p["approval_id"]]
        prior_status = approval["status"]
        approval["status"] = "reclaimed"
        app = state["applications"].get(approval["application_id"])
        ticket = state["tickets"].get(approval["ticket_id"])
        if app is not None:
            app["status"] = "reclaimed"
        if ticket is not None:
            ticket["status"] = "reclaimed"
        state["reclamations"][p["approval_id"]] = {
            "reclamation_id": p.get("reclamation_id", event["request_id"]),
            "approval_id": p["approval_id"],
            "policy_id": p["policy_id"],
            "policy_seq": p["policy_seq"],
            "reason": p.get("reason", ""),
            "officer_agent_id": p.get("officer_agent_id"),
            "officer_person_id": p.get("officer_person_id"),
            "at": event["event_time"],
            "effective_seq": seq,
            "claims_at_reclamation": approval["used_claims"],
            "prior_status": prior_status,
            "irreversible": True,
            "request_id": event["request_id"],
        }

    else:  # pragma: no cover - defensive replay guard
        raise ValueError(f"unknown event type: {et}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _require_request_id(command: dict[str, Any]) -> str:
    rid = command.get("request_id")
    if not isinstance(rid, str) or not rid:
        raise InvalidCommand("request_id is required")
    return rid


def _require_id(command: dict[str, Any], key: str) -> str:
    value = command.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidCommand(f"{key} is required", {"field": key})
    return value


def _require_int(command: dict[str, Any], key: str) -> int:
    value = command.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCommand(f"{key} must be an integer logical time", {"field": key})
    return value


def _require_list(command: dict[str, Any], key: str) -> list[Any]:
    value = command.get(key)
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list) or not value:
        raise InvalidCommand(f"{key} must be a non-empty list", {"field": key})
    return value


def _problem(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details or {}, "retryable": False}


def _raise_first(problems: list[dict[str, Any]]) -> None:
    if not problems:
        return
    first = problems[0]
    err = AuthorizationStale(first["message"], first.get("details"))
    err.code = first["code"]
    err.retryable = bool(first.get("retryable", False))
    raise err


def active_window(post: dict[str, Any], at: int) -> dict[str, Any] | None:
    matches = [w for w in post.get("windows", []) if w["valid_from"] <= at < w["valid_to"]]
    return matches[0] if len(matches) == 1 else None


def active_policy(state: dict[str, Any]) -> dict[str, Any] | None:
    return state["policies"][-1] if state["policies"] else None


# ---------------------------------------------------------------------------
# Explanations used by both writes and audit
# ---------------------------------------------------------------------------


def explain_issue(state: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    application_id = command.get("application_id")
    post_id = command.get("post_id")
    agent_id = command.get("agent_id")
    at = command.get("at")
    problems: list[dict[str, Any]] = []

    app = state["applications"].get(application_id) if isinstance(application_id, str) else None
    if app is None:
        problems.append(_problem("UNKNOWN_APPLICATION", "application does not exist", {"application_id": application_id}))
        return {"eligible": False, "problems": problems, "checks": {}}

    signatures = app["signatures"]
    stage = len(signatures)
    expected_post = app["required_posts"][stage] if stage < len(app["required_posts"]) else None

    if app["status"] not in ("awaiting_signatures",):
        problems.append(
            _problem(
                "AUTHORIZATION_STALE",
                f"application is not signable: {app['status']}",
                {"application_id": application_id, "status": app["status"]},
            )
        )

    if expected_post is None:
        problems.append(
            _problem(
                "TICKET_ALREADY_ISSUED",
                "all custodian signatures have already been recorded",
                {"application_id": application_id, "signatures": stage},
            )
        )
    elif post_id != expected_post:
        problems.append(
            _problem(
                MissingPredecessor.code,
                "this custodian stage lacks its required predecessor",
                {
                    "application_id": application_id,
                    "completed_signatures": stage,
                    "expected_stage": stage,
                    "expected_post_id": expected_post,
                    "provided_post_id": post_id,
                },
            )
        )

    agent = state["agents"].get(agent_id) if isinstance(agent_id, str) else None
    if agent is None:
        problems.append(_problem("UNKNOWN_AGENT", "signing agent does not exist", {"agent_id": agent_id}))
        person_id = None
    else:
        person_id = agent["person_id"]

    post = state["posts"].get(post_id) if isinstance(post_id, str) else None
    if post is None:
        problems.append(_problem("UNKNOWN_POST", "custodian post does not exist", {"post_id": post_id}))
        window = None
    elif not isinstance(at, int) or isinstance(at, bool):
        problems.append(_problem(InvalidCommand.code, "at must be an integer logical time", {"field": "at"}))
        window = None
    else:
        window = active_window(post, at)
        if window is None:
            problems.append(
                _problem(
                    "POST_WINDOW_INACTIVE",
                    "post has no valid holder window at the event time",
                    {"post_id": post_id, "at": at},
                )
            )
        elif agent is not None and window["agent_id"] != agent_id:
            problems.append(
                _problem(
                    "AGENT_NOT_POST_HOLDER",
                    "agent is not the active holder of this post at event time",
                    {
                        "post_id": post_id,
                        "at": at,
                        "active_agent_id": window["agent_id"],
                        "provided_agent_id": agent_id,
                    },
                )
            )

    if person_id is not None:
        overlaps = [s for s in signatures if s["person_id"] == person_id]
        if overlaps:
            problems.append(
                _problem(
                    NaturalPersonOverlap.code,
                    "another signing agent is backed by the same natural person",
                    {
                        "natural_person_id": person_id,
                        "candidate_agent_id": agent_id,
                        "overlapping_signatures": overlaps,
                    },
                )
            )

    if isinstance(at, int) and not isinstance(at, bool):
        if at < int(app.get("created_at", at)):
            problems.append(
                _problem(
                    "EVENT_TIME_OUT_OF_ORDER",
                    "signature event time precedes application creation",
                    {"application_created_at": app.get("created_at"), "provided_at": at},
                )
            )
        if signatures and at < signatures[-1]["at"]:
            problems.append(
                _problem(
                    "EVENT_TIME_OUT_OF_ORDER",
                    "signature event time precedes the previous signature",
                    {"last_at": signatures[-1]["at"], "provided_at": at},
                )
            )

    checks = {
        "stage": stage,
        "expected_post_id": expected_post,
        "candidate_person_id": person_id,
        "distinct_signed_natural_person_ids": sorted({s["person_id"] for s in signatures}),
        "active_window": window,
    }
    return {"eligible": not problems, "problems": problems, "checks": checks}


def explain_take(state: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    approval_id = command.get("approval_id")
    claim_id = command.get("claim_id")
    object_id = command.get("object_id")
    agent_id = command.get("agent_id")
    at = command.get("at")
    problems: list[dict[str, Any]] = []
    context: dict[str, Any] = {}

    approval = state["approvals"].get(approval_id) if isinstance(approval_id, str) else None
    if approval is None:
        problems.append(_problem("UNKNOWN_APPROVAL", "approval does not exist", {"approval_id": approval_id}))
        return {"eligible": False, "problems": problems, "checks": {}, "context": {}}

    app = state["applications"].get(approval["application_id"])
    context["approval"] = approval
    context["application"] = app

    if approval["status"] == "pending":
        missing = len(app["required_posts"]) - len(app["signatures"]) if app else None
        problems.append(
            _problem(
                MissingPredecessor.code,
                "ticket is not fully signed by its ordered custodians",
                {
                    "approval_id": approval_id,
                    "application_id": approval["application_id"],
                    "completed_signatures": len(app["signatures"]) if app else 0,
                    "missing_signatures": missing,
                },
            )
        )
    elif approval["status"] == "canceled":
        problems.append(
            _problem(
                "AUTHORIZATION_STALE",
                "approval was canceled by its user",
                {"approval_id": approval_id, "stale_reason": "user_canceled"},
            )
        )
    elif approval["status"] == "reclaimed":
        problems.append(
            _problem(
                "AUTHORIZATION_STALE",
                "approval was reclaimed for compliance",
                {"approval_id": approval_id, "stale_reason": "compliance_reclaimed"},
            )
        )
    elif approval["status"] != "active":
        problems.append(
            _problem(
                "AUTHORIZATION_STALE",
                f"approval is not active: {approval['status']}",
                {"approval_id": approval_id, "stale_reason": approval["status"]},
            )
        )

    if isinstance(at, int) and not isinstance(at, bool):
        if not (approval["valid_from"] <= at < approval["valid_to"]):
            problems.append(
                _problem(
                    "AUTHORIZATION_STALE",
                    "take event time is outside the authorization window",
                    {
                        "approval_id": approval_id,
                        "stale_reason": "outside_validity_window",
                        "valid_from": approval["valid_from"],
                        "valid_to": approval["valid_to"],
                        "at": at,
                    },
                )
            )
        if approval.get("issued_at") is not None and at < approval["issued_at"]:
            problems.append(
                _problem(
                    "EVENT_TIME_OUT_OF_ORDER",
                    "take event time precedes final issuance",
                    {"issued_at": approval["issued_at"], "provided_at": at},
                )
            )
    else:
        problems.append(_problem(InvalidCommand.code, "at must be an integer logical time", {"field": "at"}))

    policy = active_policy(state)
    context["policy"] = policy
    if policy and policy.get("rule", {}).get("effect") == "freeze":
        problems.append(
            _problem(
                "AUTHORIZATION_STALE",
                "the current approved policy freezes new claims",
                {
                    "approval_id": approval_id,
                    "stale_reason": "policy_freeze",
                    "policy_id": policy["policy_id"],
                    "policy_seq": policy["approved_seq"],
                },
            )
        )

    post_id = command.get("post_id") or approval["release_post_id"]
    context["post_id"] = post_id
    post = state["posts"].get(post_id) if isinstance(post_id, str) else None
    if command.get("post_id") and command["post_id"] != approval["release_post_id"]:
        problems.append(
            _problem(
                "WRONG_RELEASE_POST",
                "post is not the approval's designated release post",
                {"provided_post_id": command["post_id"], "release_post_id": approval["release_post_id"]},
            )
        )
    if post is None:
        problems.append(_problem("UNKNOWN_POST", "release post does not exist", {"post_id": post_id}))
        window = None
    elif isinstance(at, int) and not isinstance(at, bool):
        window = active_window(post, at)
        context["post_window"] = window
        if window is None:
            problems.append(
                _problem(
                    "POST_WINDOW_INACTIVE",
                    "release post has no valid holder window at take time",
                    {"post_id": post_id, "at": at},
                )
            )
        elif isinstance(agent_id, str) and window["agent_id"] != agent_id:
            problems.append(
                _problem(
                    "AGENT_NOT_POST_HOLDER",
                    "take agent is not the active release-post holder",
                    {
                        "post_id": post_id,
                        "at": at,
                        "active_agent_id": window["agent_id"],
                        "provided_agent_id": agent_id,
                    },
                )
            )

    agent = state["agents"].get(agent_id) if isinstance(agent_id, str) else None
    if agent is None:
        problems.append(_problem("UNKNOWN_AGENT", "take/release agent does not exist", {"agent_id": agent_id}))
    else:
        context["agent"] = agent

    recipient_agent_id = command.get("recipient_agent_id") or agent_id
    recipient = state["agents"].get(recipient_agent_id) if isinstance(recipient_agent_id, str) else None
    if recipient is None:
        problems.append(
            _problem("UNKNOWN_AGENT", "recipient agent does not exist", {"recipient_agent_id": recipient_agent_id})
        )
    else:
        context["recipient"] = recipient

    if object_id not in approval["object_ids"]:
        problems.append(
            _problem(
                "OBJECT_NOT_AUTHORIZED",
                "object is not listed in the ticket approval",
                {"approval_id": approval_id, "object_id": object_id, "allowed_object_ids": approval["object_ids"]},
            )
        )

    used = int(approval["used_claims"])
    context["claim_number"] = used + 1
    context["remaining_before"] = max(0, int(approval["max_claims"]) - used)
    if used >= int(approval["max_claims"]):
        problems.append(
            _problem(
                "QUOTA_EXHAUSTED",
                "approval has reached its total claim limit",
                {
                    "approval_id": approval_id,
                    "used_claims": used,
                    "max_claims": approval["max_claims"],
                    "requested_claim_id": claim_id,
                },
            )
        )

    if isinstance(claim_id, str) and claim_id in state["claims"]:
        problems.append(
            _problem(
                "CLAIM_ID_CONFLICT",
                "claim_id was already used by a different request",
                {"claim_id": claim_id, "existing_seq": state["claims"][claim_id]["seq"]},
            )
        )

    return {
        "eligible": not problems,
        "problems": problems,
        "checks": {
            "used_claims": used,
            "max_claims": approval["max_claims"],
            "remaining_claims": max(0, int(approval["max_claims"]) - used),
            "policy_id": policy["policy_id"] if policy else None,
            "policy_seq": policy["approved_seq"] if policy else None,
        },
        "context": context,
    }


# ---------------------------------------------------------------------------
# Command transition
# ---------------------------------------------------------------------------


def apply_command(
    state: dict[str, Any], command: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate *command* and return ``(next_state, event)``.

    The input state is not mutated.
    """

    request_id = _require_request_id(command)
    name = command.get("command")
    at = command.get("at")

    if name == "register_natural_person":
        pid = _require_id(command, "person_id")
        if pid in state["persons"]:
            raise InvalidCommand("person_id already exists", {"person_id": pid})
        payload = {"person_id": pid, "label": command.get("label", pid)}
        event = build_event(
            state,
            "NaturalPersonRegistered",
            _optional_int(command, "at"),
            request_id,
            payload,
        )

    elif name == "register_agent":
        aid = _require_id(command, "agent_id")
        pid = _require_id(command, "person_id")
        if aid in state["agents"]:
            raise InvalidCommand("agent_id already exists", {"agent_id": aid})
        if pid not in state["persons"]:
            raise InvalidCommand("person_id does not exist", {"person_id": pid})
        payload = {"agent_id": aid, "person_id": pid, "label": command.get("label", aid)}
        event = build_event(
            state,
            "AgentRegistered",
            _optional_int(command, "at"),
            request_id,
            payload,
        )

    elif name == "register_post":
        post_id = _require_id(command, "post_id")
        agent_id = _require_id(command, "agent_id")
        valid_from = _require_int(command, "valid_from")
        valid_to = _require_int(command, "valid_to")
        if valid_from >= valid_to:
            raise InvalidCommand("valid_from must be before valid_to")
        if agent_id not in state["agents"]:
            raise InvalidCommand("agent_id does not exist", {"agent_id": agent_id})
        post = state["posts"].get(post_id)
        if post:
            for w in post["windows"]:
                if max(w["valid_from"], valid_from) < min(w["valid_to"], valid_to):
                    raise InvalidCommand(
                        "post validity windows overlap",
                        {"post_id": post_id, "existing_window": w, "new_from": valid_from, "new_to": valid_to},
                    )
        payload = {
            "post_id": post_id,
            "agent_id": agent_id,
            "valid_from": valid_from,
            "valid_to": valid_to,
        }
        event = build_event(state, "PostWindowRegistered", at if at is not None else valid_from, request_id, payload)

    elif name == "approve_policy":
        policy_id = _require_id(command, "policy_id")
        if policy_id in {p["policy_id"] for p in state["policies"]}:
            raise InvalidCommand("policy_id already exists", {"policy_id": policy_id})
        rule = command.get("rule")
        if rule is None:
            effect = command.get("effect", "none")
            if effect not in ("none", "freeze"):
                raise InvalidCommand("policy effect must be none or freeze", {"effect": effect})
            rule = {"effect": effect}
        if not isinstance(rule, dict):
            raise InvalidCommand("policy rule must be an object")
        rule.setdefault("effect", "none")
        if rule.get("effect") not in ("none", "freeze"):
            raise InvalidCommand("unsupported policy effect", {"rule": rule})
        payload = {"policy_id": policy_id, "title": command.get("title", policy_id), "rule": rule}
        event = build_event(state, "PolicyApproved", _optional_int(command, "at"), request_id, payload)

    elif name == "create_application":
        app_id = _require_id(command, "application_id")
        owner_id = _require_id(command, "owner_person_id")
        ticket_id = _require_id(command, "ticket_id")
        required_posts = [str(x) for x in _require_list(command, "required_posts")]
        object_ids = [str(x) for x in _require_list(command, "object_ids")]
        max_claims = command.get("max_claims", 1)
        valid_from = _require_int(command, "valid_from")
        valid_to = _require_int(command, "valid_to")
        approval_id = command.get("approval_id") or f"apv-{ticket_id}"

        if app_id in state["applications"]:
            raise InvalidCommand("application_id already exists", {"application_id": app_id})
        if ticket_id in state["tickets"]:
            raise InvalidCommand("ticket_id already exists", {"ticket_id": ticket_id})
        if approval_id in state["approvals"]:
            raise InvalidCommand("approval_id already exists", {"approval_id": approval_id})
        if owner_id not in state["persons"]:
            raise InvalidCommand("owner person does not exist", {"owner_person_id": owner_id})
        if len(required_posts) < 2 or len(set(required_posts)) != len(required_posts):
            raise InvalidCommand("required_posts must contain at least two distinct posts")
        missing_posts = [p for p in required_posts if p not in state["posts"]]
        if missing_posts:
            raise InvalidCommand("one or more posts do not exist", {"missing_posts": missing_posts})
        if len(set(object_ids)) != len(object_ids):
            raise InvalidCommand("object_ids must be distinct")
        if not isinstance(max_claims, int) or isinstance(max_claims, bool) or max_claims < 1:
            raise InvalidCommand("max_claims must be a positive integer")
        if valid_from >= valid_to:
            raise InvalidCommand("valid_from must be before valid_to")

        payload = {
            "application_id": app_id,
            "owner_person_id": owner_id,
            "ticket_id": ticket_id,
            "approval_id": approval_id,
            "required_posts": required_posts,
            "release_post_id": required_posts[-1],
            "object_ids": object_ids,
            "max_claims": max_claims,
            "valid_from": valid_from,
            "valid_to": valid_to,
        }
        event = build_event(state, "ApplicationCreated", _require_int(command, "at"), request_id, payload)

    elif name == "issue_ticket":
        explanation = explain_issue(state, command)
        if not explanation["eligible"]:
            _raise_first(explanation["problems"])
        app_id = _require_id(command, "application_id")
        post_id = _require_id(command, "post_id")
        agent_id = _require_id(command, "agent_id")
        at = _require_int(command, "at")
        app = state["applications"][app_id]
        stage = len(app["signatures"])
        person_id = state["agents"][agent_id]["person_id"]
        issued = stage + 1 == len(app["required_posts"])
        payload = {
            "application_id": app_id,
            "post_id": post_id,
            "agent_id": agent_id,
            "person_id": person_id,
            "stage_index": stage,
            "issued": issued,
        }
        event = build_event(state, "TicketIssuanceRecorded", at, request_id, payload)

    elif name == "take_claim":
        explanation = explain_take(state, command)
        if not explanation["eligible"]:
            _raise_first(explanation["problems"])
        ctx = explanation["context"]
        approval = ctx["approval"]
        app = ctx["application"]
        agent = ctx["agent"]
        recipient = ctx["recipient"]
        policy = ctx.get("policy")
        at = _require_int(command, "at")
        claim_id = _require_id(command, "claim_id")
        object_id = _require_id(command, "object_id")
        post_id = ctx["post_id"]
        claim_number = int(ctx["claim_number"])
        payload = {
            "claim_id": claim_id,
            "approval_id": approval["approval_id"],
            "application_id": approval["application_id"],
            "ticket_id": approval["ticket_id"],
            "object_id": object_id,
            "post_id": post_id,
            "agent_id": agent["agent_id"],
            "person_id": agent["person_id"],
            "recipient_agent_id": recipient["agent_id"],
            "recipient_person_id": recipient["person_id"],
            "claim_number": claim_number,
            "policy_id": policy["policy_id"] if policy else None,
            "policy_seq": policy["approved_seq"] if policy else None,
            "policy_rule": policy["rule"] if policy else None,
        }
        event = build_event(state, "ClaimTaken", at, request_id, payload)

    elif name == "cancel_authorization":
        approval_id = _require_id(command, "approval_id")
        person_id = _require_id(command, "person_id")
        at = _require_int(command, "at")
        approval = state["approvals"].get(approval_id)
        if approval is None:
            raise InvalidCommand("approval does not exist", {"approval_id": approval_id})
        app = state["applications"][approval["application_id"]]
        if at < int(app["created_at"]):
            raise EventTimeOrder(
                "cancellation event time precedes application creation",
                {"application_created_at": app["created_at"], "provided_at": at},
            )
        if person_id != app["owner_person_id"]:
            raise InvalidCommand("only the owning natural person can cancel", {"owner_person_id": app["owner_person_id"]})
        if person_id not in state["persons"]:
            raise InvalidCommand("person does not exist", {"person_id": person_id})
        if approval_id in state["reclamations"]:
            err = AuthorizationStale(
                "compliance reclamation is already effective and cannot be canceled",
                {"approval_id": approval_id, "terminal": "reclaimed"},
            )
            err.code = "AUTHORIZATION_ALREADY_TERMINAL"
            raise err
        if approval_id in state["cancellations"]:
            err = AuthorizationStale("authorization was already canceled", {"approval_id": approval_id})
            err.code = "AUTHORIZATION_ALREADY_TERMINAL"
            raise err
        payload = {"approval_id": approval_id, "canceled_by_person_id": person_id}
        event = build_event(state, "AuthorizationCanceled", at, request_id, payload)

    elif name == "record_reclamation":
        approval_id = _require_id(command, "approval_id")
        policy_id = _require_id(command, "policy_id")
        at = _require_int(command, "at")
        approval = state["approvals"].get(approval_id)
        if approval is None:
            raise InvalidCommand("approval does not exist", {"approval_id": approval_id})
        policy = next((p for p in state["policies"] if p["policy_id"] == policy_id), None)
        if policy is None:
            raise InvalidCommand("policy does not exist", {"policy_id": policy_id})
        related_app = state["applications"].get(approval["application_id"])
        if related_app is not None and at < int(related_app["created_at"]):
            raise EventTimeOrder(
                "reclamation event time precedes application creation",
                {"application_created_at": related_app["created_at"], "provided_at": at},
            )
        officer_agent_id = command.get("officer_agent_id")
        officer_person_id = None
        if officer_agent_id is not None:
            officer = state["agents"].get(officer_agent_id)
            if officer is None:
                raise InvalidCommand("officer agent does not exist", {"officer_agent_id": officer_agent_id})
            officer_person_id = officer["person_id"]
        if approval_id in state["reclamations"]:
            err = AuthorizationStale("authorization has already been reclaimed", {"approval_id": approval_id})
            err.code = "AUTHORIZATION_ALREADY_TERMINAL"
            raise err
        payload = {
            "approval_id": approval_id,
            "policy_id": policy_id,
            "policy_seq": policy["approved_seq"],
            "reason": command.get("reason", ""),
            "officer_agent_id": officer_agent_id,
            "officer_person_id": officer_person_id,
        }
        event = build_event(state, "ComplianceReclamationRecorded", at, request_id, payload)

    elif name == "reverse_reclamation":
        approval_id = command.get("approval_id")
        details = {"approval_id": approval_id}
        if isinstance(approval_id, str) and approval_id in state["reclamations"]:
            rec = state["reclamations"][approval_id]
            details.update(
                {
                    "effective_seq": rec["effective_seq"],
                    "policy_seq": rec["policy_seq"],
                    "irreversible": True,
                }
            )
        raise ReclamationIrreversible(
            "an effective compliance reclamation cannot be reversed by a later policy",
            details,
        )

    else:
        raise InvalidCommand(f"unknown command: {name}", {"command": name})

    return reduce_event(state, event, verify=True), event


def _optional_int(command: dict[str, Any], key: str) -> int | None:
    value = command.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCommand(f"{key} must be an integer")
    return value


def build_receipt(command: dict[str, Any], event: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Build the stable, replayable receipt for a committed event."""

    return {
        "receipt_version": 1,
        "request_id": command["request_id"],
        "command": command["command"],
        "seq": event["seq"],
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "event_time": event["event_time"],
        "event_hash": event["event_hash"],
        "prev_hash": event["prev_hash"],
        "state_hash": state_hash(state),
        "result": event["payload"],
    }
