import csv
import json
import tempfile
import unittest
from pathlib import Path

import analyze_moon_jsonl as analyzer


class AnalyzeMoonJsonlTests(unittest.TestCase):
    def make_jsonl(self, directory: str) -> Path:
        path = Path(directory) / "rounds.jsonl"
        parsed = {
            "Status": "Parsed",
            "RoundId": "fallback-1",
            "RoundIdSource": "correlationId",
            "CorrelationId": "corr-1",
            "Stake": "1.25",
            "Payout": 0,
            "Symbols": [
                {"Symbol": "7", "Reel": 0, "Row": 0, "Index": 0, "RawValue": "7"}
            ],
            "Feature": None,
        }
        partial = {
            "Status": "Partial",
            "RoundId": "fallback-2",
            "CorrelationId": "corr-2",
            "Stake": 2,
            "Payout": None,
            "Symbols": [],
            "Feature": None,
            "Error": {"Code": "missing_payout", "Message": "missing"},
        }
        path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session_started"}),
                    json.dumps({"type": "round_result", "result": parsed}),
                    "{ malformed",
                    json.dumps({"type": "round_result", "result": partial}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_per_round_and_exports_preserve_zero_and_missing_payout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jsonl = self.make_jsonl(directory)
            csv_path = Path(directory) / "rounds.csv"
            json_path = Path(directory) / "report.json"

            report = analyzer.analyze(jsonl)
            self.assertEqual(report["round_results"], 2)
            self.assertEqual(report["parsed"], 1)
            self.assertEqual(report["partial"], 1)
            self.assertEqual(report["error"], 0)
            self.assertEqual(report["malformed_jsonl"], 1)
            self.assertEqual(report["total_stake"], analyzer.Decimal("1.25"))
            self.assertEqual(report["total_payout"], analyzer.Decimal("0"))
            self.assertEqual(report["rtp"], analyzer.Decimal("0"))
            self.assertEqual(report["rounds"][0]["payout"], analyzer.Decimal("0"))
            self.assertIsNone(report["rounds"][1]["payout"])
            self.assertIn('"reel":0', report["rounds"][0]["symbols"])

            analyzer.export_csv(jsonl, csv_path, report)
            with csv_path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["payout"], "0")
            self.assertEqual(rows[1]["payout"], "")
            self.assertEqual(rows[0]["symbolCount"], "1")
            self.assertNotIn("secret", csv_path.read_text(encoding="utf-8"))

            json_path.write_text(
                json.dumps(analyzer.json_report(report)),
                encoding="utf-8",
            )
            exported = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(exported["rounds"][0]["payout"], 0)
            self.assertIsNone(exported["rounds"][1]["payout"])

    def test_status_and_symbol_serialization_are_deterministic(self) -> None:
        result = {
            "Status": 2,
            "Symbols": [
                {"Symbol": "B", "Reel": 1, "Row": 2, "Index": 4},
                {"Symbol": "A", "Reel": 0, "Row": 0, "Index": 0},
            ],
        }
        first = analyzer.deterministic_symbols(result)
        second = analyzer.deterministic_symbols(result)
        self.assertEqual(first, second)
        self.assertEqual(analyzer.status_name(2), "Parsed")


    def test_decimal_console_format_keeps_integer_zeroes(self) -> None:
        self.assertEqual(analyzer.format_decimal(analyzer.Decimal("100")), "100")
        self.assertEqual(analyzer.format_decimal(analyzer.Decimal("60.50")), "60.5")
        self.assertEqual(analyzer.format_decimal(analyzer.Decimal("0")), "0")
        self.assertEqual(analyzer.format_decimal(None), "")


if __name__ == "__main__":
    unittest.main()