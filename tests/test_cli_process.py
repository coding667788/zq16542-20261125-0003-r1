from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support import FIXTURE_PATH, ROOT  # noqa: E402

from escrowclock.fixtures import load_fixture_file  # noqa: E402
from escrowclock.store import Store  # noqa: E402


class CliProcessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="escrowclock-cli-test-")
        self.db_path = Path(self.temp_dir.name) / "cli.db"
        store = Store(self.db_path)
        load_fixture_file(store, FIXTURE_PATH)
        replay = store.recover()
        self.fixture_head_seq = replay["replayed_events"]
        store.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def cli_env(self) -> dict[str, str]:
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing if existing else "")
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "escrowclock", *args],
            cwd=ROOT,
            env=self.cli_env(),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
        )

    def test_issue_take_audit_cli_emit_utf8_json(self) -> None:
        issue = self.run_cli(
            "issue",
            "--db",
            str(self.db_path),
            "--request-id",
            "cli-issue-1",
            "--application-id",
            "app_demo",
            "--post-id",
            "post_intake",
            "--agent-id",
            "ag_ada_laptop",
            "--at",
            "30",
        )
        self.assertEqual(issue.returncode, 0, issue.stderr)
        issue_json = json.loads(issue.stdout)
        self.assertTrue(issue_json["ok"])

        # Complete remaining signatures.
        for request_id, post, agent in (
            ("cli-issue-2", "post_review", "ag_bob_desk"),
            ("cli-issue-3", "post_release", "ag_cara_hsm"),
        ):
            result = self.run_cli(
                "issue",
                "--db",
                str(self.db_path),
                "--request-id",
                request_id,
                "--application-id",
                "app_demo",
                "--post-id",
                post,
                "--agent-id",
                agent,
                "--at",
                "31" if request_id.endswith("2") else "32",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        take = self.run_cli(
            "take",
            "--db",
            str(self.db_path),
            "--request-id",
            "cli-take-1",
            "--approval-id",
            "apv_demo",
            "--claim-id",
            "claim_cli_demo",
            "--object-id",
            "obj_profile_7001",
            "--agent-id",
            "ag_cara_hsm",
            "--recipient-agent-id",
            "ag_owner_portal",
            "--at",
            "40",
        )
        self.assertEqual(take.returncode, 0, take.stderr)
        self.assertTrue(json.loads(take.stdout)["ok"])

        audit = self.run_cli(
            "audit",
            "--db",
            str(self.db_path),
            "--claim-id",
            "claim_cli_demo",
        )
        self.assertEqual(audit.returncode, 0, audit.stderr)
        audit_json = json.loads(audit.stdout)
        audited_claim = audit_json["audit"]["claims"][0]
        self.assertEqual(audited_claim["claim"]["claim_id"], "claim_cli_demo")
        self.assertEqual(len(audited_claim["custodian_chain"]["signatures"]), 3)

    def test_deterministic_process_crash_before_commit_and_after_commit_replay(self) -> None:
        args = [
            "issue",
            "--db",
            str(self.db_path),
            "--request-id",
            "crash-issue-request",
            "--application-id",
            "app_demo",
            "--post-id",
            "post_intake",
            "--agent-id",
            "ag_ada_laptop",
            "--at",
            "50",
        ]
        crashed = self.run_cli(*args, "--crash", "after-write")
        self.assertEqual(crashed.returncode, 86, crashed.stdout + crashed.stderr)
        crash_json = json.loads(crashed.stdout)
        self.assertTrue(crash_json["crash"])
        self.assertEqual(crash_json["point"], "after_write")

        store = Store(self.db_path)
        self.assertEqual(store.replay_state()["head_seq"], self.fixture_head_seq)
        self.assertIsNone(
            store.conn.execute(
                "SELECT 1 FROM request_records WHERE request_id='crash-issue-request'"
            ).fetchone()
        )
        store.close()

        retried = self.run_cli(*args)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        retried_json = json.loads(retried.stdout)
        self.assertFalse(retried_json["replayed"])
        first_seq = retried_json["receipt"]["seq"]

        second_args = [
            "issue",
            "--db",
            str(self.db_path),
            "--request-id",
            "crash-after-commit-request",
            "--application-id",
            "app_demo",
            "--post-id",
            "post_review",
            "--agent-id",
            "ag_bob_desk",
            "--at",
            "51",
        ]
        after_commit = self.run_cli(*second_args, "--crash", "after-commit")
        self.assertEqual(after_commit.returncode, 86, after_commit.stdout + after_commit.stderr)

        store = Store(self.db_path)
        row = store.conn.execute(
            "SELECT event_seq FROM request_records WHERE request_id='crash-after-commit-request'"
        ).fetchone()
        self.assertIsNotNone(row)
        committed_seq = row["event_seq"]
        store.close()

        replay = self.run_cli(*second_args)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_json = json.loads(replay.stdout)
        self.assertTrue(replay_json["replayed"])
        self.assertEqual(replay_json["receipt"]["seq"], committed_seq)
        self.assertEqual(first_seq + 1, committed_seq)

    def test_two_processes_competing_for_last_claim_commit_exactly_one(self) -> None:
        for round_index in range(3):
            with self.subTest(round=round_index):
                temp_dir = tempfile.TemporaryDirectory(prefix=f"escrowclock-race-{round_index}-")
                self.addCleanup(temp_dir.cleanup)
                db_path = Path(temp_dir.name) / "race.db"
                store = Store(db_path)
                load_fixture_file(store, FIXTURE_PATH)
                store.close()

                processes = []
                for i in (1, 2):
                    request_id = f"race-round-{round_index}-req-{i}"
                    claim_id = f"race-round-{round_index}-claim-{i}"
                    args = [
                        sys.executable,
                        "-m",
                        "escrowclock",
                        "take",
                        "--db",
                        str(db_path),
                        "--request-id",
                        request_id,
                        "--approval-id",
                        "apv_race",
                        "--claim-id",
                        claim_id,
                        "--object-id",
                        "obj_last_slot",
                        "--agent-id",
                        "ag_cara_hsm",
                        "--at",
                        "600",
                    ]
                    processes.append(
                        subprocess.Popen(
                            args,
                            cwd=ROOT,
                            env=self.cli_env(),
                            text=True,
                            encoding="utf-8",
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                    )

                outputs = []
                for proc in processes:
                    stdout, stderr = proc.communicate(timeout=60)
                    outputs.append((proc.returncode, stdout, stderr))

                successes = []
                quota_failures = []
                for returncode, stdout, stderr in outputs:
                    self.assertNotEqual(returncode, 70, stdout + stderr)
                    payload = json.loads(stdout)
                    if returncode == 0:
                        successes.append(payload)
                    else:
                        self.assertEqual(returncode, 2, stderr)
                        self.assertFalse(payload["ok"])
                        self.assertEqual(payload["error"]["code"], "QUOTA_EXHAUSTED")
                        quota_failures.append(payload)

                self.assertEqual(len(successes), 1, outputs)
                self.assertEqual(len(quota_failures), 1, outputs)

                verify = Store(db_path)
                state = verify.replay_state()
                self.assertEqual(state["approvals"]["apv_race"]["used_claims"], 1)
                race_claims = [
                    cid
                    for cid in state["claim_order"]
                    if cid.startswith(f"race-round-{round_index}-claim-")
                ]
                self.assertEqual(len(race_claims), 1)
                records = verify.conn.execute(
                    "SELECT status, error_code FROM request_records WHERE request_id LIKE ?",
                    (f"race-round-{round_index}-req-%",),
                ).fetchall()
                self.assertEqual(len(records), 2)
                self.assertEqual({r["status"] for r in records}, {"applied", "rejected"})
                verify.close()

    def test_take_actually_races_cancellation_and_reclamation(self) -> None:
        terminal_commands = (
            (
                "cancel",
                [
                    "cancel",
                    "--approval-id",
                    "apv_race",
                    "--person-id",
                    "np_owner_7001",
                    "--at",
                    "620",
                ],
                "cancellations",
            ),
            (
                "reclaim",
                [
                    "reclaim",
                    "--approval-id",
                    "apv_race",
                    "--policy-id",
                    "pol_baseline_v1",
                    "--officer-agent-id",
                    "ag_dan_compliance",
                    "--reason",
                    "concurrent synthetic compliance action",
                    "--at",
                    "621",
                ],
                "reclamations",
            ),
        )

        for label, terminal_args, state_map in terminal_commands:
            with self.subTest(action=label):
                temp_dir = tempfile.TemporaryDirectory(prefix=f"escrowclock-{label}-race-")
                self.addCleanup(temp_dir.cleanup)
                db_path = Path(temp_dir.name) / "race.db"
                setup = Store(db_path)
                load_fixture_file(setup, FIXTURE_PATH)
                setup.close()

                take_args = [
                    sys.executable,
                    "-m",
                    "escrowclock",
                    "take",
                    "--db",
                    str(db_path),
                    "--request-id",
                    f"concurrent-{label}-take",
                    "--approval-id",
                    "apv_race",
                    "--claim-id",
                    f"claim-concurrent-{label}",
                    "--object-id",
                    "obj_last_slot",
                    "--agent-id",
                    "ag_cara_hsm",
                    "--at",
                    "619",
                ]
                other_args = [
                    sys.executable,
                    "-m",
                    "escrowclock",
                    *terminal_args,
                    "--db",
                    str(db_path),
                    "--request-id",
                    f"concurrent-{label}-terminal",
                ]
                procs = [
                    subprocess.Popen(
                        args,
                        cwd=ROOT,
                        env=self.cli_env(),
                        text=True,
                        encoding="utf-8",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    for args in (take_args, other_args)
                ]
                results = []
                for proc in procs:
                    stdout, stderr = proc.communicate(timeout=60)
                    results.append((proc.returncode, stdout, stderr))

                take_rc, take_out, take_err = results[0]
                terminal_rc, terminal_out, terminal_err = results[1]
                self.assertEqual(terminal_rc, 0, terminal_out + terminal_err)
                terminal_json = json.loads(terminal_out)
                self.assertTrue(terminal_json["ok"])

                take_json = json.loads(take_out)
                self.assertIn(take_rc, (0, 2), take_out + take_err)
                if take_rc == 2:
                    self.assertEqual(take_json["error"]["code"], "AUTHORIZATION_STALE")

                verify = Store(db_path)
                state = verify.replay_state()
                self.assertIn("apv_race", state[state_map])
                expected_claims = 1 if take_rc == 0 else 0
                actual_claims = [
                    cid for cid in state["claim_order"] if cid == f"claim-concurrent-{label}"
                ]
                self.assertEqual(len(actual_claims), expected_claims)
                # A terminal action never erases a committed claim; it records
                # how many claims had already happened in its serialized order.
                terminal_record = state[state_map]["apv_race"]
                self.assertIn(terminal_record["claims_at_" + ("cancellation" if label == "cancel" else "reclamation")], (0, 1))
                verify.close()

    def test_identical_idempotent_concurrent_request_commits_one_claim(self) -> None:
        args_base = [
            sys.executable,
            "-m",
            "escrowclock",
            "take",
            "--db",
            str(self.db_path),
            "--request-id",
            "same-concurrent-key",
            "--approval-id",
            "apv_race",
            "--claim-id",
            "same-concurrent-claim",
            "--object-id",
            "obj_last_slot",
            "--agent-id",
            "ag_cara_hsm",
            "--at",
            "601",
        ]
        procs = [
            subprocess.Popen(
                args_base,
                cwd=ROOT,
                env=self.cli_env(),
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        payloads = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, stdout + stderr)
            payloads.append(json.loads(stdout))

        self.assertEqual(payloads[0]["receipt"]["seq"], payloads[1]["receipt"]["seq"])
        self.assertEqual(
            payloads[0]["receipt"]["event_hash"], payloads[1]["receipt"]["event_hash"]
        )
        store = Store(self.db_path)
        state = store.replay_state()
        self.assertEqual(state["approvals"]["apv_race"]["used_claims"], 1)
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
