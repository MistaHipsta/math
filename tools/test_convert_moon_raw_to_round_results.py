import io
import json
import tempfile
import unittest
from pathlib import Path

import convert_moon_raw_to_round_results as converter


class ConvertMoonRawTests(unittest.TestCase):
    def make_event(
        self,
        *,
        round_win: object = 0,
        total_win: object = 12,
        last_win: object = 34,
        board: object | None = None,
        winlines: object | None = None,
    ) -> dict[str, object]:
        spins: dict[str, object] = {
            "board": board if board is not None else [[1, 2, 3], [4, 5, 6]],
            "round_bet": 100,
        }
        if round_win != "missing":
            spins["round_win"] = round_win
        if total_win != "missing":
            spins["total_win"] = total_win

        context: dict[str, object] = {
            "spins": spins,
            "last_win": last_win,
        }
        if winlines is not None:
            spins["winlines"] = winlines

        return {
            "type": "round_response",
            "sessionId": "session-1",
            "response": {
                "CorrelationId": "corr-1",
                "RawJson": json.dumps(
                    {"command": "play", "context": context},
                    ensure_ascii=False,
                ),
            },
        }

    def write_events(self, directory: str, events: list[object]) -> Path:
        path = Path(directory) / "MoonSisters-worker-01-test.jsonl"
        path.write_text(
            "\n".join(
                event if isinstance(event, str) else json.dumps(event)
                for event in events
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_compact_winlines_and_book_key_order(self) -> None:
        winlines = [
            {
                "line": 7,
                "amount": 25,
                "positions": [[0, 1], [2, 0], ["3", 2]],
                "symbol": "Ж",
            }
        ]

        book = converter.build_book([[1]], winlines, 25)

        self.assertEqual(list(book), ["board", "winLines", "payout"])
        self.assertEqual(
            book["winLines"],
            [
                {
                    "id": "7",
                    "amount": 25,
                    "positions": [[0, 1], [2, 0], ["3", 2]],
                    "symbol": "Ж",
                }
            ],
        )
        self.assertEqual(
            json.dumps(book, ensure_ascii=False, separators=(",", ":")),
            '{"board":[[1]],"winLines":[{"id":"7","amount":25,"positions":[[0,1],[2,0],["3",2]],"symbol":"Ж"}],"payout":25}',
        )

    def test_artifact_fallback_payout_and_invalid_payout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_events(
                directory,
                [
                    self.make_event(
                        round_win="missing",
                        total_win="missing",
                        last_win=34,
                    ),
                    self.make_event(
                        round_win="missing",
                        total_win=-1,
                        last_win=34,
                    ),
                    self.make_event(
                        round_win="missing",
                        total_win="34",
                        last_win=34,
                    ),
                    self.make_event(round_win=True, total_win=34, last_win=34),
                ],
            )
            stream = io.StringIO()
            stats: dict[str, int] = {}
            converted, malformed = converter.convert_file(
                path,
                stream,
                stats=stats,
            )

        self.assertEqual((converted, malformed), (1, 0))
        self.assertEqual(stats["oversized"], 0)
        self.assertEqual(json.loads(stream.getvalue())["payout"], 34)

    def test_legacy_format_keeps_round_result_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_events(
                directory,
                [
                    self.make_event(
                        round_win=0,
                        winlines=[
                            {
                                "line": "L1",
                                "amount": 10,
                                "positions": [[0, 0]],
                                "symbol": "7",
                            }
                        ],
                    )
                ],
            )
            stream = io.StringIO()
            converted, malformed = converter.convert_file(
                path,
                stream,
                format="round_result",
            )

        self.assertEqual((converted, malformed), (1, 0))
        record = json.loads(stream.getvalue())
        self.assertEqual(
            list(record),
            ["schemaVersion", "type", "sessionId", "result"],
        )
        self.assertEqual(record["type"], "round_result")
        self.assertEqual(record["result"]["Payout"], 0)
        self.assertEqual(record["result"]["WinLines"][0]["Id"], "L1")

    def test_oversized_artifact_is_skipped_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_events(
                directory,
                [
                    self.make_event(
                        board=[[1]],
                        winlines=[
                            {
                                "line": 1,
                                "amount": 1,
                                "positions": [[0, 0]],
                                "symbol": "Ж" * 50000,
                            }
                        ],
                    )
                ],
            )
            stream = io.StringIO()
            stats: dict[str, int] = {}
            converted, malformed = converter.convert_file(
                path,
                stream,
                stats=stats,
            )

        self.assertEqual((converted, malformed), (0, 0))
        self.assertEqual(stats["oversized"], 1)
        self.assertEqual(stream.getvalue(), "")

    def test_malformed_and_non_play_records_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_events(
                directory,
                [
                    "{ malformed",
                    {"type": "session_started"},
                    {
                        "type": "round_response",
                        "response": {
                            "RawJson": json.dumps({"command": "start"}),
                        },
                    },
                ],
            )
            stream = io.StringIO()
            converted, malformed = converter.convert_file(path, stream)

        self.assertEqual((converted, malformed), (0, 1))


if __name__ == "__main__":
    unittest.main()