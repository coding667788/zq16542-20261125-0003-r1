"""UTF-8 JSON command line interface for escrowclock."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from .audit import run_audit, run_explanation
from .errors import DeterministicCrash, EscrowError
from .fixtures import load_fixture_file
from .store import CRASH_POINTS, Store

DEFAULT_DB = "escrowclock.db"
META_KEYS = {"db", "crash", "crash_mode", "handler", "command_name"}


def csv_list(value: str) -> list[str]:
    if value is None:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m escrowclock",
        description="Escrow ticket issuance, one-time claim, crash replay, and temporal audit simulator.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    def add_db(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db",
            default=os.environ.get("ESCROWCLOCK_DB", DEFAULT_DB),
            help="Local SQLite database path (default: escrowclock.db or $ESCROWCLOCK_DB).",
        )
        p.add_argument(
            "--crash",
            choices=(
                *CRASH_POINTS,
                "before-write",
                "after-write",
                "after-commit",
                "before-row-write",
            ),
            help="Inject a deterministic crash at the selected point.",
        )
        p.add_argument(
            "--crash-mode",
            choices=("process", "exception"),
            default="process",
            help="process exits with code 86; exception is useful for in-process tests.",
        )

    # init / fixture / replay / snapshot -----------------------------------
    p_init = sub.add_parser("init", help="Create the SQLite database/schema.")
    add_db(p_init)
    p_init.add_argument("--fixture", help="Optional synthetic fixture JSON to load.")
    p_init.set_defaults(handler="init")

    p_fix = sub.add_parser("load-fixture", help="Load a synthetic JSON command fixture.")
    add_db(p_fix)
    p_fix.add_argument("path", help="Path to fixtures/synthetic.json")
    p_fix.set_defaults(handler="load_fixture")

    p_replay = sub.add_parser("replay", help="Replay all events and verify hashes/integrity.")
    add_db(p_replay)
    p_replay.set_defaults(handler="replay")

    p_snap = sub.add_parser("snapshot", help="Persist a known state snapshot.")
    add_db(p_snap)
    p_snap.add_argument("--snapshot-id", help="Optional explicit snapshot ID.")
    p_snap.set_defaults(handler="snapshot")

    p_verify = sub.add_parser("verify-receipt", help="Replay history and verify a saved receipt or request ID.")
    add_db(p_verify)
    p_verify.add_argument("--receipt-file", help="JSON file containing a CLI success envelope or receipt.")
    p_verify.add_argument("--request-id", help="Verify the persisted receipt for this idempotency key.")
    p_verify.set_defaults(handler="verify_receipt")

    # setup commands ---------------------------------------------------------
    p_person = sub.add_parser("register-person", help="Register a synthetic natural person.")
    add_db(p_person)
    p_person.add_argument("--request-id", required=True)
    p_person.add_argument("--person-id", required=True)
    p_person.add_argument("--label")
    p_person.add_argument("--at", type=int)
    p_person.set_defaults(handler="dispatch", command_name="register_natural_person")

    p_agent = sub.add_parser("register-agent", help="Register an agent backed by a person.")
    add_db(p_agent)
    p_agent.add_argument("--request-id", required=True)
    p_agent.add_argument("--agent-id", required=True)
    p_agent.add_argument("--person-id", required=True)
    p_agent.add_argument("--label")
    p_agent.add_argument("--at", type=int)
    p_agent.set_defaults(handler="dispatch", command_name="register_agent")

    p_post = sub.add_parser("register-post", help="Register a custodian post validity window.")
    add_db(p_post)
    p_post.add_argument("--request-id", required=True)
    p_post.add_argument("--post-id", required=True)
    p_post.add_argument("--agent-id", required=True)
    p_post.add_argument("--valid-from", required=True, type=int)
    p_post.add_argument("--valid-to", required=True, type=int)
    p_post.add_argument("--at", type=int)
    p_post.set_defaults(handler="dispatch", command_name="register_post")

    p_policy = sub.add_parser("approve-policy", help="Approve a policy at its event sequence.")
    add_db(p_policy)
    p_policy.add_argument("--request-id", required=True)
    p_policy.add_argument("--policy-id", required=True)
    p_policy.add_argument("--effect", choices=("none", "freeze"), default="none")
    p_policy.add_argument("--title")
    p_policy.add_argument("--at", type=int)
    p_policy.set_defaults(handler="approve_policy")

    # domain commands --------------------------------------------------------
    p_apply = sub.add_parser("apply", help="Create an application and planned ticket/approval.")
    add_db(p_apply)
    p_apply.add_argument("--request-id", required=True)
    p_apply.add_argument("--application-id", required=True)
    p_apply.add_argument("--owner-person-id", required=True)
    p_apply.add_argument("--ticket-id", required=True)
    p_apply.add_argument("--approval-id")
    p_apply.add_argument("--required-posts", required=True, type=csv_list)
    p_apply.add_argument("--object-ids", required=True, type=csv_list)
    p_apply.add_argument("--max-claims", type=int, default=1)
    p_apply.add_argument("--valid-from", required=True, type=int)
    p_apply.add_argument("--valid-to", required=True, type=int)
    p_apply.add_argument("--at", required=True, type=int)
    p_apply.set_defaults(handler="dispatch", command_name="create_application")

    p_issue = sub.add_parser("issue", help="Record the next ordered custodian signature/issuance.")
    add_db(p_issue)
    p_issue.add_argument("--request-id", required=True)
    p_issue.add_argument("--application-id", required=True)
    p_issue.add_argument("--post-id", required=True)
    p_issue.add_argument("--agent-id", required=True)
    p_issue.add_argument("--at", required=True, type=int)
    p_issue.set_defaults(handler="dispatch", command_name="issue_ticket")

    p_take = sub.add_parser("take", help="Competitively claim one authorized object.")
    add_db(p_take)
    p_take.add_argument("--request-id", required=True)
    p_take.add_argument("--approval-id", required=True)
    p_take.add_argument("--claim-id", required=True)
    p_take.add_argument("--object-id", required=True)
    p_take.add_argument("--agent-id", required=True, help="Active release-post agent.")
    p_take.add_argument("--recipient-agent-id")
    p_take.add_argument("--post-id")
    p_take.add_argument("--at", required=True, type=int)
    p_take.set_defaults(handler="dispatch", command_name="take_claim")

    p_cancel = sub.add_parser("cancel", help="Owner cancellation, potentially racing take.")
    add_db(p_cancel)
    p_cancel.add_argument("--request-id", required=True)
    p_cancel.add_argument("--approval-id", required=True)
    p_cancel.add_argument("--person-id", required=True)
    p_cancel.add_argument("--at", required=True, type=int)
    p_cancel.set_defaults(handler="dispatch", command_name="cancel_authorization")

    p_reclaim = sub.add_parser("reclaim", help="Record an effective compliance reclamation.")
    add_db(p_reclaim)
    p_reclaim.add_argument("--request-id", required=True)
    p_reclaim.add_argument("--approval-id", required=True)
    p_reclaim.add_argument("--policy-id", required=True)
    p_reclaim.add_argument("--officer-agent-id")
    p_reclaim.add_argument("--reason", default="")
    p_reclaim.add_argument("--at", required=True, type=int)
    p_reclaim.set_defaults(handler="dispatch", command_name="record_reclamation")

    p_reverse = sub.add_parser("reverse-reclaim", help="Attempt to reverse an effective reclaim; always rejected.")
    add_db(p_reverse)
    p_reverse.add_argument("--request-id", required=True)
    p_reverse.add_argument("--approval-id", required=True)
    p_reverse.set_defaults(handler="dispatch", command_name="reverse_reclamation")

    # audit ------------------------------------------------------------------
    p_audit = sub.add_parser("audit", help="Temporal/snapshot audit and eligibility explanations.")
    add_db(p_audit)
    p_audit.add_argument("--at", type=int, help="Logical event-time cutoff.")
    p_audit.add_argument(
        "--snapshot",
        help="Known snapshot: sequence number, snp-... ID, or receipt state_hash.",
    )
    p_audit.add_argument("--claim-id")
    p_audit.add_argument("--approval-id")
    p_audit.add_argument("--application-id")
    p_audit.add_argument("--ticket-id")
    p_audit.add_argument("--object-id")
    p_audit.add_argument("--agent-id")
    p_audit.add_argument("--person-id")
    p_audit.add_argument("--include-events", action="store_true")
    p_audit.add_argument("--include-all-requests", action="store_true")
    p_audit.add_argument("--explain", choices=("issue", "take"))
    # Explanation-only fields reuse the same names where possible.
    p_audit.add_argument("--post-id")
    p_audit.set_defaults(handler="audit")

    return parser


def _normalize_crash_point(value: str | None) -> str | None:
    aliases = {
        "before-write": "before_write",
        "after-write": "after_write",
        "after-commit": "after_commit",
        "before-row-write": "before_row_write",
    }
    return aliases.get(value or "", value)


def _command_from_args(args: argparse.Namespace) -> dict[str, Any]:
    data = vars(args)
    command = {k: v for k, v in data.items() if k not in META_KEYS and v is not None}
    command["command"] = getattr(args, "command_name", args.handler)
    return command


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _open_store(args: argparse.Namespace) -> Store:
    crash_point = _normalize_crash_point(getattr(args, "crash", None))
    if crash_point is None:
        crash_point = _normalize_crash_point(os.environ.get("ESCROWCLOCK_CRASH"))
    crash_mode = getattr(args, "crash_mode", "process")
    if not getattr(args, "crash", None) and os.environ.get("ESCROWCLOCK_CRASH_MODE"):
        crash_mode = os.environ["ESCROWCLOCK_CRASH_MODE"]
    return Store(args.db, crash_point=crash_point, crash_mode=crash_mode)


def _handle_approve_policy(args: argparse.Namespace) -> dict[str, Any]:
    command = _command_from_args(args)
    command["rule"] = {"effect": command.pop("effect", "none")}
    with _open_store(args) as store:
        return store.dispatch(command)


def _handle_audit(args: argparse.Namespace) -> dict[str, Any]:
    filters = {
        "claim_id": args.claim_id,
        "approval_id": args.approval_id,
        "application_id": args.application_id,
        "ticket_id": args.ticket_id,
        "object_id": args.object_id,
        "agent_id": args.agent_id,
        "person_id": args.person_id,
    }
    filters = {k: v for k, v in filters.items() if v is not None}
    with _open_store(args) as store:
        if args.explain:
            command = {
                "application_id": args.application_id,
                "approval_id": args.approval_id,
                "claim_id": args.claim_id,
                "object_id": args.object_id,
                "agent_id": args.agent_id,
                "post_id": args.post_id,
                "at": args.at,
            }
            return run_explanation(
                store,
                action=args.explain,
                command=command,
                at=args.at,
                snapshot=args.snapshot,
            )
        return run_audit(
            store,
            at=args.at,
            snapshot=args.snapshot,
            filters=filters,
            include_events=args.include_events,
            include_all_requests=args.include_all_requests,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Ensure Windows console/pipes emit UTF-8 rather than cp1252.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        if args.handler == "init":
            with _open_store(args) as store:
                recovery = store.recover()
                result: dict[str, Any] = {"ok": True, "initialized": True, "db": args.db, "recovery": recovery}
                if args.fixture:
                    fixture_summary = load_fixture_file(store, args.fixture)
                    fixture_summary.pop("results", None)
                    result["fixture"] = fixture_summary
                _print_json(result)
                return 0

        if args.handler == "load_fixture":
            with _open_store(args) as store:
                result = load_fixture_file(store, Path(args.path))
                result.pop("results", None)
                _print_json(result)
                return 0 if result["ok"] else 2

        if args.handler == "replay":
            with _open_store(args) as store:
                result = {"ok": True, "replay": store.recover()}
                _print_json(result)
                return 0

        if args.handler == "snapshot":
            with _open_store(args) as store:
                result = {"ok": True, "snapshot": store.create_snapshot(args.snapshot_id)}
                _print_json(result)
                return 0

        if args.handler == "verify_receipt":
            with _open_store(args) as store:
                receipt: dict[str, Any] | None = None
                if args.receipt_file:
                    with Path(args.receipt_file).open("r", encoding="utf-8") as fh:
                        saved = json.load(fh)
                    receipt = saved.get("receipt", saved) if isinstance(saved, dict) else None
                elif args.request_id:
                    row = store.conn.execute(
                        "SELECT response_json FROM request_records WHERE request_id = ?",
                        (args.request_id,),
                    ).fetchone()
                    if row is None:
                        raise EscrowError("unknown request id", {"request_id": args.request_id})
                    saved = json.loads(row["response_json"])
                    receipt = saved.get("receipt")
                if not isinstance(receipt, dict):
                    raise EscrowError("no success receipt found to verify")
                verification = store.verify_receipt(receipt)
                result = {"ok": True, "verification": verification}
                _print_json(result)
                return 0

        if args.handler == "audit":
            result = _handle_audit(args)
            _print_json(result)
            return 0

        if args.handler == "approve_policy":
            result = _handle_approve_policy(args)
        else:
            command = _command_from_args(args)
            with _open_store(args) as store:
                result = store.dispatch(command)

        _print_json(result)
        return 0 if result.get("ok") else 2

    except DeterministicCrash as exc:
        _print_json({"ok": False, "crash": True, "point": exc.point})
        return exc.exit_code
    except EscrowError as exc:
        _print_json({"ok": False, "replayed": False, "error": exc.to_dict()})
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - CLI boundary must emit JSON.
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            }
        )
        return 70


def console_main() -> None:
    raise SystemExit(main())


def _fixed_subcommand_main(subcommand: str) -> None:
    raise SystemExit(main([subcommand, *sys.argv[1:]]))


def issue_main() -> None:
    _fixed_subcommand_main("issue")


def take_main() -> None:
    _fixed_subcommand_main("take")


def audit_main() -> None:
    _fixed_subcommand_main("audit")


if __name__ == "__main__":
    console_main()
