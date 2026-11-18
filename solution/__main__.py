"""Command line runner: ``python -m solution --input cases.json --output result.json``.

Every case owns a fresh throw-away SQLite file, so a case is fully reproducible.
The result file holds one entry per operation (its receipt, if any), the final
persisted snapshot, and the outcome of the case's own assertions.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from typing import Any

from .directory import Directory, FixedClock, Hooks, SystemClock
from .util import RejectError, parse_iso_ms

DEFAULT_DB_NAME = "state.db"


# --------------------------------------------------------------------------- #
# Assertion paths such as  "receipts.2.error.code"  or  "final_state.members[0].email"
# --------------------------------------------------------------------------- #


def resolve_path(document: Any, path: str) -> Any:
    """Walk a dotted path; numeric segments index a list, ``name[i]`` also works."""
    tokens: list[Any] = []
    for chunk in path.split("."):
        name, _, rest = chunk.partition("[")
        if name:
            tokens.append(name)
        while rest:
            index, _, rest = rest.partition("]")
            tokens.append(int(index))
            rest = rest.lstrip("[")
    current = document
    for token in tokens:
        if isinstance(current, list):
            try:
                position = int(token)
            except (TypeError, ValueError):
                raise LookupError(path) from None
            if position < 0 or position >= len(current):
                raise LookupError(path)
            current = current[position]
        else:
            if not isinstance(current, dict) or token not in current:
                raise LookupError(path)
            current = current[token]
    return current


def check_assertion(document: Any, assertion: dict[str, Any]) -> dict[str, Any]:
    path = assertion["path"]
    outcome: dict[str, Any] = {"path": path}
    try:
        actual = resolve_path(document, path)
    except LookupError:
        actual = _MISSING
    outcome["actual"] = None if actual is _MISSING else actual

    if "equals" in assertion:
        expected = assertion["equals"]
        passed = actual is not _MISSING and actual == expected
    elif "length" in assertion:
        expected = assertion["length"]
        passed = actual is not _MISSING and hasattr(actual, "__len__") and len(actual) == expected
    elif "in" in assertion:
        expected = assertion["in"]
        passed = actual is not _MISSING and actual in expected
    elif "exists" in assertion:
        expected = bool(assertion["exists"])
        passed = expected == (actual is not _MISSING)
    elif "absent" in assertion:
        expected = bool(assertion["absent"])
        passed = expected == (actual is _MISSING)
    else:
        raise ValueError(f"assertion for {path!r} has no operator")
    outcome["expected"] = expected
    outcome["passed"] = passed
    return outcome


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


_MISSING = _Missing()


# --------------------------------------------------------------------------- #
# Case execution
# --------------------------------------------------------------------------- #


def _request_id_for(step: dict[str, Any]) -> str:
    rid = step.get("request_id")
    if rid is not None:
        return rid
    return "rid-" + str(step["id"])


def run_case(case: dict[str, Any], workdir: str) -> dict[str, Any]:
    case_id = case.get("id") or "case"
    case_dir = os.path.join(workdir, case_id)
    os.makedirs(case_dir, exist_ok=True)
    db_path = os.path.join(case_dir, DEFAULT_DB_NAME)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass

    clock: Any
    if case.get("start_at"):
        clock = FixedClock(parse_iso_ms(case["start_at"]))
    else:
        clock = SystemClock()
    hooks = Hooks()

    receipts: list[dict[str, Any] | None] = []
    steps_out: list[dict[str, Any]] = []
    with Directory(db_path, clock=clock, hooks=hooks) as directory:
        for step in case.get("ops", []):
            if step.get("at"):
                new_time = parse_iso_ms(step["at"])
                if isinstance(clock, FixedClock):
                    clock.set(new_time)
                else:
                    raise ValueError("'at' requires the case to define 'start_at'")
            op = step["op"]
            params = step.get("params")
            request_id = _request_id_for(step)
            if params is None:
                raise ValueError(f"step {step.get('id')!r} has no params")
            try:
                receipt = directory.execute(op, params, request_id).receipt
            except RejectError as exc:
                # A request with no usable tenant scope cannot be keyed at all,
                # so it is reported here instead of being persisted.
                receipt = {
                    "ok": False,
                    "committed": False,
                    "op": op,
                    "tenant_id": None,
                    "request_id": request_id,
                    "at": None,
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                }
            receipts.append(receipt)
            steps_out.append(
                {
                    "id": step.get("id"),
                    "op": op,
                    "request_id": request_id,
                    "receipt": receipt,
                }
            )
        final_state = directory.snapshot()

    document: dict[str, Any] = {
        "case_id": case_id,
        "steps": steps_out,
        "receipts": receipts,
        "final_state": final_state,
    }
    results = [check_assertion(document, a) for a in case.get("assertions", [])]
    document["assertions"] = results
    document["passed"] = all(r["passed"] for r in results)
    return document


def load_cases(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return data["cases"]
    if isinstance(data, dict):
        return [data]
    raise ValueError("input must be a case object, a list of cases, or {'cases': [...]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m solution", description=__doc__)
    parser.add_argument("--input", required=True, help="path to cases.json")
    parser.add_argument("--output", required=True, help="path to the result.json to write")
    parser.add_argument(
        "--workdir",
        default=None,
        help="directory for the per-case SQLite files (default: a temporary directory)",
    )
    parser.add_argument(
        "--keep-databases",
        action="store_true",
        help="keep the temporary directory and print its path",
    )
    args = parser.parse_args(argv)

    cases = load_cases(args.input)
    scratch = args.workdir or tempfile.mkdtemp(prefix="solution-cases-")
    os.makedirs(scratch, exist_ok=True)
    results = []
    try:
        for case in cases:
            results.append(run_case(case, scratch))
    finally:
        if args.workdir is None and not args.keep_databases:
            shutil.rmtree(scratch, ignore_errors=True)
        elif args.keep_databases:
            print(f"databases kept in {scratch}", file=sys.stderr)

    payload: Any
    if len(results) == 1:
        payload = results[0]
    else:
        payload = {"cases": results, "passed": all(r["passed"] for r in results)}
    directory_name = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(directory_name, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False))
        handle.write("\n")

    failed = [r["case_id"] for r in results if not r["passed"]]
    if failed:
        print(f"assertions failed in: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"{len(results)} case(s) replayed; all assertions passed")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
