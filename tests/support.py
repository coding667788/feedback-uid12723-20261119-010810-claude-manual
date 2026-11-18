"""Shared test plumbing: throw-away databases, snapshots, subprocess helpers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from typing import Any

from solution.directory import Directory, FixedClock, Hooks

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = "2026-01-01T00:00:00Z"
T0_MS = 1767225600000
MINUTE = 60_000
HOUR = 60 * MINUTE


class TempDirMixin(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._dirs: list[str] = []

    def tearDown(self) -> None:
        for path in self._dirs:
            shutil.rmtree(path, ignore_errors=True)
        super().tearDown()

    def temp_dir(self) -> str:
        path = tempfile.mkdtemp(prefix="directory-test-")
        self._dirs.append(path)
        return path

    def db_path(self) -> str:
        return os.path.join(self.temp_dir(), "state.db")


def make_directory(db_path: str, at_ms: int = T0_MS, hooks: Hooks | None = None) -> Directory:
    return Directory(db_path, clock=FixedClock(at_ms), hooks=hooks)


def snapshot_of(db_path: str, at_ms: int = T0_MS) -> dict[str, Any]:
    """Open, read one snapshot, and close again."""
    with make_directory(db_path, at_ms) as directory:
        return directory.snapshot()


def ok(directory: Directory, op: str, params: dict[str, Any], rid: str, at_ms: int | None = None):
    if at_ms is not None and isinstance(directory.clock, FixedClock):
        directory.clock.set(at_ms)
    result = directory.execute(op, params, rid)
    return result.receipt


def call(directory: Directory, op: str, params: dict[str, Any], rid: str) -> dict[str, Any]:
    return directory.execute(op, params, rid).receipt


def base_tenants(directory: Directory) -> None:
    for tenant in ("t-a", "t-b", "t-c"):
        call(directory, "tenant.create", {"tenant_id": tenant, "name": tenant.upper()}, f"setup-{tenant}")


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #


def start_worker(
    job: dict[str, Any],
    workdir: str,
    *,
    env_extra: dict[str, str] | None = None,
) -> subprocess.Popen:
    job_path = os.path.join(workdir, f"job-{uuid.uuid4().hex}.json")
    with open(job_path, "w", encoding="utf-8") as handle:
        json.dump(job, handle)
    env = dict(os.environ)
    env["PYTHONPATH"] = PROJECT_ROOT
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        [sys.executable, "-m", "solution.worker", "--job", job_path],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish_worker(process: subprocess.Popen, timeout: float = 120.0) -> tuple[int, str, str]:
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        out, err = process.communicate()
        raise AssertionError(f"worker timed out\nstdout:\n{out}\nstderr:\n{err}")
    return process.returncode, out, err


def read_receipt(path: str) -> dict[str, Any]:
    """Read the ``{"receipt": ...}`` payload a worker writes after it returns."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)["receipt"]


def wait_for_file(path: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.005)


class SyncGate:
    """A named rendezvous directory for :class:`solution.directory.Hooks`."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(path, exist_ok=True)

    def wait_reached(self, point: str, timeout: float = 120.0) -> None:
        wait_for_file(os.path.join(self.path, f"{point}.reached"), timeout)

    def release(self, point: str) -> None:
        with open(os.path.join(self.path, f"{point}.go"), "w", encoding="utf-8") as handle:
            handle.write("go")


# --------------------------------------------------------------------------- #
# A straight-from-SQLite snapshot used as the restart baseline
# --------------------------------------------------------------------------- #


def business(view: dict[str, Any]) -> dict[str, Any]:
    """Only the business tables of a snapshot, for before/after comparisons."""
    return {key: view[key] for key in ("tenants", "members", "invites")}


def request_rows(view: dict[str, Any]) -> list[dict[str, Any]]:
    return view["requests"]


def raw_rows(db_file: str) -> dict[str, list[tuple]]:
    """Every user table, row by row, read without going through the solution."""
    import sqlite3

    conn = sqlite3.connect(db_file)
    try:
        out: dict[str, list[tuple]] = {}
        for table in ("tenants", "members", "invites", "requests", "audits", "meta"):
            columns = tuple(d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description)
            rows = [tuple(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            out[table] = sorted(rows)
            out[table + ":columns"] = [columns]
        return out
    finally:
        conn.close()
