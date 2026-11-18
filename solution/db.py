"""SQLite persistence: schema, connections, transactions, and small row helpers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id    TEXT PRIMARY KEY,
    display_name TEXT,
    created_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    tenant_id    TEXT NOT NULL,
    member_id    TEXT NOT NULL,
    email        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    member_epoch INTEGER NOT NULL,
    invite_id    INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, member_id)
);

CREATE TABLE IF NOT EXISTS invites (
    invite_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
    email         TEXT NOT NULL,
    invite_epoch  INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'consumed', 'revoked', 'superseded', 'expired')),
    expires_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    consumed_at_ms INTEGER
);

CREATE TABLE IF NOT EXISTS requests (
    tenant_id         TEXT NOT NULL,
    request_id        TEXT NOT NULL,
    kind              TEXT NOT NULL,
    params_key        TEXT NOT NULL,
    params_json       TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('succeeded', 'rejected')),
    response_json     TEXT,
    error_code        TEXT,
    error_message     TEXT,
    error_details_json TEXT,
    created_at_ms     INTEGER NOT NULL,
    updated_at_ms     INTEGER NOT NULL,
    completed_at_ms   INTEGER,
    PRIMARY KEY (tenant_id, request_id)
);

CREATE TABLE IF NOT EXISTS audits (
    audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms        INTEGER NOT NULL,
    tenant_id    TEXT NOT NULL,
    action       TEXT NOT NULL,
    request_id   TEXT,
    subject_kind TEXT,
    subject_id   TEXT,
    reason       TEXT,
    detail_json  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS invites_one_pending_binding
    ON invites(tenant_id, email) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS members_one_active_email
    ON members(tenant_id, email) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS invites_binding_history
    ON invites(tenant_id, email, invite_epoch);
CREATE INDEX IF NOT EXISTS audits_by_tenant
    ON audits(tenant_id, audit_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection configured for multi-process use on a local disk."""
    conn = sqlite3.connect(db_path, timeout=60.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A DEFERRED-to-IMMEDIATE write transaction; rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def one(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, args).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, args).fetchall())
