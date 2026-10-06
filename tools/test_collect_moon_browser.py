from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

import build_moon_artifacts as builder
import collect_moon_browser as collector


def context(action: str, finished: bool, board_symbol: int, total_win: int = 0) -> dict:
    board = [[board_symbol, board_symbol, 2] for _ in range(5)]
    zeros = [[0, 0, 0] for _ in range(5)]
    return {"last_action": action, "round_finished": finished,
            "actions": ["spin"] if finished else ["respin"],
            "spins": {"board": board, "bs_values": zeros, "bs_v": zeros,
                      "round_bet": 100, "round_win": total_win,
                      "total_win": total_win, "winlines": []}}


def step(action: str, ctx: dict) -> dict:
    return {"action": action,
            "request": {"request_id": f"id-{action}", "session_id": "[redacted]"},
            "response": {"command": "play", "status": {"code": "OK"},
                         "context": ctx, "user": {"balance": 1000}}}


class MergeRoundTests(unittest.TestCase):
    def test_plain_spin_is_kept_verbatim(self) -> None:
        steps = [step("spin", context("spin", True, 1, 40))]
        raw, history = collector.merge_round(steps)
        self.assertEqual(json.loads(raw), steps[0]["response"])
        self.assertEqual(len(history), 1)

    def test_bonus_keeps_trigger_board_and_final_win(self) -> None:
        steps = [step("spin", context("spin", False, 1)),
                 step("respin", context("respin", False, 3)),
                 step("bonus_spins_stop", context("bonus_spins_stop", True, 4, 900))]
        raw, history = collector.merge_round(steps)
        spins = json.loads(raw)["context"]["spins"]
        self.assertEqual(spins["board"][0][0], 1)
        self.assertEqual(spins["round_win"], 900)
        self.assertEqual(spins["bonus_steps"], 2)
        self.assertEqual([h["last_action"] for h in history],
                         ["spin", "respin", "bonus_spins_stop"])

    def test_record_is_accepted_by_artifact_builder(self) -> None:
        steps = [step("spin", context("spin", False, 1)),
                 step("bonus_spins_stop", context("bonus_spins_stop", True, 4, 900))]
        record = collector.round_record("w", "https://x/desktop/abcdef123456/demo/", steps)
        self.assertNotIn("abcdef123456", record["response"]["Url"])
        book, payout, bonus = builder.build_book(record, 1, 100)
        self.assertEqual(payout, 900)
        self.assertTrue(bonus)


class BackoffTests(unittest.TestCase):
    def test_retry_after_is_honoured_above_cap(self) -> None:
        self.assertGreaterEqual(collector.backoff_delay(0, 2, 300, "2954"), 2954)

    def test_exponential_is_capped(self) -> None:
        self.assertLessEqual(collector.backoff_delay(20, 2, 300), 300 * 1.25)


class BonusResumeTests(unittest.TestCase):
    """A lost connection mid-bonus resends the same request after migrating."""

    def run_round(self, fail_on: set[int]):
        args = collector.parse_args([])
        state = collector.RunState(total=1)
        lane = collector.Lane("socks5://1.1.1.1:1080", 1.0, 1)
        session = collector.GameSession(lane, None, None, "https://x/", "sid", 1000)
        script = [
            context("spin", False, 1) | {"actions": ["bonus_init"]},
            context("bonus_init", False, 1),
            context("respin", False, 1),
            context("respin", False, 1) | {"actions": ["bonus_spins_stop"]},
            context("bonus_spins_stop", True, 1, 700),
        ]
        sent: list[tuple[str, str]] = []
        calls = {"n": 0}
        migrations: list[bool] = []

        async def fake_send(session_, payload, *_):
            calls["n"] += 1
            sent.append((payload["action"]["name"], payload["request_id"]))
            if calls["n"] in fail_on:
                raise collector.Transport("proxy died")
            return {"status": {"code": "OK"}, "context": script.pop(0),
                    "user": {"balance": 1700}}

        async def migrate(reason: str, mid_bonus: bool) -> None:
            migrations.append(mid_bonus)
            session.lane = collector.Lane("socks5://2.2.2.2:1080", 1.0, 1)

        original = collector.send_play
        collector.send_play = fake_send
        try:
            steps = asyncio.run(collector.play_round(session, migrate, args, state, print))
        finally:
            collector.send_play = original
        return steps, sent, migrations

    def test_failure_mid_bonus_is_resumed_with_same_request(self) -> None:
        steps, sent, migrations = self.run_round(fail_on={3})
        self.assertEqual([s["action"] for s in steps],
                         ["spin", "bonus_init", "respin", "respin", "bonus_spins_stop"])
        self.assertEqual(sent[2], sent[3])  # same respin, same request_id
        self.assertEqual(migrations, [True])
        self.assertEqual(steps[2]["egress"], "2.2.2.2:1080")
        self.assertEqual(steps[0]["egress"], "1.1.1.1:1080")

    def test_lost_spin_answer_is_resent_not_skipped(self) -> None:
        steps, sent, migrations = self.run_round(fail_on={1})
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(migrations, [False])
        self.assertEqual(len(steps), 5)

    def test_wrong_step_answer_is_not_recorded(self) -> None:
        args = collector.parse_args([])
        lane = collector.Lane(None, 1.0, 1)
        session = collector.GameSession(lane, None, None, "https://x/", "sid", 1000)
        answers = [context("spin", False, 1) | {"actions": ["bonus_init"]},
                   context("respin", False, 1)]

        async def fake_send(*_):
            return {"status": {"code": "OK"}, "context": answers.pop(0)}

        original = collector.send_play
        collector.send_play = fake_send
        try:
            with self.assertRaises(collector.SessionLost) as caught:
                asyncio.run(collector.play_round(
                    session, None, args, collector.RunState(1), print))
        finally:
            collector.send_play = original
        self.assertIn("step mismatch", str(caught.exception))
        self.assertEqual(len(caught.exception.partial_steps), 1)


