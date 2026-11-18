"""Cross-process interleavings, fixed by sync points, and idempotency across processes.

Each test starts *only* the children it needs and always terminates them; a
failure path kills the child it spawned and nothing else.
"""
from __future__ import annotations

import os
import unittest

from tests.support import (
    HOUR,
    T0_MS,
    SyncGate,
    TempDirMixin,
    call,
    finish_worker,
    make_directory,
    read_receipt,
    snapshot_of,
    start_worker,
)


class InterleavingTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workdir = self.temp_dir()
        self.db = os.path.join(self.workdir, "state.db")
        self.gate = SyncGate(os.path.join(self.workdir, "sync"))
        setup = make_directory(self.db)
        for tenant in ("t-a", "t-b", "t-c"):
            call(setup, "tenant.create", {"tenant_id": tenant}, f"mk-{tenant}")
        self.alice = call(setup, "member.add",
                          {"tenant_id": "t-a", "email": "alice@example.com"}, "add-a")["data"]
        self.bob = call(setup, "member.add",
                        {"tenant_id": "t-b", "email": "bob@example.com"}, "add-b")["data"]
        self.inv_a = call(setup, "invite.issue",
                          {"tenant_id": "t-b", "email": "alice@example.com", "ttl_ms": HOUR},
                          "inv-a")["data"]
        self.inv_b = call(setup, "invite.issue",
                          {"tenant_id": "t-a", "email": "bob@example.com", "ttl_ms": HOUR},
                          "inv-b")["data"]
        setup.close()
        self.children: list = []

    def tearDown(self) -> None:
        for process in self.children:
            if process.poll() is None:
                process.kill()
                process.communicate()
        super().tearDown()

    def run_worker(self, job: dict) -> tuple[int, str, str]:
        process = start_worker(job, self.workdir)
        self.children.append(process)
        return finish_worker(process)

    def swap_moves(self) -> list[dict]:
        return [
            {"source": {"tenant_id": "t-a", "member_id": self.alice["member_id"],
                        "member_epoch": 1},
             "target": {"tenant_id": "t-b", "email": "alice@example.com",
                        "invite_epoch": self.inv_a["invite_epoch"]}},
            {"source": {"tenant_id": "t-b", "member_id": self.bob["member_id"],
                        "member_epoch": 1},
             "target": {"tenant_id": "t-a", "email": "bob@example.com",
                        "invite_epoch": self.inv_b["invite_epoch"]}},
        ]

    # -- the same swap from two processes ----------------------------------

    def test_two_processes_run_the_same_swap_once(self) -> None:
        params = {"request_tenant": "t-a", "reason": "swap", "moves": self.swap_moves()}
        base = {"db": self.db, "op": "migrate.commit", "params": params,
                "clock_ms": T0_MS, "request_id": "swap-shared"}
        first = dict(base)
        first["output"] = os.path.join(self.workdir, "one.json")
        second = dict(base)
        second["output"] = os.path.join(self.workdir, "two.json")
        code_one, out_one, err_one = self.run_worker(first)
        code_two, out_two, err_two = self.run_worker(second)
        self.assertEqual(code_one, 0, err_one)
        self.assertEqual(code_two, 0, err_two)
        one = read_receipt(first["output"])
        two = read_receipt(second["output"])
        self.assertEqual(one, two)
        self.assertTrue(one["ok"])
        state = snapshot_of(self.db)
        self.assertEqual(
            sum(1 for a in state["audits"] if a["action"] == "migration.committed"), 1
        )
        active = sorted((m["tenant_id"], m["email"]) for m in state["members"]
                        if m["status"] == "active")
        self.assertEqual(active, [("t-a", "bob@example.com"), ("t-b", "alice@example.com")])

    # -- preflight, then somebody else moves the world ---------------------

    def test_invite_issued_by_another_process_between_preflight_and_commit_wins(self) -> None:
        """A pessimistic preflight would commit the stale invite; we must refuse."""
        stale_invite = self.inv_a
        params = {"request_tenant": "t-a", "moves": [{
            "source": {"tenant_id": "t-a", "member_id": self.alice["member_id"],
                       "member_epoch": 1},
            "target": {"tenant_id": "t-b", "email": "alice@example.com",
                       "invite_epoch": stale_invite["invite_epoch"]},
        }]}
        job = {
            "db": self.db, "op": "migrate.commit", "params": params,
            "clock_ms": T0_MS, "request_id": "stale-invite-op",
            "sync_dir": self.gate.path, "sync_points": ["after_preflight"],
            "output": os.path.join(self.workdir, "stale.json"),
        }
        process = start_worker(job, self.workdir)
        self.children.append(process)
        try:
            self.gate.wait_reached("after_preflight")
            # Another process re-issues the invitation while the first is parked.
            other = make_directory(self.db)
            try:
                reissued = call(other, "invite.issue",
                                {"tenant_id": "t-b", "email": "alice@example.com",
                                 "ttl_ms": HOUR}, "reissue")
            finally:
                other.close()
            self.assertEqual(reissued["data"]["invite_epoch"], 2)
            self.gate.release("after_preflight")
        except BaseException:
            # If the interleaving could not be staged, make sure the parked child
            # does not outlive the test; it is the only process we ever touch.
            if process.poll() is None:
                process.kill()
            raise
        code, out, err = finish_worker(process)
        self.assertEqual(code, 0, err)
        receipt = read_receipt(job["output"])
        self.assertEqual(receipt["error"]["code"], "INVITE_NOT_PENDING")
        state = snapshot_of(self.db)
        self.assertEqual(
            sorted((m["tenant_id"], m["email"]) for m in state["members"]
                   if m["status"] == "active"),
            [("t-a", "alice@example.com"), ("t-b", "bob@example.com")],
        )

    def test_member_revoked_by_another_process_between_preflight_and_commit(self) -> None:
        params = {"request_tenant": "t-a", "moves": [{
            "source": {"tenant_id": "t-a", "member_id": self.alice["member_id"],
                       "member_epoch": 1},
            "target": {"tenant_id": "t-b", "email": "alice@example.com",
                       "invite_epoch": self.inv_a["invite_epoch"]},
        }]}
        job = {
            "db": self.db, "op": "migrate.commit", "params": params,
            "clock_ms": T0_MS, "request_id": "stale-member-op",
            "sync_dir": self.gate.path, "sync_points": ["after_preflight"],
            "output": os.path.join(self.workdir, "stale-member.json"),
        }
        process = start_worker(job, self.workdir)
        self.children.append(process)
        self.gate.wait_reached("after_preflight")
        other = make_directory(self.db)
        try:
            revoked = call(other, "member.revoke",
                           {"tenant_id": "t-a", "member_id": self.alice["member_id"],
                            "member_epoch": 1}, "revoke")
        finally:
            other.close()
        self.assertTrue(revoked["ok"])
        self.gate.release("after_preflight")
        code, out, err = finish_worker(process)
        self.assertEqual(code, 0, err)
        receipt = read_receipt(job["output"])
        self.assertEqual(receipt["error"]["code"], "MEMBER_NOT_ACTIVE")
        state = snapshot_of(self.db)
        self.assertEqual(state["invites"][0]["status"], "pending")

    # -- overlapping batches ------------------------------------------------

    def test_overlapping_batches_serialise_and_the_loser_rolls_back_whole(self) -> None:
        alice = self.alice
        bob = self.bob
        setup = make_directory(self.db)
        carol = call(setup, "member.add",
                     {"tenant_id": "t-c", "email": "carol@example.com"}, "add-c")["data"]
        inv_alice_b = self.inv_a
        inv_bob_a = self.inv_b
        inv_carol_a = call(setup, "invite.issue",
                           {"tenant_id": "t-a", "email": "carol@example.com", "ttl_ms": HOUR},
                           "inv-carol")["data"]
        setup.close()
        batch_one = {"request_tenant": "t-a", "moves": self.swap_moves()}
        batch_two = {"request_tenant": "t-a", "moves": [
            {"source": {"tenant_id": "t-a", "member_id": alice["member_id"],
                        "member_epoch": 1},
             "target": {"tenant_id": "t-b", "email": "alice@example.com",
                        "invite_epoch": inv_alice_b["invite_epoch"]}},
            {"source": {"tenant_id": "t-c", "member_id": carol["member_id"],
                        "member_epoch": 1},
             "target": {"tenant_id": "t-a", "email": "carol@example.com",
                        "invite_epoch": inv_carol_a["invite_epoch"]}},
        ]}
        job_one = {
            "db": self.db, "op": "migrate.commit", "params": batch_one,
            "clock_ms": T0_MS, "request_id": "overlap-one",
            "sync_dir": self.gate.path, "sync_points": ["after_preflight"],
            "output": os.path.join(self.workdir, "overlap-one.json"),
        }
        process = start_worker(job_one, self.workdir)
        self.children.append(process)
        try:
            self.gate.wait_reached("after_preflight")
            # The second batch commits while the first is still parked outside
            # its write transaction: it must lose cleanly later.
            self.run_worker({
                "db": self.db, "op": "migrate.commit", "params": batch_two,
                "clock_ms": T0_MS, "request_id": "overlap-two",
                "output": os.path.join(self.workdir, "overlap-two.json"),
            })
        finally:
            self.gate.release("after_preflight")
        code, out, err = finish_worker(process)
        self.assertEqual(code, 0, err)
        one = read_receipt(job_one["output"])
        two = read_receipt(os.path.join(self.workdir, "overlap-two.json"))
        self.assertTrue(two["ok"], two)
        self.assertEqual(one["error"]["code"], "MEMBER_NOT_ACTIVE")
        state = snapshot_of(self.db)
        active = sorted((m["tenant_id"], m["email"]) for m in state["members"]
                        if m["status"] == "active")
        # Batch two won: alice moved to t-b, carol moved to t-a, bob (never part
        # of the winning batch) is untouched in t-b.  Batch one would have moved
        # bob to t-a; it lost and left no trace at all.
        self.assertEqual(active, [("t-a", "carol@example.com"),
                                  ("t-b", "alice@example.com"),
                                  ("t-b", "bob@example.com")])
        self.assertEqual(
            sorted(m["email"] for m in state["members"]
                   if m["tenant_id"] == "t-a" and m["status"] == "active"),
            ["carol@example.com"],
        )
        by_binding = {(i["tenant_id"], i["email"]): i["status"] for i in state["invites"]}
        self.assertEqual(by_binding[("t-b", "alice@example.com")], "consumed")
        self.assertEqual(by_binding[("t-a", "carol@example.com")], "consumed")
        self.assertEqual(by_binding[("t-a", "bob@example.com")], "pending")

    # -- one request id, two processes --------------------------------------

    def test_concurrent_duplicate_request_ids_share_one_receipt(self) -> None:
        params = {"tenant_id": "t-c", "email": "dave@example.com"}
        jobs = []
        for name in ("p1", "p2"):
            job = {
                "db": self.db, "op": "member.add", "params": params,
                "clock_ms": T0_MS, "request_id": "dup-across-processes",
                "output": os.path.join(self.workdir, f"{name}.json"),
            }
            jobs.append(job)
            self.run_worker(job)
        receipts = [read_receipt(j["output"])
                    for j in jobs]
        self.assertEqual(receipts[0], receipts[1])
        self.assertTrue(receipts[0]["ok"])
        state = snapshot_of(self.db)
        self.assertEqual(
            len([m for m in state["members"] if m["email"] == "dave@example.com"]), 1
        )

    def test_concurrent_duplicate_ids_with_different_params_one_wins(self) -> None:
        jobs = [
            {"db": self.db, "op": "member.add",
             "params": {"tenant_id": "t-c", "email": "first@example.com"},
             "clock_ms": T0_MS, "request_id": "collide",
             "output": os.path.join(self.workdir, "c1.json")},
            {"db": self.db, "op": "member.add",
             "params": {"tenant_id": "t-c", "email": "second@example.com"},
             "clock_ms": T0_MS, "request_id": "collide",
             "output": os.path.join(self.workdir, "c2.json")},
        ]
        for job in jobs:
            self.run_worker(job)
        receipts = [read_receipt(j["output"])
                    for j in jobs]
        winners = [r for r in receipts if r["ok"]]
        conflicts = [r for r in receipts if not r["ok"]]
        self.assertEqual(len(winners), 1, receipts)
        self.assertEqual(len(conflicts), 1, receipts)
        self.assertEqual(conflicts[0]["error"]["code"], "REQUEST_ID_CONFLICT")
        state = snapshot_of(self.db)
        self.assertEqual(
            len([m for m in state["members"] if m["tenant_id"] == "t-c"]), 1
        )


if __name__ == "__main__":
    unittest.main()
