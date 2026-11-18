"""Subprocess entry point used by the crash and concurrency tests.

    python -m solution.worker --job job.json

The job file is JSON::

    {
      "db": "<path to state.db>",
      "op": "migrate.commit",
      "params": {...},
      "request_id": "rid-x",
      "clock_ms": 1767225600000,      # optional virtual clock
      "crash_at": "during_effects",   # optional: os._exit(90) at that checkpoint
      "sync_dir": "<dir>",            # optional: rendezvous files
      "output": "<path to receipt>",  # written only if the process returns
      "wait_for_release": 30.0
    }

``crash_at`` calls ``os._exit`` — a real process termination with no unwinding,
exactly like a kill.  Nothing is written to ``output`` in that case.
"""
from __future__ import annotations

import argparse
import json
import os

from .directory import Directory, FixedClock, Hooks, SystemClock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m solution.worker")
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    with open(args.job, "r", encoding="utf-8") as handle:
        job = json.load(handle)

    clock = FixedClock(int(job["clock_ms"])) if "clock_ms" in job else SystemClock()
    hooks = Hooks(
        crash_at=job.get("crash_at"),
        sync_dir=job.get("sync_dir"),
        sync_points=job.get("sync_points"),
        timeout_s=float(job.get("wait_for_release", 30.0)),
    )
    with Directory(job["db"], clock=clock, hooks=hooks) as directory:
        result = directory.execute(job["op"], job["params"], job["request_id"])
        payload = {"receipt": result.receipt, "replayed": result.replayed}
    if job.get("output"):
        tmp = job["output"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, job["output"])
    print(json.dumps(payload["receipt"], sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
