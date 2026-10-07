from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import build_maxwin_artifacts as builder
import collect_maxwin as collector
from collect_moon_browser import Lane, RunState, Transport


def board(symbol: str = "K") -> list[list[str]]:
    return [[symbol] * 15 for _ in range(5)]


def line(win: str, multiplier: int = 1) -> dict:
    return {"winning": win, "symbol": "K", "winCount": 3, "line": [5, 5, 5, 5, 5],
            "multiplier": multiplier}


def free_spins(wins: list[str], start: str = "0.00") -> dict:
    spins, running, total = [], Decimal(start), len(wins)
    for index, win in enumerate(wins):
        before = running
        running += Decimal(win)
        spins.append({"stake": 0.1, "reels": board(), "winning": win,
                      "multiWays": [line(win)] if Decimal(win) else [],
                      "lockedSymbolsOverlay": [{"5": "W3", "6": "W3", "7": "W3",
                                                "8": "W3", "9": "W3"}] if index else [],
                      "winningBefore": f"{before:.2f}", "winningAfter": f"{running:.2f}",
                      "fsCountBefore": total - index, "fsCountAfter": total - index - 1,
                      "totalFreeSpins": total, "totalFreeSpinsAfter": total})
    return {"totalCount": total, "freeSpinWinning": f"{running - Decimal(start):.2f}",
            "spins": spins}


def round_event(mode: str = "base", base_win: str = "0.00",
                bonus_wins: list[str] | None = None) -> dict:
    nsp = {"stake": 0.1, "reels": board(), "winning": base_win,
           "multiWays": [line(base_win)] if Decimal(base_win) else [],
           "lockedSymbolsOverlay": []}
    total = Decimal(base_win)
    if bonus_wins is not None:
        nsp["freeSpins"] = free_spins(bonus_wins, base_win)
        total += sum(Decimal(w) for w in bonus_wins)
    game = {"win": {"total": f"{total:.2f}", "stake": "0.10"}, "state": [], "nsp": nsp}
    return {"type": "round_response", "mode": mode,
            "response": {"stake": "0.1", "game": game}}


class BookTests(unittest.TestCase):
    def test_base_round_and_natural_bonus(self) -> None:
        book, mode, payout, bonus = builder.build_book(round_event(base_win="0.30"), 1)
        self.assertEqual((mode, payout, bonus), ("base", 300, False))
        self.assertEqual(len(book["events"][0]["board"][0]), 5)  # visible rows only

        book, mode, payout, bonus = builder.build_book(
            round_event(base_win="0.10", bonus_wins=["0.00", "0.20", "1.00"]), 2)
        self.assertEqual((mode, payout, bonus), ("base", 1300, True))
        self.assertEqual([e["type"] for e in book["events"]],
                         ["spin", "freeSpin", "freeSpin", "freeSpin"])
        self.assertEqual(book["events"][2]["lockedWilds"],
                         [{"reel": 0, "symbol": "W3", "multiplier": 3}])
        self.assertEqual(book["events"][-1]["spinsLeft"], 0)

    def test_incomplete_or_inconsistent_bonus_is_rejected(self) -> None:
        missing = round_event("bonus", bonus_wins=["0.10", "0.20", "0.30"])
        del missing["response"]["game"]["nsp"]["freeSpins"]["spins"][1]
        self.assertIn("free spins", builder.round_problem(missing))

        cut = round_event("bonus", bonus_wins=["0.10", "0.20", "0.30"])
        cut["response"]["game"]["nsp"]["freeSpins"]["spins"].pop()
        self.assertIsNotNone(builder.round_problem(cut))

        wrong_total = round_event("bonus", bonus_wins=["0.10", "0.20"])
        wrong_total["response"]["game"]["win"]["total"] = "9.99"
        self.assertIn("round win", builder.round_problem(wrong_total))

        bad_line = round_event(base_win="0.30")
        bad_line["response"]["game"]["nsp"]["multiWays"][0]["winning"] = "0.20"
        self.assertIn("line wins", builder.round_problem(bad_line))

        no_bonus = round_event("bonus")
        self.assertIn("no free spins", builder.round_problem(no_bonus))
        self.assertIsNone(builder.round_problem(round_event("bonus", bonus_wins=["0.10"])))


class FakeCompressor:
    def __init__(self, level: int) -> None:
        pass

    def stream_writer(self, target, closefd: bool = True):
        return target


class ConvertTests(unittest.TestCase):
    def test_modes_are_split_and_bad_rounds_dropped(self) -> None:
        good_bonus = round_event("bonus", bonus_wins=["0.50", "1.00"])
        broken = round_event("bonus", bonus_wins=["0.50", "1.00"])
        broken["response"]["game"]["nsp"]["freeSpins"]["spins"].pop()
        rounds = [round_event(base_win="0.20"), round_event(), good_bonus, broken]
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(sys.modules, {"zstandard": types.SimpleNamespace(
                    ZstdCompressor=FakeCompressor)}):
            root = Path(folder)
            (root / "run.json").write_text(json.dumps({
                "roundsCollected": len(rounds), "buyPrice": 100, "gameId": "G",
                "stake": "0.1"}))
            with (root / "G-worker-01-x.jsonl").open("w") as out:
                for event in rounds:
                    out.write(json.dumps(event) + "\n")
                out.write(json.dumps(round_event())[:40])  # cut last line
            report = builder.convert(root)
            stake = root / "converted-artifact" / "stake"
            index = json.loads((stake / "index.json").read_text())
            bonus_books = (stake / "books_bonus.jsonl.zst").read_text().splitlines()
            base_weights = (stake / "lookUpTable_base_0.csv").read_text().splitlines()
            artube_files = sorted(p.name for p in (root / "converted-artifact" / "artube").iterdir())
        self.assertEqual(report["rejectedRounds"], 1)
        self.assertEqual(report["modes"]["base"]["rounds"], 2)
        self.assertEqual(report["modes"]["bonus"]["rounds"], 1)
        self.assertEqual([m["name"] for m in index["modes"]], ["base", "bonus"])
        self.assertEqual(index["modes"][1]["cost"], 100.0)
        self.assertEqual(json.loads(bonus_books[0])["payoutMultiplier"], 1500)
        self.assertEqual(base_weights, ["1,1,200", "2,1,0"])
        self.assertIn("books_bonus.jsonl.zst", artube_files)


