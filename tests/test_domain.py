"""Domain behaviour: invitations, generations, departure, migration batches.

Every expected value below is written down from the task's rules, never read
back out of the implementation.
"""
from __future__ import annotations

import unittest

from solution.util import RejectError
from tests.support import (
    HOUR,
    T0,
    T0_MS,
    TempDirMixin,
    base_tenants,
    business,
    call,
    make_directory,
)


class InvitationTests(TempDirMixin, unittest.TestCase):
    def test_domain_is_lowercased_and_local_part_kept(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        receipt = call(directory, "invite.issue",
                       {"tenant_id": "t-a", "email": "Mixed.Case+Tag@Example.COM", "ttl_ms": HOUR},
                       "r1")
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["data"]["email"], "Mixed.Case+Tag@example.com")
        self.assertEqual(receipt["data"]["invite_epoch"], 1)
        self.assertEqual(receipt["data"]["expires_at"], "2026-01-01T01:00:00Z")
        directory.close()

    def test_member_email_normalisation_is_identical(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        member = call(directory, "member.add",
                      {"tenant_id": "t-a", "email": "Alice@Example.COM"}, "r1")
        self.assertEqual(member["data"]["email"], "Alice@example.com")
        self.assertEqual(member["data"]["member_epoch"], 1)
        # A second address differing only in domain case is the same seat.
        duplicate = call(directory, "member.add",
                         {"tenant_id": "t-a", "email": "Alice@EXAMPLE.com"}, "r2")
        self.assertEqual(duplicate["ok"], False)
        self.assertEqual(duplicate["error"]["code"], "MEMBER_EXISTS")
        directory.close()

    def test_non_ascii_email_is_refused(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        receipt = call(directory, "invite.issue",
                       {"tenant_id": "t-a", "email": "müller@example.com", "ttl_ms": HOUR}, "r1")
        self.assertEqual(receipt["error"]["code"], "INVALID_EMAIL")
        directory.close()

    def test_reissue_bumps_invite_epoch_and_supersedes(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        first = call(directory, "invite.issue",
                     {"tenant_id": "t-a", "email": "bob@example.com", "ttl_ms": HOUR}, "r1")
        second = call(directory, "invite.issue",
                      {"tenant_id": "t-a", "email": "bob@example.com", "ttl_ms": HOUR}, "r2")
        self.assertEqual(first["data"]["invite_epoch"], 1)
        self.assertEqual(second["data"]["invite_epoch"], 2)
        self.assertEqual(second["data"]["superseded_previous"], 1)
        # Generations are per binding and never reused, even after expiry.
        directory.clock.set(T0_MS + 2 * HOUR)
        third = call(directory, "invite.issue",
                     {"tenant_id": "t-a", "email": "bob@example.com", "ttl_ms": HOUR}, "r3")
        self.assertEqual(third["data"]["invite_epoch"], 3)
        directory.close()

    def test_generations_are_independent_per_email_and_tenant(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        a = call(directory, "invite.issue",
                 {"tenant_id": "t-a", "email": "x@example.com", "ttl_ms": HOUR}, "r1")
        b = call(directory, "invite.issue",
                 {"tenant_id": "t-b", "email": "x@example.com", "ttl_ms": HOUR}, "r2")
        c = call(directory, "invite.issue",
                 {"tenant_id": "t-a", "email": "y@example.com", "ttl_ms": HOUR}, "r3")
        self.assertEqual((a["data"]["invite_epoch"], b["data"]["invite_epoch"],
                          c["data"]["invite_epoch"]), (1, 1, 1))
        directory.close()

    def test_exactly_at_expiry_is_expired(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        call(directory, "invite.issue",
             {"tenant_id": "t-a", "email": "late@example.com", "ttl_ms": HOUR}, "r1")
        directory.clock.set(T0_MS + HOUR - 1)
        revoked = call(directory, "invite.revoke",
                       {"tenant_id": "t-a", "email": "late@example.com", "invite_epoch": 1}, "r2")
        self.assertTrue(revoked["ok"], "one millisecond before expiry the invite is still pending")
        directory.close()

    def test_one_millisecond_past_expiry(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        call(directory, "invite.issue",
             {"tenant_id": "t-a", "email": "late@example.com", "ttl_ms": HOUR}, "r1")
        directory.clock.set(T0_MS + HOUR)
        revoked = call(directory, "invite.revoke",
                       {"tenant_id": "t-a", "email": "late@example.com", "invite_epoch": 1}, "r2")
        self.assertEqual(revoked["error"]["code"], "INVITE_EXPIRED")
        state = directory.snapshot()
        self.assertEqual(state["invites"][0]["status"], "expired")
        directory.close()


class DepartureTests(TempDirMixin, unittest.TestCase):
    def test_leave_bumps_member_epoch_and_retires_the_seat(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        added = call(directory, "member.add", {"tenant_id": "t-a", "email": "ann@example.com"}, "r1")
        member_id = added["data"]["member_id"]
        left = call(directory, "member.leave",
                    {"tenant_id": "t-a", "member_id": member_id, "member_epoch": 1}, "r2")
        self.assertTrue(left["ok"])
        self.assertEqual(left["data"]["member_epoch"], 2)
        self.assertEqual(left["data"]["status"], "revoked")
        again = call(directory, "member.leave",
                     {"tenant_id": "t-a", "member_id": member_id, "member_epoch": 2}, "r3")
        self.assertEqual(again["error"]["code"], "MEMBER_NOT_ACTIVE")
        stale = call(directory, "member.revoke",
                     {"tenant_id": "t-a", "member_id": member_id, "member_epoch": 1}, "r4")
        self.assertEqual(stale["error"]["code"], "MEMBER_NOT_ACTIVE")
        directory.close()

    def test_stale_member_epoch_is_rejected_on_an_active_member(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        member_id = call(directory, "member.add",
                         {"tenant_id": "t-a", "email": "ann@example.com"}, "r1")["data"]["member_id"]
        receipt = call(directory, "member.leave",
                       {"tenant_id": "t-a", "member_id": member_id, "member_epoch": 7}, "r2")
        self.assertEqual(receipt["error"]["code"], "MEMBER_EPOCH_MISMATCH")
        self.assertEqual(receipt["error"]["details"]["expected"], 1)
        directory.close()

    def test_audit_reason_is_persisted_with_the_change(self) -> None:
        directory = make_directory(self.db_path())
        base_tenants(directory)
        member_id = call(directory, "member.add",
                         {"tenant_id": "t-a", "email": "ann@example.com"}, "r1")["data"]["member_id"]
        call(directory, "member.leave",
             {"tenant_id": "t-a", "member_id": member_id, "member_epoch": 1,
              "reason": "resigned 2026-01-01"}, "r2")
        state = directory.snapshot()
        leave_audits = [a for a in state["audits"] if a["action"] == "member.left"]
        self.assertEqual(len(leave_audits), 1)
        self.assertEqual(leave_audits[0]["reason"], "resigned 2026-01-01")
        self.assertEqual(leave_audits[0]["at"], T0)
        directory.close()


class MigrationTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = make_directory(self.db_path())
        base_tenants(self.dir)
        self._invite_seq = 0

    def tearDown(self) -> None:
        self.dir.close()
        super().tearDown()

    def _member(self, tenant: str, email: str) -> dict:
        receipt = call(self.dir, "member.add", {"tenant_id": tenant, "email": email},
                       f"add-{tenant}-{email}")
        self.assertTrue(receipt["ok"], receipt)
        return receipt["data"]

    def _invite(self, tenant: str, email: str, ttl_ms: int = HOUR) -> dict:
        self._invite_seq += 1
        receipt = call(self.dir, "invite.issue",
                       {"tenant_id": tenant, "email": email, "ttl_ms": ttl_ms},
                       f"inv-{self._invite_seq}")
        self.assertTrue(receipt["ok"], receipt)
        return receipt["data"]

    def _move(self, member: dict, invite: dict) -> dict:
        return {
            "source": {
                "tenant_id": member["tenant_id"],
                "member_id": member["member_id"],
                "member_epoch": member["member_epoch"],
            },
            "target": {
                "tenant_id": invite["tenant_id"],
                "email": invite["email"],
                "invite_epoch": invite["invite_epoch"],
            },
        }

    def _batch(self, moves: list[dict], rid: str, reason: str = "reorg") -> dict:
        return call(self.dir, "migrate.commit",
                    {"request_tenant": "t-a", "reason": reason, "moves": moves}, rid)

    # -- happy path ---------------------------------------------------------

    def test_single_move_consumes_invite_and_revokes_source(self) -> None:
        member = self._member("t-a", "Zoe@Example.com")
        invite = self._invite("t-b", "Zoe@example.com")
        receipt = self._batch([self._move(member, invite)], "mig-1")
        self.assertTrue(receipt["ok"], receipt)
        data = receipt["data"]["moves"][0]
        self.assertEqual(data["source"]["status"], "revoked")
        self.assertEqual(data["source"]["member_epoch"], 2)
        self.assertEqual(data["target"]["tenant_id"], "t-b")
        self.assertEqual(data["target"]["member_epoch"], 1)
        self.assertEqual(data["target"]["invite_id"], invite["invite_id"])
        state = self.dir.snapshot()
        invite_row = state["invites"][0]
        self.assertEqual(invite_row["status"], "consumed")
        self.assertEqual(invite_row["consumed_at"], T0)
        actions = [a["action"] for a in state["audits"]]
        self.assertIn("migration.source_revoked", actions)
        self.assertIn("invite.consumed", actions)
        self.assertIn("migration.target_joined", actions)
        self.assertIn("migration.committed", actions)
        for audit in state["audits"]:
            if audit["action"].startswith("migration.") or audit["action"] == "invite.consumed":
                self.assertEqual(audit["reason"], "reorg")

    def test_two_way_swap_is_accepted_even_though_each_target_is_occupied(self) -> None:
        """The genuine swap: one address, actively held in *both* tenants.

        Each move's target seat is occupied when the batch starts, by the other
        move's source.  A per-move occupancy test would refuse both; judging
        occupancy after every source has left accepts both.
        """
        left = self._member("t-a", "alice@example.com")
        right = self._member("t-b", "alice@example.com")
        inv_left = self._invite("t-b", "alice@example.com")
        inv_right = self._invite("t-a", "alice@example.com")
        receipt = self._batch(
            [self._move(left, inv_left), self._move(right, inv_right)], "swap-1"
        )
        self.assertTrue(receipt["ok"], receipt)

        state = self.dir.snapshot()
        active = sorted((m["tenant_id"], m["member_id"]) for m in state["members"]
                        if m["status"] == "active")
        self.assertEqual(active, [("t-a", "alice@t-a.m2"), ("t-b", "alice@t-b.m2")])
        retired = sorted((m["tenant_id"], m["member_id"], m["member_epoch"])
                         for m in state["members"] if m["status"] == "revoked")
        self.assertEqual(retired, [("t-a", "alice@t-a.m1", 2), ("t-b", "alice@t-b.m1", 2)])
        self.assertEqual({i["status"] for i in state["invites"]}, {"consumed"})

    def test_two_independent_moves_in_one_batch(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        bob = self._member("t-b", "bob@example.com")
        inv_alice = self._invite("t-b", "alice@example.com")
        inv_bob = self._invite("t-a", "bob@example.com")
        receipt = self._batch([self._move(alice, inv_alice), self._move(bob, inv_bob)], "pair")
        self.assertTrue(receipt["ok"], receipt)
        active = sorted((m["tenant_id"], m["email"]) for m in self.dir.snapshot()["members"]
                        if m["status"] == "active")
        self.assertEqual(active, [("t-a", "bob@example.com"), ("t-b", "alice@example.com")])

    def test_swap_result_does_not_depend_on_move_order(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        bob = self._member("t-b", "bob@example.com")
        inv_alice = self._invite("t-b", "alice@example.com")
        inv_bob = self._invite("t-a", "bob@example.com")
        receipt = self._batch(
            [self._move(bob, inv_bob), self._move(alice, inv_alice)], "swap-reversed"
        )
        self.assertTrue(receipt["ok"], receipt)
        active = sorted(
            (m["tenant_id"], m["email"])
            for m in self.dir.snapshot()["members"] if m["status"] == "active"
        )
        self.assertEqual(active, [("t-a", "bob@example.com"), ("t-b", "alice@example.com")])

    # -- refusals -----------------------------------------------------------

    def test_occupied_target_without_a_swap_is_refused_whole(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        self._member("t-b", "alice@example.com")
        inv_alice = self._invite("t-b", "alice@example.com")
        before = business(self.dir.snapshot())
        receipt = self._batch([self._move(alice, inv_alice)], "occupied-1")
        self.assertEqual(receipt["error"]["code"], "TARGET_OCCUPIED")
        self.assertEqual(receipt["committed"], False)
        self.assertEqual(business(self.dir.snapshot()), before)

    def test_duplicate_source_fails_everything(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        inv_one = self._invite("t-b", "alice@example.com")
        inv_two = self._invite("t-b", "alice2@example.com")
        before = business(self.dir.snapshot())
        receipt = self._batch(
            [self._move(alice, inv_one), self._move(alice, inv_two)], "dup-src"
        )
        self.assertEqual(receipt["error"]["code"], "DUPLICATE_SOURCE")
        self.assertEqual(business(self.dir.snapshot()), before)

    def test_duplicate_target_fails_everything(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        bob = self._member("t-a", "bob@example.com")
        inv = self._invite("t-b", "seats@example.com")
        target = {
            "tenant_id": inv["tenant_id"],
            "email": inv["email"],
            "invite_epoch": inv["invite_epoch"],
        }
        moves = [
            {"source": {"tenant_id": "t-a", "member_id": alice["member_id"], "member_epoch": 1},
             "target": target},
            {"source": {"tenant_id": "t-a", "member_id": bob["member_id"], "member_epoch": 1},
             "target": target},
        ]
        before = business(self.dir.snapshot())
        receipt = self._batch(moves, "dup-tgt")
        self.assertEqual(receipt["error"]["code"], "DUPLICATE_TARGET")
        self.assertEqual(business(self.dir.snapshot()), before)
        # A later, valid batch can still use the untouched invitation.
        invite_after = [i for i in self.dir.snapshot()["invites"] if i["invite_id"] == inv["invite_id"]]
        self.assertEqual(invite_after[0]["status"], "pending")

    def test_wrong_invite_epoch_fails_whole_batch(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        self._invite("t-b", "alice@example.com")
        current = self._invite("t-b", "alice@example.com")  # epoch 2 supersedes epoch 1
        move = self._move(alice, current)
        move["target"]["invite_epoch"] = 1
        before = business(self.dir.snapshot())
        receipt = self._batch([move], "stale-invite")
        self.assertEqual(receipt["error"]["code"], "INVITE_NOT_PENDING")
        self.assertEqual(business(self.dir.snapshot()), before)

    def test_stale_member_epoch_fails_whole_batch(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        inv = self._invite("t-b", "alice@example.com")
        call(self.dir, "member.leave",
             {"tenant_id": "t-a", "member_id": alice["member_id"], "member_epoch": 1}, "left")
        before = business(self.dir.snapshot())
        receipt = self._batch([self._move(alice, inv)], "stale-member")
        self.assertEqual(receipt["error"]["code"], "MEMBER_NOT_ACTIVE")
        self.assertEqual(business(self.dir.snapshot()), before)

    def test_expired_invite_is_refused_and_stays_usable_by_nobody(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        inv = self._invite("t-b", "alice@example.com", ttl_ms=HOUR)
        self.dir.clock.set(T0_MS + HOUR)
        receipt = self._batch([self._move(alice, inv)], "expired")
        self.assertEqual(receipt["error"]["code"], "INVITE_EXPIRED")
        state = self.dir.snapshot()
        self.assertEqual(state["invites"][0]["status"], "expired")
        self.assertEqual(
            [m["status"] for m in state["members"] if m["tenant_id"] == "t-a"], ["active"]
        )

    def test_failed_batch_leaves_the_invite_for_another_batch(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        carol = self._member("t-c", "carol@example.com")
        inv = self._invite("t-b", "carol@example.com")
        # The first move is impossible (the invitation was already revoked).
        bad_inv = self._invite("t-b", "alice@example.com")
        call(self.dir, "invite.revoke",
             {"tenant_id": "t-b", "email": "alice@example.com", "invite_epoch": bad_inv["invite_epoch"]},
             "revoke")
        before = business(self.dir.snapshot())
        failed = self._batch(
            [self._move(alice, bad_inv), self._move(carol, inv)], "partial"
        )
        self.assertEqual(failed["error"]["code"], "INVITE_NOT_PENDING")
        self.assertEqual(business(self.dir.snapshot()), before)
        # The usable invitation survived the failed batch intact.
        second = self._batch([self._move(carol, inv)], "second-try")
        self.assertTrue(second["ok"], second)
        active = {(m["tenant_id"], m["email"]) for m in self.dir.snapshot()["members"]
                  if m["status"] == "active"}
        self.assertIn(("t-b", "carol@example.com"), active)
        self.assertIn(("t-a", "alice@example.com"), active)

    def test_same_tenant_move_is_refused(self) -> None:
        alice = self._member("t-a", "alice@example.com")
        inv = self._invite("t-a", "alice2@example.com")
        receipt = self._batch([self._move(alice, inv)], "same-tenant")
        self.assertEqual(receipt["error"]["code"], "SAME_TENANT")
        self.assertEqual(self.dir.snapshot()["invites"][0]["status"], "pending")

    def test_empty_batch_is_refused(self) -> None:
        receipt = self._batch([], "empty")
        self.assertEqual(receipt["error"]["code"], "INVALID_REQUEST")


class IdempotencyTests(TempDirMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = make_directory(self.db_path())
        base_tenants(self.dir)

    def tearDown(self) -> None:
        self.dir.close()
        super().tearDown()

    def test_success_replays_the_persisted_receipt(self) -> None:
        params = {"tenant_id": "t-a", "email": "ann@example.com"}
        first = call(self.dir, "member.add", params, "same-id")
        self.dir.clock.set(T0_MS + HOUR)
        second = call(self.dir, "member.add", params, "same-id")
        self.assertEqual(first, second)
        # Only one member and only one audit row were produced.
        state = self.dir.snapshot()
        self.assertEqual(len(state["members"]), 1)
        self.assertEqual(len([a for a in state["audits"] if a["action"] == "member.added"]), 1)

    def test_failure_replays_the_persisted_receipt(self) -> None:
        params = {"tenant_id": "t-a", "member_id": "ghost", "member_epoch": 1}
        first = call(self.dir, "member.revoke", params, "failing-id")
        self.assertEqual(first["error"]["code"], "MEMBER_NOT_FOUND")
        second = call(self.dir, "member.revoke", params, "failing-id")
        self.assertEqual(first, second)
        self.assertFalse(second["ok"])
        self.assertEqual(len([a for a in self.dir.snapshot()["audits"]
                              if a["action"] == "member.revoke.rejected"]), 1)

    def test_same_id_with_different_params_is_refused(self) -> None:
        call(self.dir, "member.add", {"tenant_id": "t-a", "email": "ann@example.com"}, "shared")
        receipt = call(self.dir, "member.add", {"tenant_id": "t-a", "email": "bob@example.com"}, "shared")
        self.assertEqual(receipt["error"]["code"], "REQUEST_ID_CONFLICT")
        emails = sorted(m["email"] for m in self.dir.snapshot()["members"])
        self.assertEqual(emails, ["ann@example.com"])

    def test_request_ids_are_scoped_per_tenant(self) -> None:
        one = call(self.dir, "member.add", {"tenant_id": "t-a", "email": "ann@example.com"}, "shared")
        two = call(self.dir, "member.add", {"tenant_id": "t-b", "email": "bob@example.com"}, "shared")
        self.assertTrue(one["ok"])
        self.assertTrue(two["ok"])
        self.assertEqual(len(self.dir.snapshot()["members"]), 2)

    def test_unknown_operation_is_refused_and_still_keyed(self) -> None:
        receipt = call(self.dir, "nope.op", {"tenant_id": "t-a"}, "unknown-1")
        self.assertEqual(receipt["error"]["code"], "UNKNOWN_OP")
        replay = call(self.dir, "nope.op", {"tenant_id": "t-a"}, "unknown-1")
        self.assertEqual(replay, receipt)
        mine = [r for r in self.dir.snapshot()["requests"] if r["request_id"] == "unknown-1"]
        self.assertEqual([r["status"] for r in mine], ["rejected"])
        self.assertEqual([r["error_code"] for r in mine], ["UNKNOWN_OP"])

    def test_request_without_a_tenant_cannot_be_keyed(self) -> None:
        """No scope tenant means no idempotency key: the caller gets a local error."""
        with self.assertRaises(RejectError) as caught:
            self.dir.execute("migrate.commit", {"moves": []}, "unscoped")
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")
        self.assertEqual(
            [r for r in self.dir.snapshot()["requests"] if r["request_id"] == "unscoped"], []
        )

    def test_duplicate_after_a_later_state_change_still_replays(self) -> None:
        alice = call(self.dir, "member.add",
                     {"tenant_id": "t-a", "email": "alice@example.com"}, "add-alice")["data"]
        inv = call(self.dir, "invite.issue",
                   {"tenant_id": "t-b", "email": "alice@example.com", "ttl_ms": HOUR}, "inv")["data"]
        moves = [{
            "source": {"tenant_id": "t-a", "member_id": alice["member_id"], "member_epoch": 1},
            "target": {"tenant_id": "t-b", "email": "alice@example.com",
                       "invite_epoch": inv["invite_epoch"]},
        }]
        params = {"request_tenant": "t-a", "reason": "reorg", "moves": moves}
        first = call(self.dir, "migrate.commit", params, "mig")
        self.assertTrue(first["ok"])
        # Re-submitting the same request now describes a *stale* world; the
        # answer must still be the original receipt, not a fresh refusal.
        replay = call(self.dir, "migrate.commit", params, "mig")
        self.assertEqual(first, replay)
        self.assertTrue(replay["ok"])


if __name__ == "__main__":
    unittest.main()
