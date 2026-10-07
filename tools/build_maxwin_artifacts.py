"""Build Stake and Artube math artifacts from `collect_maxwin.py` JSONL.

One book per round. A bonus round keeps every free spin in order: board,
locked wilds with their multipliers, win lines and the running total.
Two modes: `base` (plain spins, natural bonuses included) and `bonus`
(bought free spins, cost = buy price x stake).

Payouts are integers in hundredths of the stake (x100), the unit Stake
lookup tables use; the visible window is rows 5..9 of each 15-symbol reel
strip, the rows the win lines refer to.

A round is rejected, never written, when its bonus is incomplete or does
not add up: free-spin count, counters, running totals and the sum of line
wins must all agree with the round win.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

MODES = ("base", "bonus")
VISIBLE = slice(5, 10)
MAX_LINE_BYTES = 96000


def iter_rounds(root: Path) -> Iterator[dict[str, Any]]:
    for path in sorted(root.glob("*-worker-*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    break  # cut by a killed collector: not a finished round
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") == "round_response":
                    yield event


def money(value: Any) -> Decimal:
    return Decimal(str(value))


def x100(amount: Decimal, stake: Decimal) -> int:
    """Win as hundredths of the stake; must be exact."""
    scaled = amount / stake * 100
    if scaled != scaled.to_integral_value():
        raise ValueError(f"win {amount} is not a whole hundredth of stake {stake}")
    return int(scaled)


def visible(reels: Any) -> list[list[str]]:
    if (not isinstance(reels, list) or len(reels) != 5
            or any(not isinstance(r, list) or len(r) < 10 for r in reels)):
        raise ValueError("board is not 5 reels of at least 10 symbols")
    return [list(reel[VISIBLE]) for reel in reels]


def overlay(locked: Any) -> list[dict[str, Any]]:
    """Locked wilds as [{reel, multiplier}] from {"reel": {"row": "W5", ...}}.

    PHP serialises keys 0..n-1 as a JSON list, so reels starting at 0 arrive
    as a list indexed by reel.
    """
    if not locked:
        return []
    if isinstance(locked, list):
        locked = {str(reel): rows for reel, rows in enumerate(locked) if rows}
    wilds = []
    for reel, rows in sorted(locked.items(), key=lambda item: int(item[0])):
        if isinstance(rows, list):
            rows = {str(row): symbol for row, symbol in enumerate(rows) if symbol}
        values = set(rows.values())
        if len(values) != 1:
            raise ValueError("a locked wild reel shows mixed multipliers")
        symbol = values.pop()
        wilds.append({"reel": int(reel), "symbol": symbol,
                      "multiplier": int(symbol[1:]) if symbol[1:].isdigit() else None})
    return wilds


def lines_of(spin: dict[str, Any], stake: Decimal) -> tuple[list[dict[str, Any]], Decimal]:
    out, total = [], Decimal(0)
    for line in spin.get("multiWays") or []:
        win = money(line["winning"])
        total += win
        out.append({"symbol": line["symbol"], "count": line["winCount"],
                    "line": line["line"], "multiplier": line["multiplier"],
                    "payout": x100(win, stake)})
    return out, total


def spin_event(kind: str, spin: dict[str, Any], stake: Decimal,
               index: int | None = None) -> tuple[dict[str, Any], Decimal]:
    win_lines, line_total = lines_of(spin, stake)
    win = money(spin.get("winning") or 0)
    if line_total != win:
        raise ValueError(f"{kind} line wins {line_total} != spin win {win}")
    event = {"type": kind, "board": visible(spin.get("reels")),
             "lockedWilds": overlay(spin.get("lockedSymbolsOverlay")),
             "winLines": win_lines, "payout": x100(win, stake)}
    if index is not None:
        event.update({"index": index, "spinsLeft": spin.get("fsCountAfter"),
                      "totalSpins": spin.get("totalFreeSpinsAfter")})
    return event, win


def round_problem(event: dict[str, Any]) -> str | None:
    """Why a round must stay out of the artifacts, or None if it is complete."""
    try:
        build_book(event, 0)
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        return f"{type(error).__name__}: {error}"[:120]
    return None


def build_book(event: dict[str, Any], book_id: int) -> tuple[dict[str, Any], str, int, bool]:
    response = event["response"]
    mode = event.get("mode")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    stake = money(response["stake"])
    game = response["game"]
    nsp = game["nsp"]
    if money(nsp.get("stake")) != stake or money(game["win"]["stake"]) != stake:
        raise ValueError("round stake differs from the run stake")

    base, base_win = spin_event("spin", nsp, stake)
    events = [base]
    free = nsp.get("freeSpins")
    bonus = bool(free)
    total = base_win
    if mode == "bonus" and not bonus:
        raise ValueError("bought round has no free spins")
    if bonus:
        spins = free.get("spins") or []
        expected = free.get("totalCount")
        if not spins or len(spins) != (spins[-1].get("totalFreeSpinsAfter") or expected):
            raise ValueError(f"bonus has {len(spins)} of {expected} free spins")
        running = base_win
        for index, spin in enumerate(spins, 1):
            if spin.get("fsCountBefore", 0) - 1 != spin.get("fsCountAfter"):
                raise ValueError(f"free spin {index} counter does not step by one")
            if money(spin.get("winningBefore")) != running:
                raise ValueError(f"free spin {index} starts from {spin.get('winningBefore')}, "
                                 f"expected {running}")
            fs_event, win = spin_event("freeSpin", spin, stake, index)
            running += win
            if money(spin.get("winningAfter")) != running:
                raise ValueError(f"free spin {index} running total does not add up")
            events.append(fs_event)
        if spins[-1].get("fsCountAfter") != 0:
            raise ValueError("bonus ended with free spins left")
        if money(free.get("freeSpinWinning")) + base_win != running and \
                money(free.get("freeSpinWinning")) != running:
            raise ValueError("bonus win does not match its free spins")
        total = running
    if money(game["win"]["total"]) != total:
        raise ValueError(f"round win {game['win']['total']} != sum of spins {total}")

    payout = x100(total, stake)
    book = {"id": book_id, "events": events, "payoutMultiplier": payout}
    return book, mode, payout, bonus


def write_line(stream: Any, value: dict[str, Any]) -> None:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode("utf-8")) > MAX_LINE_BYTES:
        raise ValueError("artifact line exceeds 96 KB limit")
    stream.write(data + "\n")


def convert(root: Path) -> dict[str, Any]:
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise RuntimeError("Install requirements-artifacts.txt to create .zst artifacts") from exc
    manifest = json.loads((root / "run.json").read_text(encoding="utf-8"))
    expected = manifest.get("roundsCollected")
    if type(expected) is not int or expected <= 0:
        raise ValueError("run.json does not contain completed round count")
    buy_price = manifest.get("buyPrice") or 100
    parent = root / "converted-artifact"
    destinations = {name: parent / name for name in ("stake", "artube")}
    parent.mkdir(parents=True, exist_ok=True)
    for destination in destinations.values():
        if destination.exists():
            raise FileExistsError(f"Output already exists: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix="maxwin-convert-", dir=parent))
    per_mode = {mode: {"rounds": 0, "bonusRounds": 0, "payoutTotal": 0, "maxPayout": 0}
                for mode in MODES}
    summary: dict[str, Any] = {"game": manifest.get("gameId"),
                               "mathName": manifest.get("mathName"),
                               "stake": manifest.get("stake"), "rejectedRounds": 0,
                               "rejectedReasons": {}, "modes": per_mode,
                               "payoutUnit": "hundredths of the stake (x100)"}
    stack = ExitStack()
    try:
        books, weights, ids = {}, {}, dict.fromkeys(MODES, 0)
        for name in destinations:
            (temporary / name).mkdir()
        for mode in MODES:
            target = stack.enter_context((temporary / "stake" / f"books_{mode}.jsonl.zst").open("wb"))
            # One compressor per stream: a shared one corrupts interleaved outputs.
            books[mode] = stack.enter_context(io.TextIOWrapper(
                zstd.ZstdCompressor(level=3).stream_writer(target, closefd=False),
                encoding="utf-8", newline="\n"))
            weights[mode] = csv.writer(stack.enter_context(
                (temporary / f"weights_{mode}.csv").open("w", encoding="utf-8", newline="")),
                lineterminator="\n")
        for event in iter_rounds(root):
            try:
                book, mode, payout, bonus = build_book(event, ids.get(event.get("mode"), 0) + 1)
            except (KeyError, TypeError, ValueError, ArithmeticError) as error:
                reason = f"{type(error).__name__}: {error}"[:120]
                summary["rejectedRounds"] += 1
                summary["rejectedReasons"][reason] = summary["rejectedReasons"].get(reason, 0) + 1
                continue
            ids[mode] += 1
            write_line(books[mode], book)
            weights[mode].writerow((book["id"], 1, payout))
            stats = per_mode[mode]
            stats["rounds"] += 1
            stats["bonusRounds"] += int(bonus)
            stats["payoutTotal"] += payout
            stats["maxPayout"] = max(stats["maxPayout"], payout)
        stack.close()
        seen = sum(s["rounds"] for s in per_mode.values()) + summary["rejectedRounds"]
        if seen != expected:
            raise ValueError(f"run.json says {expected} rounds, but found {seen}")
        modes_index = []
        for mode in MODES:
            stats = per_mode[mode]
            cost = 1.0 if mode == "base" else float(buy_price)
            stats["cost"] = cost
            stats["rtp"] = (stats["payoutTotal"] / (stats["rounds"] * 100 * cost)
                            if stats["rounds"] else None)
            if not stats["rounds"]:
                (temporary / "stake" / f"books_{mode}.jsonl.zst").unlink()
                continue
            os.replace(temporary / f"weights_{mode}.csv",
                       temporary / "stake" / f"lookUpTable_{mode}_0.csv")
            modes_index.append({"name": mode, "cost": cost,
                                "events": f"books_{mode}.jsonl.zst",
                                "weights": f"lookUpTable_{mode}_0.csv"})
        for name in destinations:
            folder = temporary / name
            if name == "artube":
                for item in modes_index:
                    for key in ("events", "weights"):
                        shutil.copy2(temporary / "stake" / item[key], folder / item[key])
            (folder / "index.json").write_text(
                json.dumps({"modes": modes_index}, indent=2) + "\n", encoding="utf-8")
            (folder / "manifest.json").write_text(json.dumps({
                "format": f"maxwin-{name}-v1", "game": manifest.get("gameId"),
                "mathName": manifest.get("mathName"), "stake": manifest.get("stake"),
                "payoutMultiplierUnit": summary["payoutUnit"],
                "modes": {m: {k: per_mode[m][k] for k in ("rounds", "bonusRounds", "cost")}
                          for m in MODES if per_mode[m]["rounds"]}}, indent=2) + "\n",
                encoding="utf-8")
        (temporary / "audit-report.json").write_text(json.dumps(summary, indent=2) + "\n",
                                                     encoding="utf-8")
        for name, destination in destinations.items():
            os.replace(temporary / name, destination)
        shutil.copy2(temporary / "audit-report.json", parent / "audit-report.json")
        return summary
    finally:
        with __import__("contextlib").suppress(Exception):
            stack.close()
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(convert(args.root), indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"artifact conversion failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
