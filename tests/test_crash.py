"""Cross-process durability: real termination at two points, compared row by row.

The "uncommitted interruption" unit is judged only by the database state after a
restart, and the baseline is read straight out of SQLite with the standard
library, never through the code under test.
"""
from __future__ import annotations

import json
import os
import sqlite3
import unittest

from tests.support import (
    HOUR,
    T0_MS,
    TempDirMixin,
    call,
    finish_worker,
    make_directory,
    raw_rows,
    read_receipt,
    snapshot_of,
    start_worker,
)


class CrashTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workdir = self.temp_dir()
        self.db = os.path.join(self.workdir, "state.db")
        directory = make_directory(self.db)
        for tenant in ("t-a", "t-b"):
            call(directory, "tenant.create", {"tenant_id": tenant}, f"mk-{tenant}")
        self.member = call(directory, "member.add",
                           {"tenant_id": "t-a", "email": "alice@example.com"}, "add")["data"]
        self.invite = call(directory, "invite.issue",
                           {"tenant_id": "t-b", "email": "alice@example.com", "ttl_ms": HOUR},
                           "inv")["data"]
        directory.close()
        self.migration = {
            "request_tenant": "t-a",
            "reason": "reorg",
            "moves": [{
                "source": {"tenant_id": "t-a", "member_id": self.member["member_id"],
                           "member_epoch": 1},
                "target": {"tenant_id": "t-b", "email": "alice@example.com",
                           "invite_epoch": self.invite["invite_epoch"]},
            }],
        }

    def _job(self, crash_at: str) -> dict:
        return {
            "db": self.db,
            "op": "migrate.commit",
            "params": self.migration,
            "request_id": "mig-crash",
            "clock_ms": T0_MS,
            "crash_at": crash_at,
            "output": os.path.join(self.workdir, "receipt.json"),
        }

    def test_kill_mid_effects_before_commit_leaves_the_baseline_untouched(self) -> None:
        baseline = raw_rows(self.db)
        process = start_worker(self._job("during_effects"), self.workdir)
        code, out, err = finish_worker(process)
        self.assertEqual(code, 90, out + err)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "receipt.json")))
        self.assertEqual(raw_rows(self.db), baseline)

        # Nothing was half-applied: the source is still active, the invite is
        # still pending, there is no member in the target tenant.
        conn = sqlite3.connect(self.db)
        active = conn.execute(
            "SELECT tenant_id, email FROM members WHERE status = 'active' ORDER BY tenant_id"
        ).fetchall()
        invites = conn.execute("SELECT status FROM invites").fetchall()
        requests = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE request_id = 'mig-crash'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(active, [("t-a", "alice@example.com")])
        self.assertEqual(invites, [("pending",)])
        self.assertEqual(requests, 0)

    def test_kill_after_the_commit_but_before_the_return_keeps_everything(self) -> None:
        process = start_worker(self._job("after_commit"), self.workdir)
        code, out, err = finish_worker(process)
        self.assertEqual(code, 90, out + err)
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "receipt.json")))
        # Whatever the client saw, the restart shows a *complete* migration.
        state = snapshot_of(self.db)
        active = sorted((m["tenant_id"], m["email"])
                        for m in state["members"] if m["status"] == "active")
        self.assertEqual(active, [("t-b", "alice@example.com")])
        self.assertEqual(state["invites"][0]["status"], "consumed")
        self.assertEqual(
            [r["status"] for r in state["requests"] if r["request_id"] == "mig-crash"],
            ["succeeded"],
        )
        with sqlite3.connect(self.db) as conn:
            stored = conn.execute(
                "SELECT response_json, status, error_code FROM requests "
                "WHERE request_id = 'mig-crash'"
            ).fetchone()
        receipt = json.loads(stored[0])
        self.assertEqual(stored[1], "succeeded")
        self.assertIsNone(stored[2])
        self.assertTrue(receipt["ok"])
        # The receipt, the invite consumption, the member change and the audit
        # trail are all in the same committed unit.
        with sqlite3.connect(self.db) as conn:
            audit_requests = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT request_id FROM audits WHERE request_id IS NOT NULL"
                )
            }
        self.assertEqual(audit_requests, {"mk-t-a", "mk-t-b", "add", "inv", "mig-crash"})

        # Row-by-row baseline for the two business tables, written out by hand
        # from the rules (not read back from the implementation).
        rows = raw_rows(self.db)
        self.assertEqual(rows["members"], sorted([
            ("t-a", "alice@t-a.m1", "alice@example.com", "revoked", 2, None, T0_MS, T0_MS),
            ("t-b", "alice@t-b.m1", "alice@example.com", "active", 1, 1, T0_MS, T0_MS),
        ]))
        self.assertEqual(rows["invites"], [
            (1, "t-b", "alice@example.com", 1, "consumed",
             T0_MS + HOUR, T0_MS, T0_MS, T0_MS),
        ])
        actions = [r[3] for r in rows["audits"]]  # audits: id, at_ms, tenant, action, ...
        self.assertEqual(
            [a for a in actions if a.startswith(("migration.", "invite.consumed"))],
            ["migration.source_revoked", "invite.consumed",
             "migration.target_joined", "migration.committed"],
        )

        # Re-sending the exact request returns the durable receipt, and it is
        # identical to the one the crash prevented from being delivered.
        reopened = make_directory(self.db)
        try:
            retry = call(reopened, "migrate.commit", self.migration, "mig-crash")
        finally:
            reopened.close()
        self.assertEqual(retry, receipt)

    def test_kill_at_the_first_crash_point_keeps_the_whole_baseline(self) -> None:
        baseline = raw_rows(self.db)
        process = start_worker(self._job("after_preflight"), self.workdir)
        code, out, err = finish_worker(process)
        self.assertEqual(code, 90, out + err)
        self.assertEqual(raw_rows(self.db), baseline)

    def test_restart_then_fresh_request_succeeds(self) -> None:
        process = start_worker(self._job("during_effects"), self.workdir)
        code, _, _ = finish_worker(process)
        self.assertEqual(code, 90)
        directory = make_directory(self.db)
        receipt = call(directory, "migrate.commit", self.migration, "mig-crash")
        self.assertTrue(receipt["ok"], receipt)
        state = directory.snapshot()
        directory.close()
        self.assertEqual(sorted((m["tenant_id"], m["email"]) for m in state["members"]
                                if m["status"] == "active"),
                         [("t-b", "alice@example.com")])


if __name__ == "__main__":
    unittest.main()
