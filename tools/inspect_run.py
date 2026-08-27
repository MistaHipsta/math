"""Inspect a collected HTTP run: response shapes, errors, conversion gaps."""

from __future__ import annotations

import collections
import glob
import json
import sys
from pathlib import Path


def main(run_dir: str) -> int:
    files = sorted(glob.glob(f"{run_dir}/MoonSisters-worker-*.jsonl"))
    shapes: collections.Counter[tuple[str, ...]] = collections.Counter()
    types: collections.Counter[str] = collections.Counter()
    statuses: collections.Counter[int] = collections.Counter()
    missing: collections.Counter[str] = collections.Counter()
    example_bad = None
    total = 0

    for path in files:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            total += 1
            types[event.get("type", "?")] += 1
            response = event.get("response") or {}
            statuses[response.get("Status", 0)] += 1
            raw = response.get("RawJson") or ""
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                missing["not_json"] += 1
                continue

            shapes[tuple(sorted(payload.keys()))] += 1
            context = payload.get("context") or {}
            spins = context.get("spins") or {}
            if not isinstance(spins.get("board"), list):
                missing["no_board"] += 1
                if example_bad is None:
                    example_bad = payload
            elif spins.get("round_bet") is None:
                missing["no_round_bet"] += 1
                if example_bad is None:
                    example_bad = payload
            elif (
                spins.get("round_win") is None
                and spins.get("total_win") is None
                and context.get("last_win") is None
            ):
                missing["no_payout"] += 1
                if example_bad is None:
                    example_bad = payload

    print(f"files={len(files)}")
    print(f"lines={total}")
    print(f"types={dict(types)}")
    print(f"statuses={dict(statuses)}")
    print(f"missing={dict(missing)}")
    print("payload_shapes:")
    for keys, count in shapes.most_common(10):
        print(f"  {count} {list(keys)}")
    if example_bad is not None:
        print("example_unconvertible:")
        print(json.dumps(example_bad, ensure_ascii=False)[:900])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))