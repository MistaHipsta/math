"""Build streaming Stake and Mooncoin/GBR math artifacts from collector JSONL."""
from __future__ import annotations

import argparse
import csv
from contextlib import ExitStack
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

SYMBOL_IDS = {1: "jack", 2: "queen", 3: "king", 4: "ace", 5: "planet",
              6: "crystals", 7: "meteor", 8: "rocket", 9: "wild", 10: "coin"}
MAX_LINE_BYTES = 96000


def iter_rounds(root: Path, workers: int) -> Iterator[tuple[Path, dict[str, Any]]]:
    for worker in range(1, workers + 1):
        candidates = sorted(root.glob(f"MoonSisters-worker-{worker:02d}-*.jsonl"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        if len(candidates) > 1:
            raise ValueError(f"More than one source file for worker {worker}; select/clean the run explicitly")
        if not candidates:
            raise ValueError(f"Missing source file for worker {worker}")
        with candidates[0].open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") == "round_response":
                    yield candidates[0], event


def is_matrix(value: Any) -> bool:
    return (isinstance(value, list) and len(value) == 5
            and all(isinstance(col, list) and len(col) == 3 for col in value))


def clean_history(event: dict[str, Any], payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    history = event.get("response", {}).get("playHistory")
    if (isinstance(history, list) and history
            and all(isinstance(item, dict) and isinstance(item.get("spins"), dict) for item in history)):
        return history, True
    # Compatibility for previously collected rows. Their bonus history is gone.
    context = payload.get("context")
    return ([context] if isinstance(context, dict) else []), False


def build_book(event: dict[str, Any], expected_id: int,
               expected_bet: int) -> tuple[dict[str, Any], int, bool]:
    payload = json.loads(event["response"]["RawJson"])
    if payload.get("command") != "play":
        raise ValueError("non-play response in a completed round")
    final_context = payload.get("context") or {}
    spins = final_context.get("spins") or {}
    history, history_available = clean_history(event, payload)
    initial = history[0]
    initial_spins = initial.get("spins") or {}
    board = initial_spins.get("board")
    if final_context.get("round_finished") is not True or not is_matrix(board):
        raise ValueError("round is unfinished or has invalid 5x3 starting board")
    if any(type(n) is not int or n not in SYMBOL_IDS for col in board for n in col):
        raise ValueError("unknown symbol ID in board")
    bet = spins.get("round_bet")
    payout = spins.get("round_win", spins.get("total_win"))
    if type(bet) is not int or bet <= 0 or type(payout) is not int or payout < 0:
        raise ValueError("round bet/payout must be nonnegative integer credits")
    if bet != expected_bet:
        raise ValueError(f"round bet {bet} does not match run settings {expected_bet}")
    if type(spins.get("bonus_steps", 0)) is not int:
        raise ValueError("invalid bonus action count")

    bonus = bool(spins.get("bonus_steps", 0)) or len(history) > 1
    artifact_events = []
    for step, context in enumerate(history):
        state = context.get("spins") or {}
        current_board = state.get("board")
        if not is_matrix(current_board) or any(type(n) is not int or n not in SYMBOL_IDS for col in current_board for n in col):
            raise ValueError(f"invalid board at playHistory step {step}")
        values, amounts = state.get("bs_values"), state.get("bs_v")
        if not is_matrix(values) or not is_matrix(amounts):
            raise ValueError(f"coin matrices missing at playHistory step {step}")
        for reel in range(5):
            for row in range(3):
                value, amount = values[reel][row], amounts[reel][row]
                if type(value) is not int or value < 0:
                    raise ValueError("invalid coin denomination")
                if type(amount) is int:
                    if amount != value * bet:
                        raise ValueError("coin credit amount does not match denomination and round bet")
                elif (amount, value) not in {("mini", 30), ("major", 150), ("grand", 1000)}:
                    raise ValueError("unknown jackpot coin marker")
                if ((current_board[reel][row] == 10) != (value > 0)):
                    raise ValueError("board and coin matrix disagree in captured step")
        action = context.get("last_action", "spin" if step == 0 else None)
        if not isinstance(action, str):
            raise ValueError(f"playHistory step {step} has no action name")
        win_lines = state.get("winlines", [])
        if not isinstance(win_lines, list):
            raise ValueError("winlines must be an array")
        artifact_events.append({"action": action, "board": current_board,
                                "winLines": win_lines, "coinValues": values,
                                "coinAmounts": amounts,
                                "roundWin": state.get("round_win", 0),
                                "totalWin": state.get("total_win", 0),
                                "roundFinished": context.get("round_finished")})

    base_win_lines = artifact_events[0]["winLines"]
    history_complete = history_available or not bonus
    if bonus and not history_available:
        last = artifact_events[-1]
        # Older collector output keeps the trigger board and final coin values in
        # one response. Label it a summary and do not bind those values to cells.
        artifact_events = [{"action": "bonusSummaryOnly", "board": board,
                            "winLines": base_win_lines,
                            "coinValues": None, "coinAmounts": None,
                            "collectedCoinValues": last["coinValues"],
                            "collectedCoinAmounts": last["coinAmounts"],
                            "roundWin": payout, "totalWin": payout,
                            "roundFinished": True,
                            "historyAvailable": False}]
    # Stake imports standard API-shaped books; keep round detail in each generic event.
    stake_book = {"id": expected_id, "events": artifact_events,
                  "payoutMultiplier": payout}
    # GBR payload keeps the complete chronological game event stream intact.
    artube_book = {"id": expected_id,
                   "events": [{"type": "mooncoinRound", "board": board,
                               "winLines": base_win_lines,
                               "coinValues": artifact_events[0]["coinValues"] if history_complete else None,
                               "coinAmounts": artifact_events[0]["coinAmounts"] if history_complete else None,
                               "bonusTriggered": bonus,
                               "historyAvailable": history_complete,
                               "actions": artifact_events}],
                   "payoutMultiplier": payout}
    return {"stake": stake_book, "artube": artube_book,
            "payout": payout, "bet": bet, "bonus": bonus,
            "historyAvailable": history_available}, payout, bonus


def write_line(stream: Any, value: dict[str, Any]) -> None:
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode("utf-8")) > MAX_LINE_BYTES:
        raise ValueError("artifact line exceeds 96 KB limit")
    stream.write(data + "\n")


def convert(root: Path, workers: int) -> dict[str, Any]:
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise RuntimeError("Install requirements-artifacts.txt to create .zst artifacts") from exc
    manifest_path = root / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_count = manifest.get("roundsCollected")
    if type(expected_count) is not int or expected_count <= 0:
        raise ValueError("run.json does not contain completed round count")
    bet_settings = manifest.get("bet") or {}
    bet_fields = [bet_settings.get("betPerLine"), bet_settings.get("lines"),
                  bet_settings.get("setDenominator", 1)]
    if any(type(value) is not int or value <= 0 for value in bet_fields):
        raise ValueError("run.json contains invalid bet configuration")
    expected_bet = bet_fields[0] * bet_fields[1] * bet_fields[2]
    parent = root / "converted-artifact"
    destinations = {name: parent / name for name in ("stake", "artube")}
    parent.mkdir(parents=True, exist_ok=True)
    for destination in destinations.values():
        if destination.exists():
            raise FileExistsError(f"Output already exists: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix="moon-convert-", dir=parent))
    outputs: dict[str, Any] = {}
    compressor = zstd.ZstdCompressor(level=3)
    summary = {"rounds": 0, "bonusRounds": 0, "incompleteLegacyBonusRounds": 0,
               "stakePayoutTotal": 0, "artubePayoutTotal": 0,
               "lookupRows": 0,
               "betCredits": expected_bet,
               "schemaVersion": 2, "payoutDistribution": {}, "maxPayout": 0}
    try:
        stack = ExitStack()
        for name in destinations:
            (temporary / name).mkdir()
            target = stack.enter_context((temporary / name / "books_base.jsonl.zst").open("wb"))
            encoded = compressor.stream_writer(target, closefd=False)
            outputs[name] = stack.enter_context(__import__("io").TextIOWrapper(encoded, encoding="utf-8", newline="\n"))
        csv_tmp = temporary / "weights.csv"
        with csv_tmp.open("w", encoding="utf-8", newline="") as weight_file:
            weights = csv.writer(weight_file, lineterminator="\n")
            for index, (_, event) in enumerate(iter_rounds(root, workers), 1):
                record, payout, bonus = build_book(event, index, expected_bet)
                write_line(outputs["stake"], record["stake"])
                write_line(outputs["artube"], record["artube"])
                weights.writerow((index, 1, payout))
                summary["rounds"] += 1
                summary["bonusRounds"] += int(bonus)
                summary["incompleteLegacyBonusRounds"] += int(bonus and not record["historyAvailable"])
                summary["stakePayoutTotal"] += payout
                summary["artubePayoutTotal"] += payout
                summary["payoutDistribution"][str(payout)] = summary["payoutDistribution"].get(str(payout), 0) + 1
                summary["maxPayout"] = max(summary["maxPayout"], payout)
        stack.close()
        outputs.clear()
        if summary["rounds"] != expected_count:
            raise ValueError(f"run.json says {expected_count} rounds, but converted {summary['rounds']}")
        summary["totalStake"] = summary["rounds"] * summary["betCredits"]
        summary["lookupRows"] = summary["rounds"]
        summary["rtp"] = summary["stakePayoutTotal"] / summary["totalStake"] if summary["totalStake"] else None
        for platform in destinations:
            folder = temporary / platform
            if platform == "stake":
                os.replace(csv_tmp, folder / "lookUpTable_base_0.csv")
            else:
                shutil.copy2(folder.parent / "stake" / "lookUpTable_base_0.csv", folder / "lookUpTable_base_0.csv")
            (folder / "index.json").write_text(json.dumps({"modes": [{"name": "base", "cost": 1.0,
                "events": "books_base.jsonl.zst", "weights": "lookUpTable_base_0.csv"}]}, indent=2) + "\n", encoding="utf-8")
            (folder / "manifest.json").write_text(json.dumps({"format": f"mooncoin-{platform}-v2",
                "rounds": summary["rounds"], "betCredit": summary["betCredits"],
                "payoutMultiplierUnit": "integer credit units; not GBR currency amounts",
                "bonusRounds": summary["bonusRounds"],
                "incompleteBonusRounds": summary["incompleteLegacyBonusRounds"],
                "bonusHistoryComplete": summary["incompleteLegacyBonusRounds"] == 0,
                "symbolIds": {str(key): value for key, value in SYMBOL_IDS.items()}}, indent=2) + "\n", encoding="utf-8")
            (folder / "symbol-map.json").write_text(json.dumps(
                {str(key): value for key, value in SYMBOL_IDS.items()}, indent=2) + "\n", encoding="utf-8")
        (temporary / "audit-report.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        for platform, destination in destinations.items():
            os.replace(temporary / platform, destination)
        shutil.copy2(temporary / "audit-report.json", parent / "audit-report.json")
        return summary
    finally:
        try:
            stack.close()
        except Exception:
            pass
        shutil.rmtree(temporary, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    try:
        print(json.dumps(convert(args.root, args.workers), indent=2))
    except Exception as exc:
        print(f"artifact conversion failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
