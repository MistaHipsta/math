import argparse
import csv
import json
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


STATUS_NAMES = {
    0: "Unparsed",
    1: "Partial",
    2: "Parsed",
    3: "Error",
}


def field(result: dict[str, Any], name: str, default: Any = None) -> Any:
    """Read either System.Text.Json PascalCase or normalized camelCase."""
    return result.get(name, result.get(name[:1].lower() + name[1:], default))


def decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
    return None


def status_name(value: Any) -> str:
    if isinstance(value, str):
        return value
    return STATUS_NAMES.get(value, "Unknown")


def deterministic_symbols(result: dict[str, Any]) -> str:
    symbols = field(result, "Symbols", []) or []
    if not isinstance(symbols, list):
        return "[]"

    normalized = []
    for symbol in symbols:
        if not isinstance(symbol, dict):
            normalized.append(symbol)
            continue
        normalized.append(
            {
                "symbol": field(symbol, "Symbol"),
                "reel": field(symbol, "Reel"),
                "column": field(symbol, "Column"),
                "row": field(symbol, "Row"),
                "index": field(symbol, "Index"),
                "rawValue": field(symbol, "RawValue"),
            }
        )
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def feature_text(result: dict[str, Any]) -> str:
    feature = field(result, "Feature")
    if not isinstance(feature, dict):
        return ""
    normalized = {
        "isBonus": field(feature, "IsBonus"),
        "isFreeSpin": field(feature, "IsFreeSpin"),
        "name": field(feature, "Name"),
        "freeSpinsRemaining": field(feature, "FreeSpinsRemaining"),
        "status": field(feature, "Status"),
    }
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def is_full_board_5x3(result: dict[str, Any]) -> bool:
    symbols = field(result, "Symbols", []) or []
    if not isinstance(symbols, list) or len(symbols) != 15:
        return False

    positions = {
        (field(symbol, "Reel"), field(symbol, "Row"))
        for symbol in symbols
        if isinstance(symbol, dict)
    }
    return positions == {(reel, row) for reel in range(5) for row in range(3)}


def load_records(path: str | Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0

    with open(path, encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue

            if not isinstance(event, dict) or event.get("type") != "round_result":
                continue
            result = event.get("result")
            if isinstance(result, dict):
                records.append(result)
            else:
                malformed += 1

    return records, malformed


def record_view(result: dict[str, Any], index: int) -> dict[str, Any]:
    round_id = field(result, "RoundId")
    correlation_id = field(result, "CorrelationId")
    safe_id = round_id or correlation_id or f"record-{index}"
    symbols = field(result, "Symbols", []) or []
    return {
        "index": index,
        "id": safe_id,
        "roundId": round_id,
        "roundIdSource": field(result, "RoundIdSource"),
        "correlationId": correlation_id,
        "timestampUtc": field(result, "TimestampUtc"),
        "stake": decimal(field(result, "Stake")),
        "payout": decimal(field(result, "Payout")),
        "symbolCount": len(symbols) if isinstance(symbols, list) else 0,
        "fullBoard5x3": is_full_board_5x3(result),
        "symbols": deterministic_symbols(result),
        "feature": feature_text(result),
        "status": status_name(field(result, "Status")),
    }


def analyze(path: str | Path) -> dict[str, Any]:
    results, malformed = load_records(path)
    views = [record_view(result, index) for index, result in enumerate(results, start=1)]

    status_counts = Counter(view["status"] for view in views)
    error_count = status_counts["Error"]

    valid = [
        view
        for view in views
        if view["status"] == "Parsed"
        and view["stake"] is not None
        and view["stake"] >= 0
        and view["payout"] is not None
        and view["payout"] >= 0
    ]
    total_stake = sum((view["stake"] for view in valid), Decimal(0))
    total_payout = sum((view["payout"] for view in valid), Decimal(0))
    rtp = None if total_stake == 0 else total_payout / total_stake

    symbol_counts: Counter[str] = Counter()
    feature_counts: Counter[str] = Counter()
    feature_state_counts: Counter[str] = Counter()
    for result, view in zip(results, views):
        if view["status"] != "Parsed":
            continue
        for symbol in field(result, "Symbols", []) or []:
            if isinstance(symbol, dict):
                value = field(symbol, "Symbol")
                if value is not None:
                    symbol_counts[str(value)] += 1
        feature = field(result, "Feature")
        if isinstance(feature, dict):
            name = field(feature, "Name")
            if name:
                feature_counts[str(name)] += 1
            state = feature_text(result)
            if state:
                feature_state_counts[state] += 1

    return {
        "file": Path(path).name,
        "round_results": len(views),
        "parsed": status_counts["Parsed"],
        "partial": status_counts["Partial"],
        "unparsed": status_counts["Unparsed"],
        "error": error_count,
        "malformed_jsonl": malformed,
        "total_stake": total_stake,
        "total_payout": total_payout,
        "rtp": rtp,
        "symbols": dict(sorted(symbol_counts.items())),
        "features": dict(sorted(feature_counts.items())),
        "feature_states": dict(sorted(feature_state_counts.items())),
        "full_board_5x3": sum(
            1 for view in views
            if view["status"] == "Parsed" and view["fullBoard5x3"]
        ),
        "rounds": views,
    }


def json_number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return json_number(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    return value


def json_report(report: dict[str, Any]) -> dict[str, Any]:
    return json_safe(report)


def format_decimal(value: Decimal | None) -> str:
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def print_report(report: dict[str, Any]) -> None:
    print(f"file={report['file']}")
    print(f"round_results={report['round_results']}")
    print(f"parsed={report['parsed']}")
    print(f"partial={report['partial']}")
    print(f"unparsed={report['unparsed']}")
    print(f"error={report['error']}")
    print(f"malformed_jsonl={report['malformed_jsonl']}")
    print(f"full_board_5x3={report['full_board_5x3']}")
    print(f"total_stake={report['total_stake']:g}")
    print(f"total_payout={report['total_payout']:g}")
    if report["rtp"] is None:
        print("rtp=null")
    else:
        print(f"rtp={report['rtp']:g}")

    print("per_round:")
    print("index,id,stake,payout,symbol_count,symbols,feature,status")
    for view in report["rounds"]:
        stake = format_decimal(view["stake"])
        payout = format_decimal(view["payout"])
        print(
            f"{view['index']},{view['id']},{stake},{payout},"
            f"{view['symbolCount']},{view['symbols']},{view['feature']},{view['status']}"
        )


def export_csv(path: str | Path, output: str | Path, report: dict[str, Any]) -> None:
    field_names = [
        "index",
        "id",
        "roundId",
        "roundIdSource",
        "correlationId",
        "timestampUtc",
        "stake",
        "payout",
        "symbolCount",
        "fullBoard5x3",
        "symbols",
        "feature",
        "status",
    ]
    with open(output, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=field_names)
        writer.writeheader()
        for view in report["rounds"]:
            row = dict(view)
            row["stake"] = json_number(row["stake"])
            row["payout"] = json_number(row["payout"])
            writer.writerow(row)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze safe per-round SlotAutoPlay JSONL records."
    )
    parser.add_argument("jsonl", help="Path to a SlotAutoPlay JSONL file")
    parser.add_argument("--csv", dest="csv_path", help="Export per-round records to CSV")
    parser.add_argument("--json", dest="json_path", help="Export aggregate/per-round report to JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    report = analyze(args.jsonl)
    print_report(report)

    if args.csv_path:
        export_csv(args.jsonl, args.csv_path, report)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as stream:
            json.dump(
                json_report(report),
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())