class StandbyTests(unittest.TestCase):
    """A warm standby page takes over a bonus when the session lane dies."""

    def test_standby_takes_over_and_resends_same_step(self) -> None:
        args = collector.parse_args(["--proxies", "x"])
        state = collector.RunState(total=1)
        pool = collector.LanePool(None, args)
        primary = collector.Lane("socks5://10.0.3.1:1080", 1000.0, 1)
        standby = collector.Lane("socks5://10.0.3.2:1080", 1000.0, 1)
        primary.in_use = standby.in_use = 1
        pool.lanes = [primary, standby]
        session = collector.GameSession(primary, None, "primary-page", "https://x/", "sid", 1000)
        hedges = collector.HedgePool(None, pool, args, state, lambda _: None)
        hedges.ready.append((standby, None, "standby-page"))
        hedges.top_up = lambda _lane: None  # no real browser in this test
        script = [
            context("spin", False, 1) | {"actions": ["bonus_init"]},
            context("bonus_init", False, 1),
            context("bonus_spins_stop", True, 1, 300),
        ]
        sent: list[tuple[str, str, str]] = []

        async def post(page, endpoint, payload, timeout):
            sent.append((page, payload["action"]["name"], payload["request_id"]))
            if page == "primary-page" and len(sent) == 2:
                return 0, None, None  # session lane dies on the first bonus step
            ctx = script.pop(0)
            if payload["action"]["name"] == "bonus_init":
                ctx = ctx | {"actions": ["bonus_spins_stop"]}
            return 200, {"status": {"code": "OK"}, "context": ctx}, None

        async def no_migrate(*_):
            raise AssertionError("standby should have taken over")

        original = collector.post
        collector.post = post
        args.proxy_retries = 0
        try:
            steps = asyncio.run(collector.play_round(
                session, no_migrate, args, state, lambda _: None, hedges))
        finally:
            collector.post = original
        self.assertEqual([s["action"] for s in steps], ["spin", "bonus_init", "bonus_spins_stop"])
        self.assertEqual(sent[1][1:], sent[2][1:])  # same step, same request_id
        self.assertEqual([p for p, _, _ in sent], ["primary-page", "primary-page",
                                                  "standby-page", "standby-page"])
        self.assertEqual(steps[1]["egress"], "10.0.3.2:1080")
        self.assertEqual(hedges.promotions, 1)
        self.assertEqual(primary.in_use, 0)

    def test_steps_are_never_sent_in_parallel(self) -> None:
        self.assertFalse(hasattr(collector, "send_hedged"))


