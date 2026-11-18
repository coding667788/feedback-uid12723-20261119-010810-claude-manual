"""The member directory: invitations, departures and batch migrations.

All business mutations run inside one ``BEGIN IMMEDIATE`` SQLite transaction that
(a) re-reads every version referenced by the request, (b) stages the effects,
(c) stores the request receipt and its audit rows, and (d) commits exactly once.
A receipt is therefore either fully durable together with its effects, or absent
together with them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import db
from .util import (
    RejectError,
    iso,
    normalize_email,
    sha256,
    stable,
)

# --------------------------------------------------------------------------- #
# Small infrastructure
# --------------------------------------------------------------------------- #


class Clock:
    def now_ms(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError


class SystemClock(Clock):
    def now_ms(self) -> int:
        return int(time.time() * 1000)


class FixedClock(Clock):
    """A virtual clock used by the replayable case files and the tests."""

    def __init__(self, at_ms: int) -> None:
        self.at_ms = at_ms

    def now_ms(self) -> int:
        return self.at_ms

    def set(self, at_ms: int) -> None:
        if at_ms < self.at_ms:
            raise ValueError("the virtual clock cannot move backwards")
        self.at_ms = at_ms


class Hooks:
    """Test-only synchronisation and crash points.

    ``sync_dir`` turns the named checkpoints into rendezvous points: the process
    writes ``<name>.reached`` and then blocks until ``<name>.go`` exists.  This
    lets a test force one process to pause *after* its read-only preflight (so a
    second process can slip a change in) or in the middle of its write
    transaction, with the ordering fixed rather than hoped for.

    ``crash_at`` makes the current process die with ``os._exit(90)`` the moment
    that checkpoint is reached: a real termination with no unwinding, no
    ``finally``, no closing hook.
    """

    POINTS = ("after_preflight", "during_effects", "after_commit")

    def __init__(
        self,
        crash_at: str | None = None,
        sync_dir: str | None = None,
        sync_points: Iterable[str] | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        if crash_at is not None and crash_at not in self.POINTS:
            raise ValueError(f"unknown crash point {crash_at!r}")
        self.crash_at = crash_at
        self.sync_dir = sync_dir
        self.sync_points = set(sync_points or self.POINTS) if sync_dir else set()
        self.timeout_s = timeout_s
        self.seen: list[str] = []

    def reach(self, name: str) -> None:
        if name not in self.POINTS:
            raise ValueError(f"unknown checkpoint {name!r}")
        self.seen.append(name)
        if self.sync_dir and name in self.sync_points:
            self._rendezvous(name)
        if self.crash_at == name:
            os._exit(90)

    def _rendezvous(self, name: str) -> None:
        arrived = os.path.join(self.sync_dir, f"{name}.reached")
        with open(arrived, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        released = os.path.join(self.sync_dir, f"{name}.go")
        deadline = time.monotonic() + self.timeout_s
        while not os.path.exists(released):
            if time.monotonic() > deadline:
                raise TimeoutError(f"sync point {name!r} was never released")
            time.sleep(0.005)


@dataclass
class ExecResult:
    receipt: dict[str, Any]
    replayed: bool


# --------------------------------------------------------------------------- #
# Parameter helpers
# --------------------------------------------------------------------------- #

_ACTIVE = "active"
_REVOKED = "revoked"
_PENDING = "pending"
_CONSUMED = "consumed"
_SUPERSEDED = "superseded"


def _str(params: dict[str, Any], key: str, *, code: str = "INVALID_REQUEST") -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RejectError(code, f"{key!r} must be a non-empty string", {"field": key})
    return value


def _int(params: dict[str, Any], key: str, *, code: str = "INVALID_REQUEST") -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RejectError(code, f"{key!r} must be an integer", {"field": key})
    return value


def _obj(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RejectError("INVALID_REQUEST", f"{what} must be an object", {"field": what})
    return value


def _list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise RejectError("INVALID_REQUEST", f"{what} must be an array", {"field": what})
    return value


@dataclass
class MovePlan:
    src_tenant: str
    src_member: str
    src_epoch: int
    src_email: str = ""
    tgt_tenant: str = ""
    tgt_email: str = ""
    tgt_epoch: int = 0
    invite_id: int = 0
    invite_expires_ms: int = 0
    new_member_id: str = ""
    # populated at preflight / resolve time
    src_row: sqlite3.Row | None = None
    invite_row: sqlite3.Row | None = None


@dataclass
class MigrationPlan:
    moves: list[MovePlan] = field(default_factory=list)
    reason: str | None = None
    scope_tenant: str = ""


# --------------------------------------------------------------------------- #
# Directory
# --------------------------------------------------------------------------- #


class Directory:
    def __init__(
        self,
        db_path: str,
        clock: Clock | None = None,
        hooks: Hooks | None = None,
    ) -> None:
        self.db_path = db_path
        self.clock = clock or SystemClock()
        self.hooks = hooks or Hooks()
        self.conn = db.connect(db_path)
        db.initialize(self.conn)

    # -- lifecycle ---------------------------------------------------------- #

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Directory":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- public API --------------------------------------------------------- #

    def execute(self, op: str, params: dict[str, Any], request_id: str | None = None) -> ExecResult:
        """Execute a mutating request under a per-tenant idempotency key."""
        if not isinstance(params, dict):
            raise RejectError("INVALID_REQUEST", "params must be an object")
        canonical = self._canonical(op, params)
        if request_id is None:
            raise RejectError("INVALID_REQUEST", "mutating requests require a request_id")
        if not isinstance(request_id, str) or not request_id:
            raise RejectError("INVALID_REQUEST", "request_id must be a non-empty string")
        scope = self._scope_tenant(op, params)
        params_key = sha256({"op": op, "params": canonical})

        # Best-effort read-only preflight.  Its verdict is deliberately *not*
        # trusted and never decides the outcome: another process may change the
        # world between here and the commit, so every version referenced by the
        # request is re-read inside the write transaction below.  The pass exists
        # so the failure mode (and the sync point) can be exercised.
        try:
            self._preflight(op, params)
        except RejectError:
            pass
        self.hooks.reach("after_preflight")

        with db.write_txn(self.conn) as conn:
            stored = db.one(
                conn,
                "SELECT * FROM requests WHERE tenant_id = ? AND request_id = ?",
                (scope, request_id),
            )
            if stored is not None:
                if stored["params_key"] != params_key:
                    # Never overwrite the first receipt: the duplicate is refused
                    # without touching anything.
                    return ExecResult(self._make_receipt(
                        ok=False,
                        op=op,
                        scope=scope,
                        request_id=request_id,
                        at_ms=self.clock.now_ms(),
                        error=RejectError(
                            "REQUEST_ID_CONFLICT",
                            "request_id was already used with different parameters",
                            {"request_id": request_id, "tenant_id": scope, "original_kind": stored["kind"]},
                        ),
                    ), False)
                return ExecResult(json.loads(stored["response_json"]), True)

            now = self.clock.now_ms()
            error: RejectError | None = None
            data: dict[str, Any] | None = None
            # Time-driven expiry is not a request effect: materialise it before
            # the business savepoint so a refused request does not resurrect an
            # invitation that has genuinely lapsed.
            for tenant in self._sweep_tenants(op, params):
                self._expire_due(conn, tenant, now)
            # A savepoint separates the business effects from the receipt: a
            # refused request rolls its business changes back to here, while the
            # receipt and its audit row below are still committed.
            conn.execute("SAVEPOINT business")
            try:
                self._check_shape(op, params)
                data = self._apply(conn, op, params, request_id, now)
            except RejectError as exc:
                error = exc
                conn.execute("ROLLBACK TO SAVEPOINT business")
            conn.execute("RELEASE SAVEPOINT business")

            receipt = self._make_receipt(
                ok=error is None,
                op=op,
                scope=scope,
                request_id=request_id,
                at_ms=now,
                data=data,
                error=error,
            )
            conn.execute(
                """
                INSERT INTO requests (tenant_id, request_id, kind, params_key, params_json,
                                      status, response_json, error_code, error_message,
                                      error_details_json, created_at_ms, completed_at_ms,
                                      updated_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    request_id,
                    op,
                    params_key,
                    stable(canonical),
                    "succeeded" if error is None else "rejected",
                    stable(receipt),
                    None if error is None else error.code,
                    None if error is None else error.message,
                    None if error is None else stable(error.details),
                    now,
                    now,
                    now,
                ),
            )
            if error is not None:
                self._audit(
                    conn, now, scope, f"{op}.rejected", request_id,
                    subject_kind="request", subject_id=request_id,
                    reason=self._reason_of(params),
                    detail={"code": error.code, "message": error.message, "details": error.details},
                )
            self.hooks.reach("during_effects")
        # The transaction is durable here; the receipts cannot be lost anymore.
        self.hooks.reach("after_commit")
        return ExecResult(receipt, False)

    def snapshot(self, tenant_id: str | None = None) -> dict[str, Any]:
        """A deterministic, row-by-row dump of the persisted state."""
        conn = self.conn
        conn.execute("BEGIN")
        try:
            where, args = ("WHERE tenant_id = ?", (tenant_id,)) if tenant_id else ("", ())
            tenants = db.all_rows(conn, "SELECT * FROM tenants " + where + " ORDER BY tenant_id", args)
            members = db.all_rows(
                conn,
                "SELECT * FROM members " + where + " ORDER BY tenant_id, member_id",
                args,
            )
            invites = db.all_rows(
                conn,
                "SELECT * FROM invites " + where + " ORDER BY tenant_id, email, invite_epoch",
                args,
            )
            audits = db.all_rows(
                conn,
                "SELECT * FROM audits " + where + " ORDER BY audit_id",
                args,
            )
            requests = db.all_rows(
                conn,
                "SELECT * FROM requests " + where + " ORDER BY tenant_id, request_id",
                args,
            )
        finally:
            conn.execute("COMMIT")
        now = self.clock.now_ms()
        return {
            "now": iso(now),
            "tenants": [
                {"tenant_id": r["tenant_id"], "name": r["display_name"], "created_at": iso(r["created_at_ms"])}
                for r in tenants
            ],
            "members": [
                {
                    "tenant_id": r["tenant_id"],
                    "member_id": r["member_id"],
                    "email": r["email"],
                    "status": r["status"],
                    "member_epoch": r["member_epoch"],
                    "invite_id": r["invite_id"],
                    "created_at": iso(r["created_at_ms"]),
                    "updated_at": iso(r["updated_at_ms"]),
                }
                for r in members
            ],
            "invites": [
                {
                    "invite_id": r["invite_id"],
                    "tenant_id": r["tenant_id"],
                    "email": r["email"],
                    "invite_epoch": r["invite_epoch"],
                    "status": r["status"],
                    "effective_status": (
                        "expired"
                        if r["status"] == _PENDING and now >= r["expires_at_ms"]
                        else r["status"]
                    ),
                    "expires_at": iso(r["expires_at_ms"]),
                    "created_at": iso(r["created_at_ms"]),
                    "updated_at": iso(r["updated_at_ms"]),
                    "consumed_at": iso(r["consumed_at_ms"]),
                }
                for r in invites
            ],
            "audits": [
                {
                    "audit_id": r["audit_id"],
                    "at": iso(r["at_ms"]),
                    "tenant_id": r["tenant_id"],
                    "action": r["action"],
                    "request_id": r["request_id"],
                    "subject_kind": r["subject_kind"],
                    "subject_id": r["subject_id"],
                    "reason": r["reason"],
                    "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
                }
                for r in audits
            ],
            "requests": [
                {
                    "tenant_id": r["tenant_id"],
                    "request_id": r["request_id"],
                    "kind": r["kind"],
                    "status": r["status"],
                    "params_key": r["params_key"],
                    "error_code": r["error_code"],
                    "created_at": iso(r["created_at_ms"]),
                }
                for r in requests
            ],
        }

    # -- request plumbing --------------------------------------------------- #

    @staticmethod
    def _canonical(op: str, params: dict[str, Any]) -> dict[str, Any]:
        return json.loads(stable(params))

    @staticmethod
    def _scope_tenant(op: str, params: dict[str, Any]) -> str:
        if not isinstance(params, dict):
            raise RejectError("INVALID_REQUEST", "params must be an object")
        if op == "migrate.commit":
            explicit = params.get("request_tenant")
            if isinstance(explicit, str) and explicit:
                return explicit
            moves = params.get("moves")
            if isinstance(moves, list) and moves:
                first = moves[0]
                if isinstance(first, dict):
                    source = first.get("source")
                    if isinstance(source, dict) and isinstance(source.get("tenant_id"), str):
                        return source["tenant_id"]
            raise RejectError("INVALID_REQUEST", "cannot determine the request scope tenant")
        tenant = params.get("tenant_id")
        if isinstance(tenant, str) and tenant:
            return tenant
        raise RejectError("INVALID_REQUEST", "'tenant_id' must be a non-empty string", {"field": "tenant_id"})

    @staticmethod
    def _reason_of(params: dict[str, Any]) -> str | None:
        reason = params.get("reason") if isinstance(params, dict) else None
        return reason if isinstance(reason, str) else None

    @staticmethod
    def _make_receipt(
        *,
        ok: bool,
        op: str,
        scope: str,
        request_id: str,
        at_ms: int,
        data: dict[str, Any] | None = None,
        error: RejectError | None = None,
    ) -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "ok": ok,
            "committed": ok,
            "op": op,
            "tenant_id": scope,
            "request_id": request_id,
            "at": iso(at_ms),
        }
        if ok:
            receipt["data"] = data or {}
        else:
            assert error is not None
            receipt["error"] = {
                "code": error.code,
                "message": error.message,
                "details": error.details,
            }
        return receipt

    # -- shape checks (pure) ------------------------------------------------ #

    def _check_shape(self, op: str, params: dict[str, Any]) -> None:
        handler = {
            "tenant.create": self._shape_tenant_create,
            "member.add": self._shape_member_add,
            "member.revoke": self._shape_member_revoke,
            "member.leave": self._shape_member_revoke,
            "invite.issue": self._shape_invite_issue,
            "invite.revoke": self._shape_invite_revoke,
            "migrate.commit": self._shape_migrate,
        }.get(op)
        if handler is None:
            raise RejectError("UNKNOWN_OP", f"unknown operation {op!r}", {"op": op})
        handler(params)

    def _shape_tenant_create(self, params: dict[str, Any]) -> None:
        _str(params, "tenant_id")
        name = params.get("name")
        if name is not None and not isinstance(name, str):
            raise RejectError("INVALID_REQUEST", "'name' must be a string", {"field": "name"})

    def _shape_member_add(self, params: dict[str, Any]) -> None:
        _str(params, "tenant_id")
        normalize_email(params.get("email"))

    def _shape_member_revoke(self, params: dict[str, Any]) -> None:
        _str(params, "tenant_id")
        _str(params, "member_id")
        _int(params, "member_epoch")

    def _shape_invite_issue(self, params: dict[str, Any]) -> None:
        _str(params, "tenant_id")
        normalize_email(params.get("email"))
        ttl = _int(params, "ttl_ms")
        if ttl <= 0:
            raise RejectError("INVALID_REQUEST", "'ttl_ms' must be positive", {"field": "ttl_ms"})

    def _shape_invite_revoke(self, params: dict[str, Any]) -> None:
        _str(params, "tenant_id")
        normalize_email(params.get("email"))
        _int(params, "invite_epoch")

    def _shape_migrate(self, params: dict[str, Any]) -> None:
        moves = _list(params.get("moves"), "moves")
        if not moves:
            raise RejectError("INVALID_REQUEST", "'moves' must not be empty", {"field": "moves"})
        reason = params.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise RejectError("INVALID_REQUEST", "'reason' must be a string", {"field": "reason"})
        for index, move in enumerate(moves):
            move = _obj(move, f"moves[{index}]")
            source = _obj(move.get("source"), f"moves[{index}].source")
            target = _obj(move.get("target"), f"moves[{index}].target")
            _str(source, "tenant_id")
            _str(source, "member_id")
            _int(source, "member_epoch")
            _str(target, "tenant_id")
            normalize_email(target.get("email"))
            _int(target, "invite_epoch")

    # -- read-only preflight ------------------------------------------------ #

    @staticmethod
    def _sweep_tenants(op: str, params: dict[str, Any]) -> list[str]:
        """The tenants whose invitations a request can look at, deduplicated."""
        tenants: list[str] = []
        if op == "migrate.commit":
            moves = params.get("moves")
            if isinstance(moves, list):
                for move in moves:
                    target = move.get("target") if isinstance(move, dict) else None
                    tenant = target.get("tenant_id") if isinstance(target, dict) else None
                    if isinstance(tenant, str) and tenant not in tenants:
                        tenants.append(tenant)
        elif isinstance(params.get("tenant_id"), str):
            tenants.append(params["tenant_id"])
        return tenants

    def _preflight(self, op: str, params: dict[str, Any]) -> None:
        """Read without a write lock, for the sync point only.

        The verdict is *never* used to decide anything and is never stored: the
        authoritative pass happens under the write lock (see ``_apply``).  This
        is what makes a preflight-then-commit design safe against another
        process moving the world in between.
        """
        conn = self.conn
        try:
            if op == "migrate.commit":
                plan = self._parse_migration(params)
                for move in plan.moves:
                    db.one(conn,
                           "SELECT * FROM members WHERE tenant_id = ? AND member_id = ?",
                           (move.src_tenant, move.src_member))
                    db.one(conn,
                           "SELECT * FROM invites WHERE tenant_id = ? AND email = ? AND invite_epoch = ?",
                           (move.tgt_tenant, move.tgt_email, move.tgt_epoch))
            elif op in ("member.revoke", "member.leave"):
                db.one(conn,
                       "SELECT * FROM members WHERE tenant_id = ? AND member_id = ?",
                       (params["tenant_id"], params["member_id"]))
        except RejectError:
            raise
        except (KeyError, TypeError):
            # A malformed request has no read to warm; the write path refuses it.
            return


    # -- authoritative apply (inside the write transaction) ----------------- #

    def _apply(
        self,
        conn: sqlite3.Connection,
        op: str,
        params: dict[str, Any],
        request_id: str,
        now: int,
    ) -> dict[str, Any]:
        handler = {
            "tenant.create": self._apply_tenant_create,
            "member.add": self._apply_member_add,
            "member.revoke": self._apply_member_revoke,
            "member.leave": self._apply_member_leave,
            "invite.issue": self._apply_invite_issue,
            "invite.revoke": self._apply_invite_revoke,
            "migrate.commit": self._apply_migrate,
        }[op]
        return handler(conn, params, request_id, now)

    # -- tenants ------------------------------------------------------------ #

    def _apply_tenant_create(
        self, conn: sqlite3.Connection, params: dict[str, Any], request_id: str, now: int
    ) -> dict[str, Any]:
        tenant = params["tenant_id"]
        if db.one(conn, "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant,)):
            raise RejectError("TENANT_EXISTS", "tenant already exists", {"tenant_id": tenant})
        name = params.get("name")
        conn.execute(
            "INSERT INTO tenants (tenant_id, display_name, created_at_ms) VALUES (?, ?, ?)",
            (tenant, name, now),
        )
        self._audit(
            conn, now, tenant, "tenant.created", request_id,
            subject_kind="tenant", subject_id=tenant, reason=self._reason_of(params),
            detail={"name": name},
        )
        return {"tenant_id": tenant, "name": name, "created_at": iso(now)}

    # -- members ------------------------------------------------------------ #

    def _apply_member_add(
        self, conn: sqlite3.Connection, params: dict[str, Any], request_id: str, now: int
    ) -> dict[str, Any]:
        tenant = params["tenant_id"]
        email = normalize_email(params.get("email"))
        self._require_tenant(conn, tenant)
        self._expire_due(conn, tenant, now)
        if db.one(
            conn,
            "SELECT 1 FROM members WHERE tenant_id = ? AND email = ? AND status = 'active'",
            (tenant, email),
        ):
            raise RejectError(
                "MEMBER_EXISTS", "an active member already holds that email in this tenant",
                {"tenant_id": tenant, "email": email},
            )
        member_id = self._new_member_id(conn, tenant, email)
        conn.execute(
            """
            INSERT INTO members (tenant_id, member_id, email, status, member_epoch, invite_id,
                                 created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, 'active', 1, NULL, ?, ?)
            """,
            (tenant, member_id, email, now, now),
        )
        self._audit(
            conn, now, tenant, "member.added", request_id,
            subject_kind="member", subject_id=member_id, reason=self._reason_of(params),
            detail={"email": email},
        )
        return {
            "tenant_id": tenant,
            "member_id": member_id,
            "email": email,
            "member_epoch": 1,
            "status": _ACTIVE,
        }

    def _apply_member_revoke(
        self, conn: sqlite3.Connection, params: dict[str, Any], request_id: str, now: int
    ) -> dict[str, Any]:
        return self._revoke_member(conn, params, request_id, now, action="member.revoked", why="administrative revocation")

    def _apply_member_leave(
        self, conn: sqlite3.Connection, params: dict[str, Any], request_id: str, now: int
    ) -> dict[str, Any]:
        return self._revoke_member(conn, params, request_id, now, action="member.left", why="member departure")

    def _revoke_member(
        self,
        conn: sqlite3.Connection,
        params: dict[str, Any],
        request_id: str,
        now: int,
        *,
        action: str,
        why: str,
    ) -> dict[str, Any]:
        tenant = params["tenant_id"]
        member_id = params["member_id"]
        epoch = params["member_epoch"]
        row = db.one(
            conn,
            "SELECT * FROM members WHERE tenant_id = ? AND member_id = ?",
            (tenant, member_id),
        )
        if row is None:
            raise RejectError(
                "MEMBER_NOT_FOUND", "no such member", {"tenant_id": tenant, "member_id": member_id}
            )
        if row["status"] != _ACTIVE:
            raise RejectError(
                "MEMBER_NOT_ACTIVE", "the member is not active",
                {"tenant_id": tenant, "member_id": member_id, "status": row["status"]},
            )
        if row["member_epoch"] != epoch:
            raise RejectError(
                "MEMBER_EPOCH_MISMATCH",
                "member_epoch does not match the current member generation",
                {"tenant_id": tenant, "member_id": member_id,
                 "expected": row["member_epoch"], "given": epoch},
            )
        new_epoch = row["member_epoch"] + 1
        conn.execute(
            "UPDATE members SET status = 'revoked', member_epoch = ?, updated_at_ms = ? "
            "WHERE tenant_id = ? AND member_id = ?",
            (new_epoch, now, tenant, member_id),
        )
        self._audit(
            conn, now, tenant, action, request_id,
            subject_kind="member", subject_id=member_id,
            reason=self._reason_of(params) or why,
            detail={"email": row["email"], "from_epoch": epoch, "to_epoch": new_epoch},
        )
        return {
            "tenant_id": tenant,
            "member_id": member_id,
            "email": row["email"],
            "status": _REVOKED,
            "previous_member_epoch": epoch,
            "member_epoch": new_epoch,
        }

    # -- invitations -------------------------------------------------------- #

    def _apply_invite_issue(
        self, conn: sqlite3.Connection, params: dict[str, Any], request_id: str, now: int
    ) -> dict[str, Any]:
        tenant = params["tenant_id"]
        email = normalize_email(params.get("email"))
        ttl_ms = params["ttl_ms"]
        self._require_tenant(conn, tenant)
        self._expire_due(conn, tenant, now)
        # An invitation says nothing about whether the seat is currently held:
        # occupancy is decided by migrate.commit, after every source has left.
        previous = db.one(
            conn,
            "SELECT MAX(invite_epoch) AS m FROM invites WHERE tenant_id = ? AND email = ?",
            (tenant, email),
        )["m"]
        epoch = (previous or 0) + 1
        superseded = conn.execute(
            "UPDATE invites SET status = 'superseded', updated_at_ms = ? "
            "WHERE tenant_id = ? AND email = ? AND status = 'pending'",
            (now, tenant, email),
        ).rowcount
        expires_at = now + ttl_ms
        cursor = conn.execute(
            """
            INSERT INTO invites (tenant_id, email, invite_epoch, status, expires_at_ms,
                                 created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, 'pending', ?, ?, ?)
            """,
            (tenant, email, epoch, expires_at, now, now),
        )
        invite_id = int(cursor.lastrowid)
        self._audit(
            conn, now, tenant, "invite.issued", request_id,
            subject_kind="invite", subject_id=str(invite_id),
            reason=self._reason_of(params),
            detail={"email": email, "invite_epoch": epoch, "expires_at": iso(expires_at),
                    "superseded": superseded, "ttl_ms": ttl_ms},
        )
        return {
            "invite_id": invite_id,
            "tenant_id": tenant,
            "email": email,
            "invite_epoch": epoch,
            "status": _PENDING,
            "expires_at": iso(expires_at),
            "superseded_previous": superseded,
        }

    def _apply_invite_revoke(
        self, conn: sqlite3.Connection, params: dict[str, Any], request_id: str, now: int
    ) -> dict[str, Any]:
        tenant = params["tenant_id"]
        email = normalize_email(params.get("email"))
        epoch = params["invite_epoch"]
        self._expire_due(conn, tenant, now)
        row = db.one(
            conn,
            "SELECT * FROM invites WHERE tenant_id = ? AND email = ? AND invite_epoch = ?",
            (tenant, email, epoch),
        )
        if row is None:
            raise RejectError(
                "INVITE_NOT_FOUND", "no invitation with that binding and generation",
                {"tenant_id": tenant, "email": email, "invite_epoch": epoch},
            )
        if row["status"] == "expired":
            raise RejectError(
                "INVITE_EXPIRED", "the invitation expired",
                {"tenant_id": tenant, "email": email, "invite_epoch": epoch,
                 "expires_at": iso(row["expires_at_ms"])},
            )
        if row["status"] != _PENDING:
            raise RejectError(
                "INVITE_NOT_PENDING", "the invitation is not pending",
                {"tenant_id": tenant, "email": email, "invite_epoch": epoch,
                 "status": row["status"]},
            )
        conn.execute(
            "UPDATE invites SET status = 'revoked', updated_at_ms = ? WHERE invite_id = ?",
            (now, row["invite_id"]),
        )
        self._audit(
            conn, now, tenant, "invite.revoked", request_id,
            subject_kind="invite", subject_id=str(row["invite_id"]),
            reason=self._reason_of(params),
            detail={"email": email, "invite_epoch": epoch},
        )
        return {
            "invite_id": row["invite_id"],
            "tenant_id": tenant,
            "email": email,
            "invite_epoch": epoch,
            "status": "revoked",
        }

    # -- migration ---------------------------------------------------------- #

    def _parse_migration(self, params: dict[str, Any]) -> MigrationPlan:
        plan = MigrationPlan(reason=self._reason_of(params))
        for index, raw in enumerate(params["moves"]):
            source = _obj(raw.get("source"), f"moves[{index}].source")
            target = _obj(raw.get("target"), f"moves[{index}].target")
            plan.moves.append(
                MovePlan(
                    src_tenant=source["tenant_id"],
                    src_member=source["member_id"],
                    src_epoch=source["member_epoch"],
                    tgt_tenant=target["tenant_id"],
                    tgt_email=normalize_email(target.get("email")),
                    tgt_epoch=target["invite_epoch"],
                )
            )
        plan.scope_tenant = ""
        return plan

    @staticmethod
    def _check_duplicates(plan: MigrationPlan) -> None:
        seen_sources: dict[tuple[str, str], int] = {}
        seen_targets: dict[tuple[str, str], int] = {}
        for index, move in enumerate(plan.moves):
            source_key = (move.src_tenant, move.src_member)
            if source_key in seen_sources:
                raise RejectError(
                    "DUPLICATE_SOURCE", "the same source member appears twice in the batch",
                    {"tenant_id": move.src_tenant, "member_id": move.src_member,
                     "first_index": seen_sources[source_key], "again_index": index},
                )
            seen_sources[source_key] = index
            target_key = (move.tgt_tenant, move.tgt_email)
            if target_key in seen_targets:
                raise RejectError(
                    "DUPLICATE_TARGET", "the same target invitation appears twice in the batch",
                    {"tenant_id": move.tgt_tenant, "email": move.tgt_email,
                     "first_index": seen_targets[target_key], "again_index": index},
                )
            seen_targets[target_key] = index

    def _apply_migrate(
        self, conn: sqlite3.Connection, params: dict[str, Any], request_id: str, now: int
    ) -> dict[str, Any]:
        plan = self._parse_migration(params)
        self._check_duplicates(plan)

        # ---- 1. re-read every referenced version under the write lock ----
        for index, move in enumerate(plan.moves):
            row = db.one(
                conn,
                "SELECT * FROM members WHERE tenant_id = ? AND member_id = ?",
                (move.src_tenant, move.src_member),
            )
            if row is None:
                raise RejectError(
                    "MEMBER_NOT_FOUND", "no such source member",
                    {"tenant_id": move.src_tenant, "member_id": move.src_member,
                     "move_index": index},
                )
            if row["status"] != _ACTIVE:
                raise RejectError(
                    "MEMBER_NOT_ACTIVE", "the source member is not active",
                    {"tenant_id": move.src_tenant, "member_id": move.src_member,
                     "status": row["status"], "move_index": index},
                )
            if row["member_epoch"] != move.src_epoch:
                raise RejectError(
                    "MEMBER_EPOCH_MISMATCH",
                    "the source member generation moved on",
                    {"tenant_id": move.src_tenant, "member_id": move.src_member,
                     "expected": row["member_epoch"], "given": move.src_epoch,
                     "move_index": index},
                )
            move.src_row = row
            move.src_email = row["email"]

        for index, move in enumerate(plan.moves):
            if move.src_tenant == move.tgt_tenant:
                raise RejectError(
                    "SAME_TENANT", "a migration move must cross tenant boundaries",
                    {"tenant_id": move.src_tenant, "email": move.tgt_email,
                     "move_index": index},
                )

        for index, move in enumerate(plan.moves):
            self._expire_due(conn, move.tgt_tenant, now)
            inv = db.one(
                conn,
                "SELECT * FROM invites WHERE tenant_id = ? AND email = ? AND invite_epoch = ?",
                (move.tgt_tenant, move.tgt_email, move.tgt_epoch),
            )
            if inv is None:
                raise RejectError(
                    "INVITE_NOT_FOUND", "no invitation with that binding and generation",
                    {"tenant_id": move.tgt_tenant, "email": move.tgt_email,
                     "invite_epoch": move.tgt_epoch, "move_index": index},
                )
            move.invite_row = inv
            move.invite_id = int(inv["invite_id"])
            move.invite_expires_ms = int(inv["expires_at_ms"])
            if inv["status"] == "expired" or (inv["status"] == _PENDING and now >= inv["expires_at_ms"]):
                raise RejectError(
                    "INVITE_EXPIRED", "the target invitation expired",
                    {"tenant_id": move.tgt_tenant, "email": move.tgt_email,
                     "invite_epoch": move.tgt_epoch, "expires_at": iso(inv["expires_at_ms"]),
                     "now": iso(now), "move_index": index},
                )
            if inv["status"] != _PENDING:
                raise RejectError(
                    "INVITE_NOT_PENDING", "the target invitation is no longer pending",
                    {"tenant_id": move.tgt_tenant, "email": move.tgt_email,
                     "invite_epoch": move.tgt_epoch, "status": inv["status"],
                     "move_index": index},
                )

        # ---- 2. occupancy *after* every source has been removed ----------
        # A target is free if the batch itself vacates it; otherwise some other
        # active member must not already hold it.
        vacated = {(m.src_tenant, m.src_email) for m in plan.moves}
        for move in plan.moves:
            occupied = db.one(
                conn,
                "SELECT member_id FROM members WHERE tenant_id = ? AND email = ? AND status = 'active'",
                (move.tgt_tenant, move.tgt_email),
            )
            if occupied is not None and (move.tgt_tenant, move.tgt_email) not in vacated:
                raise RejectError(
                    "TARGET_OCCUPIED",
                    "the target seat is still held after the batch sources leave",
                    {"tenant_id": move.tgt_tenant, "email": move.tgt_email,
                     "occupant_member_id": occupied["member_id"]},
                )

        # ---- 3. plan the new member identities (deterministic) -----------
        for move in plan.moves:
            move.new_member_id = self._new_member_id(conn, move.tgt_tenant, move.tgt_email)

        # ---- 4. effects: every validation has already passed --------------
        for move in plan.moves:
            conn.execute(
                "UPDATE members SET status = 'revoked', member_epoch = member_epoch + 1, "
                "updated_at_ms = ? WHERE tenant_id = ? AND member_id = ?",
                (now, move.src_tenant, move.src_member),
            )
            self._audit(
                conn, now, move.src_tenant, "migration.source_revoked", request_id,
                subject_kind="member", subject_id=move.src_member,
                reason=plan.reason,
                detail={"email": move.src_email, "to_tenant": move.tgt_tenant,
                        "member_epoch": move.src_epoch + 1},
            )
        for move in plan.moves:
            conn.execute(
                "UPDATE invites SET status = 'consumed', consumed_at_ms = ?, updated_at_ms = ? "
                "WHERE invite_id = ?",
                (now, now, move.invite_id),
            )
            self._audit(
                conn, now, move.tgt_tenant, "invite.consumed", request_id,
                subject_kind="invite", subject_id=str(move.invite_id),
                reason=plan.reason,
                detail={"email": move.tgt_email, "invite_epoch": move.tgt_epoch,
                        "member_id": move.new_member_id},
            )
        for move in plan.moves:
            conn.execute(
                """
                INSERT INTO members (tenant_id, member_id, email, status, member_epoch,
                                     invite_id, created_at_ms, updated_at_ms)
                VALUES (?, ?, ?, 'active', 1, ?, ?, ?)
                """,
                (move.tgt_tenant, move.new_member_id, move.tgt_email,
                 move.invite_id, now, now),
            )
            self._audit(
                conn, now, move.tgt_tenant, "migration.target_joined", request_id,
                subject_kind="member", subject_id=move.new_member_id,
                reason=plan.reason,
                detail={"email": move.tgt_email, "from_tenant": move.src_tenant,
                        "from_member_id": move.src_member, "invite_id": move.invite_id},
            )
        self._audit(
            conn, now, self._scope_tenant("migrate.commit", params),
            "migration.committed", request_id,
            subject_kind="batch", subject_id=request_id,
            reason=plan.reason,
            detail={"moves": len(plan.moves)},
        )
        return {
            "reason": plan.reason,
            "moves": [
                {
                    "source": {
                        "tenant_id": m.src_tenant,
                        "member_id": m.src_member,
                        "email": m.src_email,
                        "member_epoch": m.src_epoch + 1,
                        "status": _REVOKED,
                    },
                    "target": {
                        "tenant_id": m.tgt_tenant,
                        "member_id": m.new_member_id,
                        "email": m.tgt_email,
                        "member_epoch": 1,
                        "invite_id": m.invite_id,
                        "invite_epoch": m.tgt_epoch,
                        "status": _ACTIVE,
                    },
                }
                for m in plan.moves
            ],
        }

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _require_tenant(conn: sqlite3.Connection, tenant: str) -> None:
        if not db.one(conn, "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant,)):
            raise RejectError("TENANT_NOT_FOUND", "no such tenant", {"tenant_id": tenant})

    @staticmethod
    def _new_member_id(conn: sqlite3.Connection, tenant: str, email: str) -> str:
        """A deterministic identity: same tenant + email + first-ever insert ordinal.

        The ordinal counts *all* memberships ever created for that binding, so a
        replayed case can name the new member without reading the implementation.
        """
        count = db.one(
            conn,
            "SELECT COUNT(*) AS n FROM members WHERE tenant_id = ? AND email = ?",
            (tenant, email),
        )["n"]
        ordinal = count + 1
        local = email.split("@", 1)[0]
        return f"{local}@{tenant}.m{ordinal}"

    @staticmethod
    def _expire_due(conn: sqlite3.Connection, tenant: str, now: int) -> None:
        """Materialise the ``pending -> expired`` transition for one tenant."""
        due = db.all_rows(
            conn,
            "SELECT invite_id, email, invite_epoch FROM invites "
            "WHERE tenant_id = ? AND status = 'pending' AND expires_at_ms <= ?",
            (tenant, now),
        )
        for row in due:
            conn.execute(
                "UPDATE invites SET status = 'expired', updated_at_ms = ? WHERE invite_id = ?",
                (now, row["invite_id"]),
            )
            conn.execute(
                """
                INSERT INTO audits (at_ms, tenant_id, action, request_id, subject_kind,
                                    subject_id, reason, detail_json)
                VALUES (?, ?, 'invite.expired', NULL, 'invite', ?, 'ttl_elapsed', ?)
                """,
                (now, tenant, str(row["invite_id"]),
                 json.dumps({"email": row["email"], "invite_epoch": row["invite_epoch"]},
                            sort_keys=True)),
            )

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        now: int,
        tenant: str,
        action: str,
        request_id: str | None,
        *,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO audits (at_ms, tenant_id, action, request_id, subject_kind, subject_id,
                                reason, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (now, tenant, action, request_id, subject_kind, subject_id, reason,
             json.dumps(detail, sort_keys=True) if detail is not None else None),
        )
