from __future__ import annotations

import csv
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import build_moon_artifacts as builder


def board(symbol: int = 1) -> list[list[int]]:
    return [[symbol, symbol, 2] for _ in range(5)]


def matrix(value: int = 0) -> list[list[int]]:
    return [[value, value, value] for _ in range(5)]


def ctx(action: str, *, finished: bool, coin: bool = False) -> dict:
    b = board()
    values, amounts = matrix(), matrix()
    if coin:
        b[2][1], values[2][1], amounts[2][1] = 10, 5, 500
    return {"last_action": action, "round_finished": finished,
            "spins": {"board": b, "bs_values": values, "bs_v": amounts,
                      "round_bet": 100, "round_win": 0, "total_win": 0,
                      "winlines": []}}


class FakeCompressor:
    def __init__(self, level: int) -> None:
        pass

    def stream_writer(self, target, closefd: bool = True):
        return target


class BuildMoonArtifactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.zstandard = types.SimpleNamespace(ZstdCompressor=FakeCompressor)
        self.modules = patch.dict(sys.modules, {"zstandard": self.zstandard})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.root.mkdir()

    def write_round(self, history: list[dict], *, payout: int, bonus_steps: int) -> dict:
        final = history[-1]
        final_spins = dict(final["spins"])
        final_spins.update(round_bet=100, round_win=payout, total_win=payout,
                           bonus_steps=bonus_steps)
        response = {"command": "play", "context": dict(final, spins=final_spins)}
        return {"type": "round_response", "response": {
            "RawJson": json.dumps(response), "playHistory": history}}

    def write_source(self, rounds: list[dict]) -> None:
        (self.root / "run.json").write_text(json.dumps({
            "roundsCollected": len(rounds), "bet": {"betPerLine": 4, "lines": 25,
            "setDenominator": 1}, "workerStats": [{"file": "MoonSisters-worker-01-test.jsonl"}]}))
        with (self.root / "MoonSisters-worker-01-test.jsonl").open("w") as out:
            for record in rounds:
                out.write(json.dumps(record) + "\n")

    def test_full_history_outputs_two_independent_artifacts(self) -> None:
        base = self.write_round([ctx("spin", finished=True, coin=True)], payout=0, bonus_steps=0)
        history = [ctx("spin", finished=False), ctx("bonus_init", finished=False),
                   ctx("respin", finished=False, coin=True), ctx("bonus_spins_stop", finished=True, coin=True)]
        for context, offered in zip(history, ["bonus_init", "respin", "bonus_spins_stop", "spin"]):
            context["actions"] = [offered]
        bonus = self.write_round(history, payout=3200, bonus_steps=3)
        self.write_source([base, bonus])

        report = builder.convert(self.root, workers=1)
        self.assertEqual(report["rounds"], 2)
        self.assertEqual(report["bonusRounds"], 1)
        self.assertEqual(report["incompleteLegacyBonusRounds"], 0)
        for platform in ("stake", "artube"):
            folder = self.root / "converted-artifact" / platform
            self.assertTrue((folder / "books_base.jsonl.zst").is_file())
            self.assertTrue((folder / "lookUpTable_base_0.csv").is_file())
            self.assertTrue((folder / "index.json").is_file())
            books = [json.loads(line) for line in (folder / "books_base.jsonl.zst").read_text().splitlines()]
            self.assertEqual([item["id"] for item in books], [1, 2])
            self.assertEqual([item["payoutMultiplier"] for item in books], [0, 3200])
            self.assertEqual(len(books[1]["events"] if platform == "stake" else books[1]["events"][0]["actions"]), 4)
            self.assertEqual(books[0]["events"][0]["coinValues"][2][1], 5)
            with (folder / "lookUpTable_base_0.csv").open(newline="", encoding="utf-8") as weights:
                self.assertEqual(list(csv.reader(weights)),
                                 [["1", "1", "0"], ["2", "1", "3200"]])
        stake = json.loads((self.root / "converted-artifact/stake/books_base.jsonl.zst").read_text().splitlines()[1])
        artube = json.loads((self.root / "converted-artifact/artube/books_base.jsonl.zst").read_text().splitlines()[1])
        self.assertEqual(stake["events"][-1]["action"], "bonus_spins_stop")
        self.assertEqual(artube["events"][0]["actions"][2]["coinValues"][2][1], 5)

    def bonus_history(self) -> list[dict]:
        history = [ctx("spin", finished=False), ctx("bonus_init", finished=False),
                   ctx("respin", finished=False), ctx("bonus_spins_stop", finished=True)]
        for context, offered in zip(history, ["bonus_init", "respin", "bonus_spins_stop", "spin"]):
            context["actions"] = [offered]
        return history

    def test_incomplete_bonus_never_reaches_artifacts(self) -> None:
        base = self.write_round([ctx("spin", finished=True)], payout=40, bonus_steps=0)
        complete = self.write_round(self.bonus_history(), payout=3200, bonus_steps=3)
        skipped = self.bonus_history()
        del skipped[2]  # a respin is missing from the middle of the chain
        gap = self.write_round(skipped, payout=900, bonus_steps=2)
        unfinished = self.bonus_history()[:3]
        cut = self.write_round(unfinished, payout=0, bonus_steps=2)
        cut["response"]["RawJson"] = json.dumps({"command": "play", "context": dict(
            unfinished[-1], spins=dict(unfinished[-1]["spins"], bonus_steps=2))})
        self.write_source([base, complete, gap, cut])

        report = builder.convert(self.root, workers=1)
        self.assertEqual(report["rounds"], 2)
        self.assertEqual(report["bonusRounds"], 1)
        self.assertEqual(report["rejectedIncompleteRounds"], 2)
        self.assertEqual(report["stakePayoutTotal"], 3240)
        books = (self.root / "converted-artifact/stake/books_base.jsonl.zst").read_text().splitlines()
        self.assertEqual([json.loads(b)["payoutMultiplier"] for b in books], [40, 3200])
        self.assertEqual([json.loads(b)["id"] for b in books], [1, 2])
        weights = (self.root / "converted-artifact/stake/lookUpTable_base_0.csv").read_text().splitlines()
        self.assertEqual(weights, ["1,1,40", "2,1,3200"])

    def test_bonus_with_respins_left_is_rejected(self) -> None:
        history = self.bonus_history()
        history[2].update({"current": "bonus", "bonus": {"rounds_left": 2}})
        event = self.write_round(history, payout=500, bonus_steps=3)
        self.assertEqual(builder.incomplete_round(event), "bonus ended with respins left")

    def test_cut_last_line_is_ignored(self) -> None:
        base = self.write_round([ctx("spin", finished=True)], payout=40, bonus_steps=0)
        self.write_source([base])
        with (self.root / "MoonSisters-worker-01-test.jsonl").open("a") as out:
            out.write(json.dumps(base)[:50])
        self.assertEqual(builder.convert(self.root, workers=1)["rounds"], 1)

    def test_live_bonus_state_is_taken_from_context_bonus(self) -> None:
        trigger = ctx("spin", finished=False)
        respin = ctx("respin", finished=False)
        live_board = board(3)
        live_board[2][1] = 10
        values, amounts = matrix(), matrix()
        values[2][1], amounts[2][1] = 5, 500
        respin.update({"current": "bonus", "bonus": {
            "board": live_board, "bs_values": values, "bs_v": amounts,
            "rounds_left": 2, "bs_count": 1, "new_bs": [[2, 1]],
            "round_win": 0, "total_win": 0}})
        stop = ctx("bonus_spins_stop", finished=True)
        stop["current"] = "spins"
        round_ = self.write_round([trigger, respin, stop], payout=500, bonus_steps=2)
        book, _, _ = builder.build_book(round_, 1, 100)
        event = book["stake"]["events"][1]
        self.assertEqual(event["phase"], "bonus")
        self.assertEqual(event["board"][2][1], 10)
        self.assertEqual(event["coinValues"][2][1], 5)
        self.assertEqual(event["respinsLeft"], 2)
        self.assertEqual(event["newCoins"], [[2, 1]])
        self.assertNotIn("phase", book["stake"]["events"][0])

    def test_existing_run_without_saved_history_is_marked_incomplete(self) -> None:
        legacy = self.write_round([ctx("bonus_spins_stop", finished=True, coin=True)], payout=3000, bonus_steps=4)
        legacy["response"].pop("playHistory")
        self.write_source([legacy])
        report = builder.convert(self.root, workers=1)
        self.assertEqual(report["incompleteLegacyBonusRounds"], 1)
        book = json.loads((self.root / "converted-artifact/artube/books_base.jsonl.zst").read_text().splitlines()[0])
        self.assertIsNone(book["events"][0]["coinValues"])
        self.assertFalse(book["events"][0]["historyAvailable"])
        self.assertEqual(book["events"][0]["actions"][0]["action"], "bonusSummaryOnly")
        self.assertEqual(book["events"][0]["actions"][0]["collectedCoinValues"][2][1], 5)


if __name__ == "__main__":
    unittest.main()
