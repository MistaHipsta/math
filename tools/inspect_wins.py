"""Compare win fields to find the authoritative payout for a round."""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path


def main(run_dir: str) -> int:
    rows = []
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
            rows.append(
                {
                    "bet": spins.get("round_bet"),
                    "round_win": spins.get("round_win"),
                    "total_win": spins.get("total_win"),
                    "last_win": context.get("last_win"),
                    "bonus": spins.get("bonus_steps", 0),
                    "balance": (payload.get("user") or {}).get("balance"),
                }
            )

    bet = sum(r["bet"] or 0 for r in rows)
    print(f"rounds={len(rows)} total_bet={bet}")
    for name in ("round_win", "total_win", "last_win"):
        total = sum(r[name] or 0 for r in rows)
        print(f"sum_{name}={total} rtp={total / bet * 100:.2f}%")

    bonus = [r for r in rows if r["bonus"]]
    plain = [r for r in rows if not r["bonus"]]
    print(f"bonus_rounds={len(bonus)} plain_rounds={len(plain)}")
    for label, subset in (("plain", plain), ("bonus", bonus)):
        if not subset:
            continue
        sub_bet = sum(r["bet"] or 0 for r in subset)
        for name in ("round_win", "total_win", "last_win"):
            total = sum(r[name] or 0 for r in subset)
            print(
                f"{label}.{name}={total} "
                f"rtp={total / sub_bet * 100:.2f}%"
            )

    print("sample_wins (bet, round_win, total_win, last_win, bonus):")
    shown = 0
    for row in rows:
        if (row["round_win"] or 0) > 0 or row["bonus"]:
            print(
                f"  {row['bet']}, {row['round_win']}, {row['total_win']}, "
                f"{row['last_win']}, {row['bonus']}"
            )
            shown += 1
            if shown >= 12:
                break

    # Balance delta is the ground truth: it already nets bet and payout.
    balances = [r["balance"] for r in rows if r["balance"] is not None]
    if len(balances) > 1:
        print(f"balance_first={balances[0]} balance_last={balances[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))