class RealZstdTests(unittest.TestCase):
    def test_mode_books_decompress_to_every_round(self) -> None:
        import zstandard

        rounds = [round_event(base_win="0.20")] * 2000 + \
            [round_event("bonus", bonus_wins=["0.50", "1.00"])] * 2000
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "run.json").write_text(json.dumps({
                "roundsCollected": len(rounds), "buyPrice": 100, "gameId": "G", "stake": "0.1"}))
            with (root / "G-worker-01-x.jsonl").open("w") as out:
                for base, bonus in zip(rounds[:2000], rounds[2000:]):  # modes interleaved
                    out.write(json.dumps(base) + "\n" + json.dumps(bonus) + "\n")
            builder.convert(root)
            for platform in ("stake", "artube"):
                for mode in ("base", "bonus"):
                    raw = (root / "converted-artifact" / platform /
                           f"books_{mode}.jsonl.zst").read_bytes()
                    text = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
                    self.assertEqual(text.count(b"\n"), 2000, (platform, mode))


class CollectorTests(unittest.TestCase):
    def test_target_and_api_are_read_from_the_page(self) -> None:
        target = collector.parse_target(collector.DEFAULT_URL)
        self.assertEqual((target["gameId"], target["vertical"]), ("BanditGambit", "r3_rtp96"))
        html = "var apiEndpoint = cond ? 'https://lab/x/' : '//games.example.com/backend/casino/Dummy/';"
        self.assertEqual(collector.parse_api(html),
                         "https://games.example.com/backend/casino/Dummy/game/")
        self.assertEqual(collector.parse_body('<br/>Warning<br/>{"success":true}'),
                         {"success": True})

    def test_modes_are_interleaved_and_released(self) -> None:
        state = collector.ModeState({"base": 4, "bonus": 2})
        claimed = [state.claim() for _ in range(6)]
        self.assertEqual(sorted(claimed), ["base"] * 4 + ["bonus"] * 2)
        self.assertEqual(claimed[:2], ["base", "bonus"])
        self.assertIsNone(state.claim())
        state.release("bonus")
        self.assertEqual(state.claim(), "bonus")

    def session(self) -> collector.MaxwinSession:
        target = {"gameId": "G", "vertical": "v", "feature": "FreeSpins"}
        session = collector.MaxwinSession(Lane(None, 1000.0, 1), None, "page", "https://x/game/",
                                          target, "fp")
        session.token, session.user_id = "secret-token", 1
        session.last_game = {"previous": True}
        return session

    def run_round(self, answers: list, history_game: dict | None):
        session = self.session()
        sent: list[str] = []
        migrations: list[str] = []

        async def fake_send(session_, path, body, args, state, log):
            sent.append(path)
            if path == "history/list":
                responses = [{"game": history_game, "user": {"balance": {"cash": {"atEnd": "5"}}}}]
                return {"result": {"history": {"responses": responses}}}
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        async def migrate(reason: str) -> None:
            migrations.append(reason)

        args = collector.parse_args(["--stake", "0.1"])
        with patch.object(collector, "send", fake_send):
            entry = asyncio.run(collector.play_round(session, "base", migrate, args,
                                                     RunState(1), lambda _: None))
        return entry, sent, migrations, session

    def ok(self, total: str) -> dict:
        game = round_event(base_win=total)["response"]["game"]
        return {"result": {"game": game, "transactions": {"roundId": 7},
                           "user": {"balance": {"cash": {"atEnd": "9"}}}}}

    def test_lost_spin_that_was_played_is_read_back_not_replayed(self) -> None:
        played = round_event(base_win="0.40")["response"]["game"]
        entry, sent, migrations, session = self.run_round([Transport("proxy died")], played)
        self.assertEqual(sent, ["spin", "history/list"])  # never sent twice
        self.assertTrue(entry["recovered"])
        self.assertEqual(entry["game"], played)
        self.assertEqual(session.last_game, played)
        self.assertEqual(len(migrations), 1)

    def test_lost_spin_that_never_arrived_is_sent_again(self) -> None:
        entry, sent, _, _ = self.run_round([Transport("proxy died"), self.ok("0.20")],
                                           {"previous": True})
        self.assertEqual(sent, ["spin", "history/list", "spin"])
        self.assertFalse(entry["recovered"])
        self.assertEqual(entry["roundId"], 7)

    def test_record_never_contains_the_token(self) -> None:
        session = self.session()
        entry = {"game": round_event()["response"]["game"], "roundId": 1,
                 "recovered": False, "balanceAtEnd": "1"}
        record = collector.round_record("label", session, "base", "0.1", entry)
        self.assertNotIn("secret-token", json.dumps(record))
        self.assertIsNone(builder.round_problem(copy.deepcopy(record)))


if __name__ == "__main__":
    unittest.main()
