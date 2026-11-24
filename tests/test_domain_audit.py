from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import TempStoreTestCase

from escrowclock.audit import run_audit, run_explanation
from escrowclock.model import state_hash


class DomainAuditTest(TempStoreTestCase):
    def test_fixture_persists_expected_failures_and_replay_matches(self) -> None:
        replay = self.store.recover()
        self.assertEqual(replay["integrity_check"], "ok")
        self.assertGreaterEqual(replay["replayed_events"], 37)
        self.assertGreaterEqual(replay["rejected"], 2)

        state = self.store.replay_state()
        self.assertEqual(state_hash(state), replay["state_hash"])

    def test_successful_chain_has_distinct_natural_persons(self) -> None:
        audit = run_audit(self.store, filters={"claim_id": "claim_spent_1"})["audit"]
        claim = audit["claims"][0]
        chain = claim["custodian_chain"]
        self.assertEqual(
            chain["distinct_natural_person_ids"],
            ["np_ada_7002", "np_bob_7003", "np_cara_7004"],
        )
        self.assertFalse(chain["natural_person_overlap"])
        self.assertEqual(claim["who"]["recipient_natural_person_id"], "np_owner_7001")
        self.assertEqual(claim["authorization"]["claim_number"], 1)

    def test_natural_person_overlap_is_distinguishable_and_persisted(self) -> None:
        response = self.store.dispatch(
            {
                "command": "issue_ticket",
                "request_id": "test-overlap-again",
                "application_id": "app_overlap",
                "post_id": "post_overlap_demo",
                "agent_id": "ag_ada_cli",
                "at": 403,
            }
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "NATURAL_PERSON_OVERLAP")
        overlap = response["error"]["details"]["overlapping_signatures"]
        self.assertEqual(overlap[0]["agent_id"], "ag_ada_laptop")
        self.assertEqual(response["error"]["details"]["natural_person_id"], "np_ada_7002")

        record = self.store.conn.execute(
            "SELECT status, error_code FROM request_records WHERE request_id=?",
            ("test-overlap-again",),
        ).fetchone()
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["error_code"], "NATURAL_PERSON_OVERLAP")

        explanation = run_explanation(
            self.store,
            action="issue",
            command={
                "application_id": "app_overlap",
                "post_id": "post_overlap_demo",
                "agent_id": "ag_ada_cli",
                "at": 403,
            },
        )
        codes = [p["code"] for p in explanation["audit"]["explanation"]["problems"]]
        self.assertIn("NATURAL_PERSON_OVERLAP", codes)

    def test_missing_predecessor_is_distinguishable(self) -> None:
        explanation = run_explanation(
            self.store,
            action="issue",
            command={
                "application_id": "app_demo",
                "post_id": "post_release",
                "agent_id": "ag_cara_hsm",
                "at": 15,
            },
        )
        problem = explanation["audit"]["explanation"]["problems"][0]
        self.assertEqual(problem["code"], "MISSING_PREDECESSOR")
        self.assertEqual(problem["details"]["expected_post_id"], "post_intake")

    def test_issuance_outside_post_window_is_rejected(self) -> None:
        response = self.store.dispatch(
            {
                "command": "issue_ticket",
                "request_id": "test-issue-after-post-window",
                "application_id": "app_demo",
                "post_id": "post_intake",
                "agent_id": "ag_ada_laptop",
                "at": 10_000,
            }
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "POST_WINDOW_INACTIVE")

    def test_take_outside_release_post_window_is_rejected(self) -> None:
        self.store.dispatch(
            {
                "command": "register_post",
                "request_id": "test-register-narrow-post",
                "post_id": "post_narrow_release",
                "agent_id": "ag_cara_hsm",
                "valid_from": 540,
                "valid_to": 600,
                "at": 500,
            }
        )
        self.store.dispatch(
            {
                "command": "create_application",
                "request_id": "test-narrow-post-app",
                "application_id": "app_narrow_post",
                "owner_person_id": "np_owner_7001",
                "ticket_id": "tkt_narrow_post",
                "approval_id": "apv_narrow_post",
                "required_posts": ["post_intake", "post_review", "post_narrow_release"],
                "object_ids": ["obj_narrow"],
                "max_claims": 1,
                "valid_from": 500,
                "valid_to": 2000,
                "at": 500,
            }
        )
        for i, (post, agent, at) in enumerate(
            [
                ("post_intake", "ag_ada_laptop", 501),
                ("post_review", "ag_bob_desk", 502),
                ("post_narrow_release", "ag_cara_hsm", 550),
            ],
            start=1,
        ):
            result = self.store.dispatch(
                {
                    "command": "issue_ticket",
                    "request_id": f"test-narrow-sig-{i}",
                    "application_id": "app_narrow_post",
                    "post_id": post,
                    "agent_id": agent,
                    "at": at,
                }
            )
            self.assertTrue(result["ok"], result)

        late_take = self.store.dispatch(
            {
                "command": "take_claim",
                "request_id": "test-narrow-late-take",
                "approval_id": "apv_narrow_post",
                "claim_id": "claim_narrow_late",
                "object_id": "obj_narrow",
                "agent_id": "ag_cara_hsm",
                "at": 700,
            }
        )
        self.assertEqual(late_take["error"]["code"], "POST_WINDOW_INACTIVE")

    def test_quota_exhausted_is_distinguishable(self) -> None:
        explanation = run_explanation(
            self.store,
            action="take",
            command={
                "approval_id": "apv_spent",
                "claim_id": "claim_explain_quota",
                "object_id": "obj_spent_profile",
                "agent_id": "ag_cara_hsm",
                "at": 220,
            },
        )
        codes = [p["code"] for p in explanation["audit"]["explanation"]["problems"]]
        self.assertIn("QUOTA_EXHAUSTED", codes)
        self.assertNotIn("AUTHORIZATION_STALE", codes)

    def test_cancellation_makes_authorization_stale_but_keeps_history(self) -> None:
        explanation = run_explanation(
            self.store,
            action="take",
            command={
                "approval_id": "apv_canceled",
                "claim_id": "claim_after_cancel",
                "object_id": "obj_canceled",
                "agent_id": "ag_cara_hsm",
                "at": 320,
            },
        )
        problem_codes = [p["code"] for p in explanation["audit"]["explanation"]["problems"]]
        self.assertIn("AUTHORIZATION_STALE", problem_codes)
        stale = next(p for p in explanation["audit"]["explanation"]["problems"] if p["code"] == "AUTHORIZATION_STALE")
        self.assertEqual(stale["details"]["stale_reason"], "user_canceled")

        audit = run_audit(self.store, filters={"approval_id": "apv_canceled"})["audit"]
        self.assertEqual(audit["claim_count"], 0)

    def test_expired_authorization_is_stale(self) -> None:
        response = self.store.dispatch(
            {
                "command": "take_claim",
                "request_id": "test-expired-take",
                "approval_id": "apv_expired",
                "claim_id": "claim_expired_late",
                "object_id": "obj_old_profile",
                "agent_id": "ag_cara_hsm",
                "at": 700,
            }
        )
        self.assertEqual(response["error"]["code"], "AUTHORIZATION_STALE")
        self.assertEqual(response["error"]["details"]["stale_reason"], "outside_validity_window")

    def test_event_time_audit_and_snapshot_audit(self) -> None:
        before = run_audit(self.store, at=209, filters={"claim_id": "claim_spent_1"})["audit"]
        self.assertEqual(before["claim_count"], 0)

        at_time = run_audit(self.store, at=210, filters={"claim_id": "claim_spent_1"})["audit"]
        self.assertEqual(at_time["claim_count"], 1)

        head_seq = self.store.replay_state()["head_seq"]
        early_snapshot = run_audit(self.store, snapshot=1, include_all_requests=True)["audit"]
        self.assertEqual(early_snapshot["snapshot_seq"], 1)
        self.assertEqual(early_snapshot["claim_count"], 0)
        self.assertEqual(len(early_snapshot["persisted_request_records"]), 1)
        self.assertEqual(
            early_snapshot["persisted_request_records"][0]["request_id"],
            "fix-person-owner",
        )

        hash_one = state_hash(self.store.replay_state(up_to_seq=1))
        hash_snapshot = run_audit(self.store, snapshot=hash_one)["audit"]
        self.assertEqual(hash_snapshot["snapshot_seq"], 1)

        full_snapshot = run_audit(self.store, snapshot=head_seq)["audit"]
        self.assertEqual(full_snapshot["snapshot_seq"], head_seq)
        self.assertGreaterEqual(len(full_snapshot["claims"]), 1)

    def test_successful_receipt_can_be_replayed_and_verified(self) -> None:
        response = self.store.dispatch(
            {
                "command": "take_claim",
                "request_id": "verify-receipt-unit",
                "approval_id": "apv_race",
                "claim_id": "claim_verify_receipt",
                "object_id": "obj_last_slot",
                "agent_id": "ag_cara_hsm",
                "recipient_agent_id": "ag_owner_portal",
                "at": 150,
            }
        )
        self.assertTrue(response["ok"], response)
        verification = self.store.verify_receipt(response["receipt"])
        self.assertTrue(verification["valid"])
        self.assertTrue(all(verification["checks"].values()))

    def test_failed_idempotency_key_is_stable(self) -> None:
        command = {
            "command": "take_claim",
            "request_id": "persisted-failure-key",
            "approval_id": "apv_spent",
            "claim_id": "claim_persisted_failure",
            "object_id": "obj_spent_profile",
            "agent_id": "ag_cara_hsm",
            "at": 220,
        }
        first = self.store.dispatch(command)
        self.assertFalse(first["ok"])
        self.assertEqual(first["error"]["code"], "QUOTA_EXHAUSTED")

        second = self.store.dispatch(command)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["error"]["code"], "QUOTA_EXHAUSTED")

        # A different body under the same key is a conflict, not a new execute.
        changed = dict(command, object_id="different-object")
        conflict = self.store.dispatch(changed)
        self.assertEqual(conflict["error"]["code"], "IDEMPOTENCY_KEY_CONFLICT")

    def test_policy_cannot_reverse_effective_reclamation(self) -> None:
        reclaim = self.store.dispatch(
            {
                "command": "record_reclamation",
                "request_id": "test-reclaim-race-approval",
                "approval_id": "apv_race",
                "policy_id": "pol_baseline_v1",
                "officer_agent_id": "ag_dan_compliance",
                "reason": "synthetic compliance hold",
                "at": 140,
            }
        )
        self.assertTrue(reclaim["ok"], reclaim)
        reclaim_seq = reclaim["receipt"]["seq"]

        new_policy = self.store.dispatch(
            {
                "command": "approve_policy",
                "request_id": "test-policy-lenient",
                "policy_id": "pol_future_lenient_attempt",
                "effect": "none",
                "at": 141,
            }
        )
        self.assertTrue(new_policy["ok"])
        policy_seq = new_policy["receipt"]["seq"]
        self.assertGreater(policy_seq, reclaim_seq)

        reverse = self.store.dispatch(
            {
                "command": "reverse_reclamation",
                "request_id": "test-reverse-reject",
                "approval_id": "apv_race",
            }
        )
        self.assertEqual(reverse["error"]["code"], "RECLAMATION_IRREVERSIBLE")
        self.assertEqual(reverse["error"]["details"]["effective_seq"], reclaim_seq)

        state = self.store.replay_state()
        self.assertEqual(state["approvals"]["apv_race"]["status"], "reclaimed")
        self.assertEqual(
            state["reclamations"]["apv_race"]["policy_seq"],
            reclaim["receipt"]["result"]["policy_seq"],
        )

    def test_freeze_policy_is_effective_from_approval_sequence(self) -> None:
        # Create and fully sign a fresh approval with quota remaining.
        self._create_fresh_approval("app_policy", "apv_policy", max_claims=2)
        before_policy = self.store.dispatch(
            self._take_command("apv_policy", "claim_policy_before", at=610)
        )
        self.assertTrue(before_policy["ok"], before_policy)

        policy = self.store.dispatch(
            {
                "command": "approve_policy",
                "request_id": "policy-freeze-seq",
                "policy_id": "pol_freeze_now",
                "effect": "freeze",
                "at": 611,
            }
        )
        freeze_seq = policy["receipt"]["seq"]

        after = self.store.dispatch(
            self._take_command("apv_policy", "claim_policy_after", at=612)
        )
        self.assertFalse(after["ok"])
        self.assertEqual(after["error"]["code"], "AUTHORIZATION_STALE")
        self.assertEqual(after["error"]["details"]["stale_reason"], "policy_freeze")
        self.assertEqual(after["error"]["details"]["policy_seq"], freeze_seq)

        # Audit at event time before the take still sees the first claim.
        audit = run_audit(self.store, at=610, filters={"approval_id": "apv_policy"})["audit"]
        self.assertEqual(audit["claim_count"], 1)

    def _create_fresh_approval(self, app_id: str, approval_id: str, max_claims: int) -> None:
        ticket_id = f"tkt-{app_id}"
        self.store.dispatch(
            {
                "command": "create_application",
                "request_id": f"req-{app_id}",
                "application_id": app_id,
                "owner_person_id": "np_owner_7001",
                "ticket_id": ticket_id,
                "approval_id": approval_id,
                "required_posts": ["post_intake", "post_review", "post_release"],
                "object_ids": ["obj_a", "obj_b"],
                "max_claims": max_claims,
                "valid_from": 600,
                "valid_to": 9000,
                "at": 600,
            }
        )
        for i, (post, agent, at) in enumerate(
            [
                ("post_intake", "ag_ada_laptop", 601),
                ("post_review", "ag_bob_desk", 602),
                ("post_release", "ag_cara_hsm", 603),
            ],
            start=1,
        ):
            result = self.store.dispatch(
                {
                    "command": "issue_ticket",
                    "request_id": f"req-{app_id}-sig-{i}",
                    "application_id": app_id,
                    "post_id": post,
                    "agent_id": agent,
                    "at": at,
                }
            )
            assert result["ok"], result

    def _take_command(self, approval_id: str, claim_id: str, at: int) -> dict:
        return {
            "command": "take_claim",
            "request_id": f"req-{claim_id}",
            "approval_id": approval_id,
            "claim_id": claim_id,
            "object_id": "obj_a",
            "agent_id": "ag_cara_hsm",
            "recipient_agent_id": "ag_owner_portal",
            "at": at,
        }


if __name__ == "__main__":
    unittest.main(verbosity=2)
