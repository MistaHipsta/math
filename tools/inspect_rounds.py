"""Check whether collected rounds are finished or need follow-up actions."""

from __future__ import annotations

import collections
import glob
import json
import sys
from pathlib import Path


def main(run_dir: str) -> int:
    finished: collections.Counter = collections.Counter()
    actions: collections.Counter = collections.Counter()
    last_action: collections.Counter = collections.Counter()
    current: collections.Counter = collections.Counter()
    unfinished_example = None
    win_from_unfinished = 0
    total_round_win = 0
    total_last_win = 0

    for path in sorted(glob.glob(f"{run_dir}/MoonSisters-worker-*.jsonl")):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") != "round_response":
                continue
            payload = json.loads(event["response"]["RawJson"])
            context = payload.get("context") or {}
            spins = context.get("spins") or {}

            done = context.get("round_finished")
            finished[str(done)] += 1
            actions[json.dumps(context.get("actions"))] += 1
            last_action[str(context.get("last_action"))] += 1
            current[str(context.get("current"))] += 1

            total_round_win += spins.get("round_win") or 0
            total_last_win += context.get("last_win") or 0

            if done is False:
                win_from_unfinished += 1
                if unfinished_example is None:
                    unfinished_example = payload

    print(f"round_finished={dict(finished)}")
    print(f"last_action={dict(last_action.most_common(5))}")
    print(f"current={dict(current.most_common(5))}")
    print(f"actions={dict(actions.most_common(5))}")
    print(f"unfinished_rounds={win_from_unfinished}")
    print(f"sum_round_win={total_round_win}")
    print(f"sum_last_win={total_last_win}")
    if unfinished_example is not None:
        print("unfinished_example:")
        print(json.dumps(unfinished_example, ensure_ascii=False)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))