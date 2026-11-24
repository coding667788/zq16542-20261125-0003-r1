from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support import FIXTURE_PATH  # noqa: E402

from escrowclock.canonical import canonical_text  # noqa: E402
from escrowclock.errors import DeterministicCrash  # noqa: E402
from escrowclock.fixtures import load_fixture_dict  # noqa: E402
from escrowclock.reference import SerialReference  # noqa: E402
from escrowclock.store import Store  # noqa: E402


class Attempt:
    def __init__(self, command: dict[str, Any], crash_point: str | None):
        self.command = command
        self.crash_point = crash_point
        self.crash_consumed = False
        self.mirrored_to_reference = False
        self.done = False


class RandomRecoveryEquivalenceTest(unittest.TestCase):
    def test_random_interleavings_and_crashes_match_serial_reference(self) -> None:
        with FIXTURE_PATH.open("r", encoding="utf-8") as fh:
            fixture = json.load(fh)

        seed_count = int(__import__("os").environ.get("RANDOM_SEEDS", "40"))
        for seed in range(seed_count):
            with self.subTest(seed=seed):
                rng = random.Random(0xE5C0C10C + seed)
                seed_fixture = json.loads(json.dumps(fixture))
                temp_dir = tempfile.TemporaryDirectory(prefix=f"escrowclock-rand-{seed}-")
                self.addCleanup(temp_dir.cleanup)
                db_path = Path(temp_dir.name) / "state.db"
                stores: list[Store] = []

                def close_stores() -> None:
                    for active_store in reversed(stores):
                        active_store.close()

                self.addCleanup(close_stores)
                store = Store(db_path, crash_mode="exception")
                stores.append(store)
                summary = load_fixture_dict(store, seed_fixture)
                self.assertTrue(summary["ok"])

                reference = SerialReference()
                for command in seed_fixture["commands"]:
                    expected = reference.dispatch(command)
                    # Fixture responses are checked by the loader; this also
                    # ensures the reference accepts the same command stream.
                    self.assertIn("ok", expected)

                # Deterministically create two fully issued approvals used for
                # cancellation/reclamation races.
                for app_id, approval_id, at in (
                    ("app_rand_cancel", "apv_rand_cancel", 700),
                    ("app_rand_reclaim", "apv_rand_reclaim", 750),
                ):
                    self._prepare_approval(store, reference, app_id, approval_id, at)

                attempts = self._build_attempts(rng)

                guard = 0
                while any(not a.done for a in attempts):
                    guard += 1
                    self.assertLess(guard, len(attempts) * 5 + 10)
                    pending = [a for a in attempts if not a.done]
                    attempt = rng.choice(pending)
                    command = attempt.command

                    if attempt.crash_consumed:
                        store.crash_point = None
                    else:
                        store.crash_point = attempt.crash_point

                    try:
                        real_response = store.dispatch(command)
                    except DeterministicCrash as crash:
                        if crash.point == "after_commit":
                            reference.dispatch(command)
                            attempt.mirrored_to_reference = True
                        # before_write/before_row_write/after_write leave no
                        # committed SQLite state, so reference gets no effect.
                        attempt.crash_consumed = True
                        store.close()
                        store = Store(db_path, crash_mode="exception")
                        stores.append(store)
                        continue

                    if not attempt.mirrored_to_reference:
                        expected_response = reference.dispatch(command)
                    else:
                        expected_response = reference.dispatch(command)

                    self.assertEqual(
                        canonical_text(real_response),
                        canonical_text(expected_response),
                        msg=f"response mismatch for {command['request_id']} seed={seed}",
                    )
                    attempt.done = True

                store.crash_point = None
                real_state = store.replay_state()
                self.assertEqual(
                    canonical_text(real_state),
                    canonical_text(reference.state),
                    msg=f"state mismatch seed={seed}",
                )

                real_rows = {
                    row["request_id"]: row
                    for row in store.conn.execute("SELECT * FROM request_records")
                }
                self.assertEqual(set(real_rows), set(reference.records))
                for request_id, expected_record in reference.records.items():
                    row = real_rows[request_id]
                    self.assertEqual(row["request_hash"], expected_record["request_hash"])
                    self.assertEqual(row["status"], expected_record["status"])
                    self.assertEqual(row["error_code"], expected_record["error_code"])
                    self.assertEqual(row["event_seq"], expected_record["event_seq"])
                    stored_response = json.loads(row["response_json"])
                    self.assertEqual(
                        canonical_text(stored_response),
                        canonical_text(expected_record["response"]),
                    )

                recovery = store.recover()
                self.assertEqual(recovery["integrity_check"], "ok")
                self.assertEqual(recovery["state_hash"], reference.state_hash())
                store.close()
                temp_dir.cleanup()

    def _prepare_approval(
        self,
        store: Store,
        reference: SerialReference,
        app_id: str,
        approval_id: str,
        at: int,
    ) -> None:
        commands = [
            {
                "command": "create_application",
                "request_id": f"{app_id}-apply",
                "application_id": app_id,
                "owner_person_id": "np_owner_7001",
                "ticket_id": f"tkt_{app_id}",
                "approval_id": approval_id,
                "required_posts": ["post_intake", "post_review", "post_release"],
                "object_ids": ["obj_race_a", "obj_race_b"],
                "max_claims": 1,
                "valid_from": at,
                "valid_to": 9000,
                "at": at,
            },
            {
                "command": "issue_ticket",
                "request_id": f"{app_id}-sig-1",
                "application_id": app_id,
                "post_id": "post_intake",
                "agent_id": "ag_ada_laptop",
                "at": at + 1,
            },
            {
                "command": "issue_ticket",
                "request_id": f"{app_id}-sig-2",
                "application_id": app_id,
                "post_id": "post_review",
                "agent_id": "ag_bob_desk",
                "at": at + 2,
            },
            {
                "command": "issue_ticket",
                "request_id": f"{app_id}-sig-3",
                "application_id": app_id,
                "post_id": "post_release",
                "agent_id": "ag_cara_hsm",
                "at": at + 3,
            },
        ]
        for command in commands:
            real = store.dispatch(command)
            expected = reference.dispatch(command)
            self.assertTrue(real["ok"], real)
            self.assertEqual(canonical_text(real), canonical_text(expected))

    def _build_attempts(self, rng: random.Random) -> list[Attempt]:
        crash_choices = [None, None, "before_write", "after_write", "after_commit"]
        attempts: list[Attempt] = []

        def add(command: dict[str, Any]) -> None:
            attempts.append(Attempt(command, rng.choice(crash_choices)))

        # Two distinct processes/requests compete for the one apv_race slot.
        for i in (1, 2):
            add(
                {
                    "command": "take_claim",
                    "request_id": f"random-race-take-{i}",
                    "approval_id": "apv_race",
                    "claim_id": f"random-race-claim-{i}",
                    "object_id": "obj_last_slot",
                    "agent_id": "ag_cara_hsm",
                    "recipient_agent_id": "ag_owner_portal",
                    "at": 500 + i,
                }
            )

        # Take versus user cancellation.
        add(
            {
                "command": "take_claim",
                "request_id": "random-cancel-take",
                "approval_id": "apv_rand_cancel",
                "claim_id": "claim_cancel_race",
                "object_id": "obj_race_a",
                "agent_id": "ag_cara_hsm",
                "at": 710,
            }
        )
        add(
            {
                "command": "cancel_authorization",
                "request_id": "random-cancel-user",
                "approval_id": "apv_rand_cancel",
                "person_id": "np_owner_7001",
                "at": 711,
            }
        )

        # Take versus compliance reclamation.
        add(
            {
                "command": "take_claim",
                "request_id": "random-reclaim-take",
                "approval_id": "apv_rand_reclaim",
                "claim_id": "claim_reclaim_race",
                "object_id": "obj_race_a",
                "agent_id": "ag_cara_hsm",
                "at": 760,
            }
        )
        add(
            {
                "command": "record_reclamation",
                "request_id": "random-reclaim-record",
                "approval_id": "apv_rand_reclaim",
                "policy_id": "pol_baseline_v1",
                "officer_agent_id": "ag_dan_compliance",
                "reason": "random synthetic interleaving",
                "at": 761,
            }
        )

        # Two full lifecycles. Random scheduling may intentionally attempt a
        # successor before its predecessor; both implementations must agree.
        for n in (1, 2):
            app_id = f"app_random_life_{n}"
            approval_id = f"apv_random_life_{n}"
            base = 800 + 100 * n
            add(
                {
                    "command": "create_application",
                    "request_id": f"random-life-{n}-apply",
                    "application_id": app_id,
                    "owner_person_id": "np_owner_7001",
                    "ticket_id": f"tkt_random_life_{n}",
                    "approval_id": approval_id,
                    "required_posts": ["post_intake", "post_review", "post_release"],
                    "object_ids": [f"obj_life_{n}"],
                    "max_claims": 1,
                    "valid_from": base,
                    "valid_to": 9000,
                    "at": base,
                }
            )
            for stage, (post, agent) in enumerate(
                [
                    ("post_intake", "ag_ada_laptop"),
                    ("post_review", "ag_bob_desk"),
                    ("post_release", "ag_cara_hsm"),
                ],
                start=1,
            ):
                add(
                    {
                        "command": "issue_ticket",
                        "request_id": f"random-life-{n}-sig-{stage}",
                        "application_id": app_id,
                        "post_id": post,
                        "agent_id": agent,
                        "at": base + stage,
                    }
                )
            add(
                {
                    "command": "take_claim",
                    "request_id": f"random-life-{n}-take",
                    "approval_id": approval_id,
                    "claim_id": f"claim_random_life_{n}",
                    "object_id": f"obj_life_{n}",
                    "agent_id": "ag_cara_hsm",
                    "at": base + 10,
                }
            )

        rng.shuffle(attempts)
        return attempts


if __name__ == "__main__":
    unittest.main(verbosity=2)
