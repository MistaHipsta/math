import argparse
import json
import os
from pathlib import Path
from typing import Any


def get(mapping: Any, *names: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    wanted = {name.lower() for name in names}
    for key, value in mapping.items():
        if str(key).lower() in wanted:
            return value
    return default


def load_latest_worker_files(root: Path, workers: int) -> list[Path]:
    files: list[Path] = []
    for worker in range(1, workers + 1):
        candidates = sorted(
            root.glob(f"MoonSisters-worker-{worker:02d}-*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            files.append(candidates[0])
    return files


def positions_from_board(board: Any) -> list[dict[str, Any]]:
    if not isinstance(board, list):
        return []

    positions: list[dict[str, Any]] = []
    index = 0
    for reel, column in enumerate(board):
        if not isinstance(column, list):
            continue
        for row, value in enumerate(column):
            positions.append(
                {
                    "Symbol": str(value),
                    "Reel": reel,
                    "Row": row,
                    "Index": index,
                    "RawValue": str(value),
                    "Column": reel,
                }
            )
            index += 1
    return positions


def win_lines_from_payload(winlines: Any) -> list[dict[str, Any]]:
    if not isinstance(winlines, list):
        return []

    result: list[dict[str, Any]] = []
    for line in winlines:
        if not isinstance(line, dict):
            continue
        positions = []
        raw_positions = get(line, "positions", default=[])
        if isinstance(raw_positions, list):
            for item in raw_positions:
                if isinstance(item, list) and len(item) >= 2:
                    positions.append(
                        {
                            "Symbol": str(get(line, "symbol", default="")),
                            "Reel": item[0],
                            "Row": item[1],
                        }
                    )
        result.append(
            {
                "Id": str(get(line, "line", default="")),
                "Amount": get(line, "amount"),
                "Positions": positions,
            }
        )
    return result


def convert_file(path: Path, output_stream: Any) -> tuple[int, int]:
    converted = 0
    malformed = 0

    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue

            if not isinstance(event, dict) or event.get("type") != "round_response":
                continue

            response = get(event, "response", default={})
            raw = get(response, "RawJson", "rawJson", default="")
            if not isinstance(raw, str):
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if str(get(payload, "command", default="")).lower() != "play":
                continue

            context = get(payload, "context", default={})
            spins = get(context, "spins", default={})
            board = get(spins, "board", default=[])
            user = get(payload, "user", default={})
            correlation_id = str(
                get(response, "CorrelationId", "correlationId", default="")
            )
            timestamp = get(
                response,
                "TimestampUtc",
                "timestampUtc",
                default=event.get("timestamp"),
            )
            stake = get(spins, "round_bet")
            # In Moon Sisters, context.last_win may remain stale from the
            # previous response. Prefer the payout belonging to this spin.
            payout = get(spins, "round_win")
            if payout is None:
                payout = get(spins, "total_win")
            if payout is None:
                payout = get(context, "last_win")

            if not isinstance(board, list) or stake is None or payout is None:
                continue

            result = {
                "Status": "Parsed",
                "RoundId": correlation_id,
                "CorrelationId": correlation_id,
                "TimestampUtc": timestamp,
                "Stake": stake,
                "Payout": payout,
                "Balance": get(user, "balance"),
                "Currency": get(user, "currency"),
                "Symbols": positions_from_board(board),
                "WinLines": win_lines_from_payload(get(spins, "winlines", default=[])),
                "Feature": None,
                "ParserName": "moon-sisters-raw",
                "ParserVersion": "1.0",
                "RawJson": raw,
                "Error": None,
                "RoundIdSource": "correlationId",
            }
            output_stream.write(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "type": "round_result",
                        "sessionId": event.get("sessionId"),
                        "result": result,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            converted += 1

    return converted, malformed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Moon Sisters raw play responses to round_result JSONL."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("output/slotautoplay/moon-sisters"),
    )
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "output/slotautoplay/moon-sisters/moon-sisters-10000-round-results.jsonl"
        ),
    )
    args = parser.parse_args()

    files = load_latest_worker_files(args.root, args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    malformed = 0
    with args.output.open("w", encoding="utf-8") as stream:
        for path in files:
            converted, invalid = convert_file(path, stream)
            total += converted
            malformed += invalid
            print(f"{path.name}: converted={converted} malformed={invalid}")

    print(f"files={len(files)}")
    print(f"round_results={total}")
    print(f"malformed_source_lines={malformed}")
    print(f"output={args.output}")


if __name__ == "__main__":
    main()