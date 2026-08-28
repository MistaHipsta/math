import argparse
import json
from pathlib import Path
from typing import Any, TextIO


MAX_ARTIFACT_LINE_BYTES = 96000
FORMATS = ("artifact_book", "round_result")


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


def _winline_positions(line: dict[str, Any]) -> list[list[Any]]:
    raw_positions = get(line, "positions", default=[])
    if not isinstance(raw_positions, list):
        return []

    positions: list[list[Any]] = []
    for item in raw_positions:
        if isinstance(item, list) and len(item) >= 2:
            positions.append([item[0], item[1]])
    return positions


def compact_winlines(winlines: Any) -> list[dict[str, Any]]:
    """Convert backend winlines to the compact MathArtifact representation."""
    if not isinstance(winlines, list):
        return []

    result: list[dict[str, Any]] = []
    for line in winlines:
        if not isinstance(line, dict):
            continue
        result.append(
            {
                "id": str(get(line, "line", default="")),
                "amount": get(line, "amount"),
                "positions": _winline_positions(line),
                "symbol": str(get(line, "symbol", default="")),
            }
        )
    return result


def win_lines_from_payload(winlines: Any) -> list[dict[str, Any]]:
    """Preserve the historical verbose winline representation for legacy output."""
    if not isinstance(winlines, list):
        return []

    result: list[dict[str, Any]] = []
    for line in winlines:
        if not isinstance(line, dict):
            continue
        positions = [
            {
                "Symbol": str(get(line, "symbol", default="")),
                "Reel": item[0],
                "Row": item[1],
            }
            for item in get(line, "positions", default=[])
            if isinstance(item, list) and len(item) >= 2
        ]
        result.append(
            {
                "Id": str(get(line, "line", default="")),
                "Amount": get(line, "amount"),
                "Positions": positions,
            }
        )
    return result


def build_book(
    board: list[list[int]], winlines: Any, payout: int
) -> dict[str, Any]:
    """Build a compact MathArtifact book with deterministic key order."""
    return {
        "board": board,
        "winLines": compact_winlines(winlines),
        "payout": payout,
    }


def _is_integer_board(board: Any) -> bool:
    return (
        isinstance(board, list)
        and all(
            isinstance(column, list)
            and all(isinstance(value, int) and not isinstance(value, bool) for value in column)
            for column in board
        )
    )


def _extract_play(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("type") != "round_response":
        return None

    response = get(event, "response", default={})
    raw = get(response, "RawJson", "rawJson", default="")
    if not isinstance(raw, str):
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if str(get(payload, "command", default="")).lower() != "play":
        return None

    context = get(payload, "context", default={})
    spins = get(context, "spins", default={})
    board = get(spins, "board", default=[])
    stake = get(spins, "round_bet")
    payout = get(spins, "round_win")
    if payout is None:
        payout = get(spins, "total_win")
    if payout is None:
        payout = get(context, "last_win")

    if not isinstance(board, list) or stake is None or payout is None:
        return None

    return {
        "event": event,
        "response": response,
        "payload": payload,
        "context": context,
        "spins": spins,
        "board": board,
        "stake": stake,
        "payout": payout,
        "raw": raw,
    }


def _legacy_result(parsed: dict[str, Any]) -> dict[str, Any]:
    event = parsed["event"]
    response = parsed["response"]
    payload = parsed["payload"]
    context = parsed["context"]
    spins = parsed["spins"]
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
    return {
        "Status": "Parsed",
        "RoundId": correlation_id,
        "CorrelationId": correlation_id,
        "TimestampUtc": timestamp,
        "Stake": parsed["stake"],
        "Payout": parsed["payout"],
        "Balance": get(user, "balance"),
        "Currency": get(user, "currency"),
        "Symbols": positions_from_board(parsed["board"]),
        "WinLines": win_lines_from_payload(get(spins, "winlines", default=[])),
        "Feature": None,
        "ParserName": "moon-sisters-raw",
        "ParserVersion": "1.0",
        "RawJson": parsed["raw"],
        "Error": None,
        "RoundIdSource": "correlationId",
    }


def convert_file(
    path: Path,
    output_stream: TextIO,
    format: str = "artifact_book",
    stats: dict[str, int] | None = None,
) -> tuple[int, int]:
    if format not in FORMATS:
        raise ValueError(f"unsupported format: {format}")

    converted = 0
    malformed = 0
    if stats is not None:
        stats.setdefault("oversized", 0)

    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue

            if not isinstance(event, dict):
                continue
            parsed = _extract_play(event)
            if parsed is None:
                continue

            if format == "artifact_book":
                payout = parsed["payout"]
                if not _is_integer_board(parsed["board"]):
                    continue
                if isinstance(payout, bool) or not isinstance(payout, int) or payout < 0:
                    continue
                value = build_book(
                    parsed["board"],
                    get(parsed["spins"], "winlines", default=[]),
                    payout,
                )
            else:
                value = {
                    "schemaVersion": 1,
                    "type": "round_result",
                    "sessionId": event.get("sessionId"),
                    "result": _legacy_result(parsed),
                }

            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if (
                format == "artifact_book"
                and len(serialized.encode("utf-8")) > MAX_ARTIFACT_LINE_BYTES
            ):
                if stats is not None:
                    stats["oversized"] += 1
                continue
            output_stream.write(serialized + "\n")
            converted += 1

    return converted, malformed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Moon Sisters raw play responses to MathArtifact books."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("output/http-runs"),
        help="Run directory containing MoonSisters worker JSONL files.",
    )
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument(
        "--format",
        choices=FORMATS,
        default="artifact_book",
        help="Output format; artifact_book is the compact default.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSONL path. Defaults to <root>/artifact-books.jsonl.",
    )
    args = parser.parse_args()

    default_name = (
        "artifact-books.jsonl"
        if args.format == "artifact_book"
        else "round-results.jsonl"
    )
    output = args.output or args.root / default_name
    files = load_latest_worker_files(args.root, args.workers)
    output.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    malformed = 0
    stats: dict[str, int] = {"oversized": 0}
    with output.open("w", encoding="utf-8") as stream:
        for path in files:
            converted, invalid = convert_file(
                path, stream, format=args.format, stats=stats
            )
            total += converted
            malformed += invalid
            label = "books" if args.format == "artifact_book" else "round_results"
            print(f"{path.name}: {label}={converted} malformed={invalid}")

    label = "books" if args.format == "artifact_book" else "round_results"
    print(f"files={len(files)}")
    print(f"{label}={total}")
    print(f"malformed_source_lines={malformed}")
    if args.format == "artifact_book":
        print(f"oversized={stats['oversized']}")
    print(f"output={output}")


if __name__ == "__main__":
    main()