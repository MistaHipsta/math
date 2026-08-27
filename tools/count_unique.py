"""Count unique round outcomes in a collected run.

Reports how many distinct boards, board+payout pairs and payout values were
observed, which shows whether the sample is genuinely varied or repeats a
small set of server-side outcomes.
"""

from __future__ import annotations

import collections
import glob
import json
import sys
from pathlib import Path


def main(run_dir: str) -> int:
    boards: collections.Counter[str] = collections.Counter()
    outcomes: collections.Counter[str] = collections.Counter()
    payouts: collections.Counter[int] = collections.Counter()
    request_ids: set[str] = set()
    total = 0

    for path in sorted(glob.glob(f"{run_dir}/MoonSisters-worker-*.jsonl")):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") != "round_response":
                continue
            payload = json.loads(event["response"]["RawJson"])
            spins = (payload.get("context") or {}).get("spins") or {}
            board = spins.get("board")
            if not isinstance(board, list):
                continue

            total += 1
            key = json.dumps(board, separators=(",", ":"))
            win = spins.get("round_win") or 0
            boards[key] += 1
            outcomes[f"{key}|{win}"] += 1
            payouts[win] += 1
            request_ids.add(str(payload.get("request_id")))

    print(f"rounds={total}")
    print(f"unique_boards={len(boards)}")
    print(f"unique_board_payout={len(outcomes)}")
    print(f"unique_payouts={len(payouts)}")
    print(f"unique_request_ids={len(request_ids)}")
    if total:
        print(f"board_uniqueness={len(boards) / total * 100:.2f}%")

    repeats = [(key, count) for key, count in boards.most_common(5) if count > 1]
    print(f"boards_seen_more_than_once={sum(1 for c in boards.values() if c > 1)}")
    for key, count in repeats:
        print(f"  x{count} {key}")

    wins = sum(count for win, count in payouts.items() if win > 0)
    print(f"winning_rounds={wins} ({wins / total * 100:.2f}%)" if total else "")
    print("top_payouts:")
    for win, count in sorted(payouts.items(), reverse=True)[:5]:
        print(f"  {win} x{count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))