class LaneTests(unittest.TestCase):
    def test_workers_on_one_ip_share_its_budget(self) -> None:
        lane = collector.Lane(None, 20.0, capacity=6)
        state = collector.RunState(total=10)

        async def run() -> float:
            started = time.monotonic()
            await asyncio.gather(*(lane.acquire(state) for _ in range(6)))
            return time.monotonic() - started

        # Six requests at 20/s need about five intervals of 50 ms.
        self.assertGreaterEqual(asyncio.run(run()), 0.18)

    def test_throttle_pauses_only_that_lane(self) -> None:
        banned = collector.Lane("socks5://1.1.1.1:1080", 1.0, 1)
        other = collector.Lane("socks5://2.2.2.2:1080", 1.0, 1)
        banned.throttle(2, 300, "60")
        now = time.monotonic()
        self.assertFalse(banned.available(now))
        self.assertTrue(other.available(now))

    def test_proxy_dies_after_failures_but_direct_never(self) -> None:
        proxy = collector.Lane("socks5://1.1.1.1:1080", 1.0, 1)
        direct = collector.Lane(None, 1.0, 1)
        for _ in range(2):
            proxy.failed(2)
            direct.failed(2)
        self.assertTrue(proxy.dead)
        self.assertFalse(direct.dead)

    def test_lease_now_prefers_proven_lanes(self) -> None:
        args = collector.parse_args(["--proxies", "x"])
        pool = collector.LanePool(None, args)
        fresh, proven = (collector.Lane(f"socks5://10.0.1.{i}:1080", 1.0, 1) for i in range(2))
        proven.rounds = 5
        pool.lanes = [fresh, proven]
        self.assertEqual(pool.lease_now(2), [proven, fresh])
        self.assertEqual(pool.lease_now(2), [])

    def test_rescue_shares_busy_proven_lanes_only(self) -> None:
        args = collector.parse_args(["--proxies", "x"])
        pool = collector.LanePool(None, args)
        busy_proven, busy_fresh, dead = (
            collector.Lane(f"socks5://10.0.2.{i}:1080", 1.0, 1) for i in range(3))
        for lane in (busy_proven, busy_fresh, dead):
            lane.in_use = 1
        busy_proven.rounds = dead.rounds = 3
        dead.dead = True
        pool.lanes = [busy_proven, busy_fresh, dead]
        self.assertEqual(pool.lease_now(4), [])
        self.assertEqual(pool.lease_now(4, rescue=True), [busy_proven])
        self.assertEqual(busy_proven.in_use, 2)

    def test_lease_skips_busy_dead_and_cooling_lanes(self) -> None:
        args = collector.parse_args(["--proxies", "x"])
        pool = collector.LanePool(None, args)
        busy, dead, cooling, free = (
            collector.Lane(f"socks5://10.0.0.{i}:1080", 1.0, 1) for i in range(4))
        busy.in_use = 1
        dead.dead = True
        cooling.cooldown_until = time.monotonic() + 60
        pool.lanes = [busy, dead, cooling, free]
        leased = asyncio.run(pool.lease(collector.RunState(total=1)))
        self.assertIs(leased, free)
        self.assertEqual(free.in_use, 1)


class LaunchAndProxyListTests(unittest.TestCase):
    def test_parse_launch_builds_desktop_endpoint(self) -> None:
        html = ('{"server_url": "//b.example/gs/moon/desktop/{QUEUE}/demo/"},'
                '{"server_url": "//b.example/gs/moon/mobile/{QUEUE}/demo/"},'
                '{"queue": "abc123", "token": "tok456"}')
        endpoint, token = collector.parse_launch(html)
        self.assertEqual(endpoint, "https://b.example/gs/moon/desktop/abc123/demo/")
        self.assertEqual(token, "tok456")

    def test_parse_launch_rejects_challenge_page(self) -> None:
        with self.assertRaises(collector.SessionLost):
            collector.parse_launch("<html>Just a moment...</html>")

    def test_load_proxy_list_formats(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            as_json = Path(folder) / "p.json"
            as_json.write_text(json.dumps([
                {"proxy": "socks5://1.2.3.4:1080"},
                {"proxy": "socks5://1.2.3.4:1080"},
                {"proxy": "ftp://1.2.3.4:21"},
            ]), encoding="utf-8")
            as_text = Path(folder) / "p.txt"
            as_text.write_text("5.6.7.8:3128\nsocks4://9.9.9.9:4145\nbad\n", encoding="utf-8")
            self.assertEqual(collector.load_proxy_list(str(as_json)),
                             ["socks5://1.2.3.4:1080"])
            self.assertEqual(collector.load_proxy_list(str(as_text)),
                             ["http://5.6.7.8:3128", "socks4://9.9.9.9:4145"])


if __name__ == "__main__":
    unittest.main()
