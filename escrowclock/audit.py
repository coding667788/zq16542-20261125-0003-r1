"""Point-in-time and snapshot audit projection."""

from __future__ import annotations

from typing import Any

from . import model
from .store import Store


def _signature_basis(signatures: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for sig in signatures:
        post = state["posts"].get(sig["post_id"], {})
        result.append(
            {
                "stage": sig["stage"],
                "post_id": sig["post_id"],
                "agent_id": sig["agent_id"],
                "natural_person_id": sig["person_id"],
                "at": sig["at"],
                "seq": sig["seq"],
                "event_hash": sig["event_hash"],
                "post_windows": post.get("windows", []),
            }
        )
    return result


def claim_basis(state: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    approval = state["approvals"].get(claim["approval_id"], {})
    application = state["applications"].get(approval.get("application_id"), {})
    ticket = state["tickets"].get(approval.get("ticket_id"), {})
    signatures = application.get("signatures", [])
    person_ids = [s["person_id"] for s in signatures]

    return {
        "claim": claim,
        "who": {
            "release_agent_id": claim["agent_id"],
            "release_natural_person_id": claim["person_id"],
            "recipient_agent_id": claim["recipient_agent_id"],
            "recipient_natural_person_id": claim["recipient_person_id"],
        },
        "authorization": {
            "approval_id": claim["approval_id"],
            "approval_status_at_audit": approval.get("status"),
            "ticket_id": claim["ticket_id"],
            "application_id": claim["application_id"],
            "object_id": claim["object_id"],
            "claim_number": claim["claim_number"],
            "max_claims": approval.get("max_claims"),
            "used_claims_at_audit": approval.get("used_claims"),
            "valid_from": approval.get("valid_from"),
            "valid_to": approval.get("valid_to"),
        },
        "custodian_chain": {
            "required_posts": application.get("required_posts", []),
            "signatures": _signature_basis(signatures, state),
            "distinct_natural_person_ids": sorted(set(person_ids)),
            "natural_person_overlap": len(person_ids) != len(set(person_ids)),
        },
        "policy_basis": {
            "policy_id": claim.get("policy_id"),
            "approved_seq": claim.get("policy_seq"),
            "rule_at_claim": claim.get("policy_rule"),
        },
        "ticket": ticket,
    }


def _claim_matches(basis: dict[str, Any], filters: dict[str, Any]) -> bool:
    claim = basis["claim"]
    who = basis["who"]
    if filters.get("claim_id") and claim["claim_id"] != filters["claim_id"]:
        return False
    if filters.get("approval_id") and claim["approval_id"] != filters["approval_id"]:
        return False
    if filters.get("application_id") and claim["application_id"] != filters["application_id"]:
        return False
    if filters.get("ticket_id") and claim["ticket_id"] != filters["ticket_id"]:
        return False
    if filters.get("object_id") and claim["object_id"] != filters["object_id"]:
        return False
    if filters.get("agent_id") and filters["agent_id"] not in (
        claim["agent_id"],
        claim["recipient_agent_id"],
    ):
        return False
    if filters.get("person_id") and filters["person_id"] not in (
        claim["person_id"],
        claim["recipient_person_id"],
    ):
        return False
    return True


def run_audit(
    store: Store,
    *,
    at: int | None = None,
    snapshot: str | int | None = None,
    filters: dict[str, Any] | None = None,
    include_events: bool = False,
    include_all_requests: bool = False,
) -> dict[str, Any]:
    filters = filters or {}
    requested_snapshot = snapshot
    snapshot_seq = store.resolve_snapshot_seq(snapshot)
    prefix_state = store.replay_state(up_to_seq=snapshot_seq)
    known_seq = int(prefix_state["head_seq"])
    state = (
        prefix_state
        if at is None
        else store.replay_state(up_to_seq=known_seq, at=at)
    )

    bases = [claim_basis(state, state["claims"][cid]) for cid in state["claim_order"]]
    bases = [b for b in bases if _claim_matches(b, filters)]

    request_records = store.list_request_records(
        snapshot_seq=known_seq if snapshot is not None else None
    )
    if not include_all_requests:
        request_records = [r for r in request_records if r["status"] == "rejected"]
        # Remove bulky response while retaining the durable key/code.
        for record in request_records:
            response = record.pop("response", None)
            if response is not None:
                record["error"] = response.get("error")

    result: dict[str, Any] = {
        "ok": True,
        "audit": {
            "requested_snapshot": requested_snapshot,
            "snapshot_seq": known_seq,
            "event_time": at,
            "state_hash": model.state_hash(state),
            "full_snapshot_state_hash": model.state_hash(prefix_state),
            "claim_count": len(bases),
            "claims": bases,
            "persisted_request_records": request_records,
        },
    }

    if include_events:
        rows = store.conn.execute(
            "SELECT * FROM events WHERE seq <= ? ORDER BY seq", (known_seq,)
        ).fetchall()
        events = [Store.row_to_event(row) for row in rows]
        if at is not None:
            events = [
                e
                for e in events
                if e["event_time"] is None or e["event_time"] <= at
            ]
        result["audit"]["events"] = events

    return result


def run_explanation(
    store: Store,
    *,
    action: str,
    command: dict[str, Any],
    at: int | None = None,
    snapshot: str | int | None = None,
) -> dict[str, Any]:
    snapshot_seq = store.resolve_snapshot_seq(snapshot)
    prefix_state = store.replay_state(up_to_seq=snapshot_seq)
    known_seq = int(prefix_state["head_seq"])
    state = prefix_state if at is None else store.replay_state(up_to_seq=known_seq, at=at)
    command = {**command, "request_id": "audit-preview"}
    if action == "issue":
        explanation = model.explain_issue(state, command)
    elif action == "take":
        explanation = model.explain_take(state, command)
        # Context contains live state objects and is JSON safe, but remove it
        # from the public explanation to keep output compact/stable.
        explanation.pop("context", None)
    else:
        raise ValueError("action must be issue or take")

    return {
        "ok": True,
        "audit": {
            "mode": f"explain_{action}",
            "event_time": at,
            "snapshot_seq": known_seq,
            "state_hash": model.state_hash(state),
            "explanation": explanation,
        },
    }
