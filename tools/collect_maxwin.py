"""MaxWin Gaming (Dopamine platform) round collector, e.g. Bandit Gambit.

The only input is the public game URL. Proxy pool, per-IP rate limits and
browser handling are shared with `collect_moon_browser.py`.

Protocol: POST <api>/game/settings with no token returns a fresh demo player
(token, 1000 balance, stakes, featureBuy); POST <api>/game/spin plays one
round. A whole bonus, every free spin of it, comes back inside that single
spin answer, so a round is atomic: recorded in full or not at all.

Two modes are collected in one run: `base` (plain spins, natural bonuses
included) and `bonus` (free spins bought through
extras.features.featureBuy, 100x stake).

Spins carry no request id, so a lost answer is never resent blindly: the
session moves to another lane and the player's latest round is read back
from game/history/list. If it is new, it is the lost round and is recorded;
if not, the spin never happened and is sent again. A spin whose outcome
cannot be read back is counted as abandoned and never written as a round.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from collect_moon_browser import (
    PROXY_SOURCES, TRANSIENT_STATUS, BrowserPool, Lane, LanePool, Reporter,
    RunState, SessionLost, Throttled, Transport, backoff_delay, iso_now,
    open_page, throttle,
)

DEFAULT_URL = ("https://lobby.maxwingaming.com/games/BanditGambit/play"
               "?vertical=r3_rtp96&hasRegulations=false")
API_MARKERS = ("/backend/",)
MODES = ("base", "bonus")

FETCH_JSON_JS = """
async ([url, body, timeoutMs]) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body,
      signal: controller.signal,
    });
    return {status: response.status, text: await response.text(),
            retryAfter: response.headers.get('retry-after')};
  } catch (error) {
    return {status: 0, text: String(error), retryAfter: null};
  } finally {
    clearTimeout(timer);
  }
}
"""


class OutOfFunds(SessionLost):
    """The demo balance cannot pay for the next round; open a new player."""


class Unresolved(SessionLost):
    """A spin's answer was lost and its outcome could not be read back."""


def parse_target(url: str) -> dict[str, str]:
    """gameId and vertical (math/RTP variant) from the public game URL."""
    parts = urlsplit(url)
    match = re.search(r"/games/([^/]+)/play", parts.path)
    if not match:
        raise SystemExit(f"Not a MaxWin game URL: {url}")
    vertical = (parse_qs(parts.query).get("vertical") or [""])[0]
    return {"url": url, "gameId": match.group(1), "vertical": vertical}


def parse_api(html: str) -> str:
    """The casino API base the game page embeds, ending in `game/`."""
    found = re.findall(r"""['"]((?:https?:)?//[^'"]+/backend/casino/[^'"]+/)['"]""", html)
    if not found:
        raise SessionLost("game page has no API endpoint")
    base = found[0]
    return ("https:" + base if base.startswith("//") else base) + "game/"


def parse_body(text: str) -> dict[str, Any] | None:
    """JSON answer; the backend may print PHP warnings before it."""
    start = text.find("{")
    if start < 0:
        return None
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class ModeState(RunState):
    """Round budget split by mode; modes are interleaved to finish together."""

    def __init__(self, totals: dict[str, int]) -> None:
        super().__init__(sum(totals.values()))
        self.totals = {mode: count for mode, count in totals.items() if count > 0}
        self.claimed_by = dict.fromkeys(self.totals, 0)
        self.done_by = dict.fromkeys(self.totals, 0)

    def claim(self) -> str | None:  # type: ignore[override]
        if self.stop:
            return None
        open_modes = [m for m in self.totals if self.claimed_by[m] < self.totals[m]]
        if not open_modes:
            return None
        mode = min(open_modes, key=lambda m: self.claimed_by[m] / self.totals[m])
        self.claimed_by[mode] += 1
        self.claimed += 1
        return mode

    def release(self, mode: str) -> None:  # type: ignore[override]
        self.claimed_by[mode] -= 1
        self.claimed -= 1

    def complete(self, mode: str) -> int:  # type: ignore[override]
        self.done_by[mode] += 1
        return super().complete()


class MaxwinSession:
    def __init__(self, lane: Lane, context: Any, page: Any, api: str,
                 target: dict[str, Any], fingerprint: str) -> None:
        self.lane = lane
        self.context = context
        self.page = page
        self.api = api
        self.target = target
        self.fingerprint = fingerprint
        self.token = ""
        self.user_id: Any = None
        self.balance = Decimal(0)
        self.last_game: dict[str, Any] | None = None
        self.migrations = 0

    def common(self) -> dict[str, Any]:
        return {
            "token": self.token, "sessionId": 0, "playMode": "demo",
            "gameId": self.target["gameId"],
            "userData": {"userId": self.user_id, "affiliate": "", "lang": "en",
                         "channel": "I", "userType": "U",
                         "fingerprint": self.fingerprint},
            "custom": {"siteId": "", "vertical": self.target["vertical"]},
        }

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.context.close()


def settings_body(target: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    return {
        "token": None, "sessionId": "0", "playMode": "demo",
        "gameId": target["gameId"],
        "userData": {"userId": "demo-user", "hash": "", "affiliate": "", "lang": "en",
                     "channel": "I", "userType": "U", "fingerprint": fingerprint},
        "custom": {"siteId": "", "vertical": target["vertical"]},
    }


def spin_body(session: MaxwinSession, stake: Decimal, mode: str) -> dict[str, Any]:
    extras = None
    if mode == "bonus":
        extras = {"features": {"featureBuy": session.target["feature"],
                               "featureBuyCost": float(stake)}}
    return {**session.common(), "stake": float(stake), "bonusId": None,
            "extras": extras, "gameMode": 0}


async def call(page: Any, url: str, body: dict[str, Any],
               timeout: float) -> tuple[int, dict[str, Any] | None, str | None]:
    result = await page.evaluate(FETCH_JSON_JS, [
        url, json.dumps(body, separators=(",", ":")), int(timeout * 1000)])
    data = parse_body(result["text"]) if result["status"] == 200 else None
    if data is None and result["status"] == 200:
        data = {"success": False, "error": {"msg": result["text"][:160]}}
    return result["status"], data, result["retryAfter"]


async def send(session: MaxwinSession, path: str, body: dict[str, Any],
               args: argparse.Namespace, state: RunState, log: Any) -> dict[str, Any]:
    """POST one API call. A spin is never retried here: it may have been played."""
    lane = session.lane
    retries = 0 if path == "spin" else (
        args.max_retries if lane.server is None else args.proxy_retries)
    attempt = 0
    while True:
        await lane.acquire(state)
        if state.stop:
            raise SessionLost("stopped")
        try:
            status, data, retry_after = await call(session.page, session.api + path,
                                                   body, args.timeout)
        except Exception as error:  # noqa: BLE001 - page or browser died
            raise Transport(f"page: {error}".splitlines()[0]) from error
        if status == 429:
            raise Throttled(retry_after, throttle(lane, state, args, retry_after))
        if status == 200 and data is not None:
            if data.get("success"):
                return data
            error = data.get("error") or {}
            if error.get("code") == 5:
                raise OutOfFunds(f"{path}: insufficient funds")
            raise SessionLost(f"{path} rejected: {error.get('msg') or error}"[:200])
        reason = f"http_{status}"
        if status not in TRANSIENT_STATUS:
            raise SessionLost(reason)
        if attempt >= retries:
            raise Transport(f"{path} failed ({reason})")
        delay = backoff_delay(attempt, args.backoff, args.max_backoff)
        attempt += 1
        log(f"{lane.label}: {path} {reason}, retry {attempt}/{retries} in {delay:.1f}s")
        await state.nap(delay)


async def open_session(browsers: BrowserPool, lane: Lane, args: argparse.Namespace,
                       state: RunState, target: dict[str, Any]) -> MaxwinSession:
    """Load the game page through the lane and create a fresh demo player."""
    await lane.acquire(state, args.session_cost)
    context, page, html = await open_page(browsers, lane, args, state, allow=API_MARKERS)
    session = MaxwinSession(lane, context, page, "", target, str(uuid.uuid4()))
    try:
        session.api = parse_api(html)
        answer = await send(session, "settings", settings_body(target, session.fingerprint),
                            args, state, lambda _: None)
        user = answer["result"]["user"]
        game = answer["result"].get("game") or {}
        session.token, session.user_id = user["token"], user["userId"]
        session.balance = Decimal(str(user["balance"]["cash"]))
        buys = game.get("featureBuy") or []
        target.setdefault("mathName", game.get("mathName"))
        target.setdefault("feature", buys[0]["name"] if buys else None)
        target.setdefault("buyPrice", buys[0]["price"] if buys else None)
        stakes = (user.get("stakes") or {}).get("types") or []
        if stakes and Decimal(args.stake) not in {Decimal(s) for s in stakes}:
            raise SystemExit(f"--stake {args.stake} is not offered; stakes: {', '.join(stakes)}")
        return session
    except BaseException:
        await session.close()
        raise


async def resolve_lost(session: MaxwinSession, migrate: Any, args: argparse.Namespace,
                       state: RunState, log: Any) -> dict[str, Any] | None:
    """The round a lost spin produced (history entry), or None if it was not played.

    history/list returns the player's latest round; one session is driven by
    one worker, so a latest round that differs from the last known one can
    only be the lost spin.
    """
    for _ in range(args.resolve_attempts):
        try:
            answer = await send(session, "history/list", session.common(), args, state, log)
        except Transport as error:
            await migrate(f"history: {error}")
            continue
        history = (answer.get("result") or {}).get("history") or {}
        responses = history.get("responses") or []
        latest = responses[0] if responses else None
        if latest is None or latest.get("game") == session.last_game:
            return None
        return latest
    raise Unresolved("outcome of a lost spin could not be read back")


async def play_round(session: MaxwinSession, mode: str, migrate: Any,
                     args: argparse.Namespace, state: RunState, log: Any) -> dict[str, Any]:
    """Play one round of `mode`; a lost answer is read back, never replayed."""
    body = spin_body(session, Decimal(args.stake), mode)
    while True:
        try:
            answer = await send(session, "spin", body, args, state, log)
        except Transport as error:
            if state.stop:
                raise
            await migrate(f"{type(error).__name__}: {error}")
            entry = await resolve_lost(session, migrate, args, state, log)
            if entry is None:
                continue  # never reached the game: play it now
            game, user = entry.get("game"), entry.get("user") or {}
            round_id, recovered = None, True
        else:
            result = answer["result"]
            game, user = result.get("game"), result.get("user") or {}
            round_id, recovered = (result.get("transactions") or {}).get("roundId"), False
        if not isinstance(game, dict) or not isinstance(game.get("nsp"), dict):
            raise SessionLost("round answer has no game")
        if mode == "bonus" and not game["nsp"].get("freeSpins"):
            raise SessionLost("bought round has no free spins")
        session.last_game = game
        balance = ((user.get("balance") or {}).get("cash") or {}).get("atEnd")
        if balance is not None:
            session.balance = Decimal(str(balance))
        return {"game": game, "roundId": round_id, "recovered": recovered,
                "balanceAtEnd": balance}


def round_record(label: str, session: MaxwinSession, mode: str, stake: str,
                 entry: dict[str, Any]) -> dict[str, Any]:
    """One JSONL line per round; the player token is never written."""
    return {"schemaVersion": 3, "timestamp": iso_now(), "type": "round_response",
            "platform": "maxwin", "sessionId": label, "mode": mode,
            "egress": session.lane.label,
            "response": {"gameId": session.target["gameId"], "stake": stake, **entry}}


async def run_worker(worker: int, browsers: BrowserPool, lanes: LanePool,
                     args: argparse.Namespace, state: ModeState, reporter: Reporter,
                     path: Path, target: dict[str, Any]) -> dict[str, Any]:
    label = path.stem
    stats: dict[str, Any] = {"worker": worker, "ok": 0, "base": 0, "bonus": 0,
                             "naturalBonuses": 0, "sessions": 0, "sessionFailures": 0,
                             "throttled": 0, "abandoned": 0, "recovered": 0,
                             "migrations": 0, "outOfFunds": 0}
    stake = Decimal(args.stake)
    buy_cost = stake * Decimal(str(target.get("buyPrice") or 100))

    def log(message: str) -> None:
        print(f"[worker-{worker:02d}] {message}", flush=True)

    await state.nap(worker * args.startup_stagger)
    failures = 0
    with path.open("a", encoding="utf-8") as stream:

        def write(record: dict[str, Any]) -> None:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()

        while not state.stop:
            lane = await lanes.lease(state)
            if lane is None:
                break
            session: MaxwinSession | None = None

            async def migrate(reason: str) -> None:
                """Move the demo player to a fresh page on another lane.

                MaxWin keeps a player alive across idle time and IP changes, so
                there is no deadline; lanes are tried until one loads.
                """
                assert session is not None
                old = session.lane
                old.failed(args.proxy_max_failures)
                write({"schemaVersion": 3, "timestamp": iso_now(), "type": "migration",
                       "sessionId": label, "egress": old.label,
                       "error": {"message": reason[:200]}})
                log(f"{old.label}: {reason[:120]}; moving session")
                await session.close()
                lanes.release(old)
                while not state.stop:
                    candidate = await lanes.lease(state)
                    if candidate is None:
                        break
                    try:
                        await candidate.acquire(state)
                        context, page, _ = await open_page(browsers, candidate, args, state,
                                                           allow=API_MARKERS)
                    except Exception as error:  # noqa: BLE001 - try the next lane
                        if not isinstance(error, Throttled):
                            candidate.failed(args.proxy_max_failures)
                        lanes.release(candidate)
                        continue
                    session.lane, session.context, session.page = candidate, context, page
                    session.migrations += 1
                    stats["migrations"] += 1
                    return
                old.in_use += 1  # balanced by the release in finally
                session.lane = old
                raise SessionLost("stopped while moving the session")

            try:
                session = await open_session(browsers, lane, args, state, target)
                stats["sessions"] += 1
                while True:
                    mode = state.claim()
                    if mode is None:
                        break
                    cost = buy_cost if mode == "bonus" else stake
                    if session.balance < cost:
                        state.release(mode)
                        stats["outOfFunds"] += 1
                        break  # new demo player with a full balance
                    try:
                        entry = await play_round(session, mode, migrate, args, state, log)
                    except BaseException:
                        state.release(mode)
                        raise
                    write(round_record(label, session, mode, args.stake, entry))
                    failures = 0
                    session.lane.succeeded()
                    stats["ok"] += 1
                    stats[mode] += 1
                    stats["recovered"] += entry["recovered"]
                    if mode == "base" and entry["game"]["nsp"].get("freeSpins"):
                        stats["naturalBonuses"] += 1
                    reporter.tick(state.complete(mode))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - any failure opens a new player
                if state.stop:
                    break
                reason = f"{type(error).__name__}: {error}".splitlines()[0][:200]
                active = session.lane if session is not None else lane
                throttled = isinstance(error, Throttled)
                if isinstance(error, Unresolved):
                    state.abandoned += 1
                    stats["abandoned"] += 1
                if throttled:
                    stats["throttled"] += 1
                elif isinstance(error, OutOfFunds):
                    stats["outOfFunds"] += 1
                else:
                    failures += 1
                    state.session_failures += 1
                    stats["sessionFailures"] += 1
                    if isinstance(error, Transport):
                        active.failed(args.proxy_max_failures)
                write({"schemaVersion": 3, "timestamp": iso_now(),
                       "type": "throttled" if throttled else "session_error",
                       "sessionId": label, "egress": active.label,
                       "abandoned": isinstance(error, Unresolved),
                       "error": {"message": reason, "consecutive": failures}})
                if lanes.direct and not throttled:
                    delay = backoff_delay(failures - 1, args.backoff, args.max_backoff)
                    log(f"{reason}; new session in {delay:.0f}s")
                    await state.nap(delay)
                else:
                    log(f"{active.label}: {reason}")
                    await state.nap(1)
            finally:
                if session is not None:
                    await session.close()
                    lanes.release(session.lane)
                else:
                    lanes.release(lane)

    stats["file"] = str(path)
    return stats


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect MaxWin (Dopamine) demo rounds through browser workers.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Public game URL")
    parser.add_argument("--rounds", type=int, default=10000,
                        help="Base-game rounds (natural bonuses included)")
    parser.add_argument("--bonus-rounds", type=int, default=0,
                        help="Extra rounds with the bonus bought (featureBuy)")
    parser.add_argument("--stake", default="0.1",
                        help="Stake per round; the smallest keeps the demo balance longest")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--rate", type=float, default=2.0,
                        help="Backend requests per second per egress IP")
    parser.add_argument("--session-cost", type=float, default=2.0,
                        help="Request budget charged for opening one session")
    parser.add_argument("--proxies",
                        help="Comma-separated proxy sources: built-in "
                             f"{', '.join(PROXY_SOURCES)} or 'all', a file or a URL")
    parser.add_argument("--workers-per-proxy", type=int, default=1)
    parser.add_argument("--proxy-check-concurrency", type=int, default=50)
    parser.add_argument("--proxy-check-timeout", type=float, default=15.0)
    parser.add_argument("--proxy-refresh", type=float, default=600.0)
    parser.add_argument("--proxy-max-failures", type=int, default=2)
    parser.add_argument("--proxy-retries", type=int, default=1,
                        help="Retries of a non-spin call on the same proxy")
    parser.add_argument("--resolve-attempts", type=int, default=6,
                        help="Lanes tried to read back the outcome of a lost spin")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--page-timeout", type=float, default=45.0)
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--backoff", type=float, default=2.0)
    parser.add_argument("--max-backoff", type=float, default=300.0)
    parser.add_argument("--startup-stagger", type=float, default=1.0)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--out-root", type=Path,
                        default=Path(__file__).resolve().parent.parent / "output" / "browser-runs",
                        help="Defaults to output/browser-runs in the project root")
    parser.add_argument("--run-name")
    parser.add_argument("--raw-only", action="store_true",
                        help="Skip building the Stake and Artube artifacts")
    args = parser.parse_args(argv)
    # Shared code (proxy checker) loads this page to test a proxy.
    args.label = parse_target(args.url)["gameId"]
    return args


async def collect(args: argparse.Namespace, run_dir: Path, run_id: str,
                  target: dict[str, Any]) -> tuple[ModeState, list[dict[str, Any]], float,
                                                   dict[str, Any]]:
    from playwright.async_api import async_playwright

    state = ModeState({"base": args.rounds, "bonus": args.bonus_rounds})

    def on_signal(*_: Any) -> None:
        if not state.stop:
            print("cancellation requested; finishing current requests.", flush=True)
            state.stop = True

    signal.signal(signal.SIGINT, on_signal)
    started = time.monotonic()
    async with async_playwright() as playwright:
        browsers = BrowserPool(playwright, args.headed)
        lanes = LanePool(playwright, args)
        reporter = Reporter(state, lanes, args.progress_every)
        checker = asyncio.create_task(lanes.run_checker(state))
        try:
            results = await asyncio.gather(*(
                run_worker(worker, browsers, lanes, args, state, reporter,
                           run_dir / f"{args.label}-worker-{worker:02d}-{run_id}.jsonl",
                           target)
                for worker in range(1, args.workers + 1)))
        finally:
            state.stop = True
            checker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await checker
            await browsers.close()
    return state, list(results), time.monotonic() - started, lanes.summary()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.workers < 1 or args.rate <= 0 or args.rounds < 0 or args.bonus_rounds < 0:
        raise SystemExit("--workers and --rate must be positive, round counts non-negative.")
    if args.rounds + args.bonus_rounds < 1:
        raise SystemExit("Nothing to collect: set --rounds and/or --bonus-rounds.")
    if Decimal(args.stake) <= 0:
        raise SystemExit("--stake must be positive.")
    if not args.raw_only:
        try:
            import zstandard  # noqa: F401
        except ImportError as error:
            raise SystemExit("Artifact export needs zstandard: "
                             "python -m pip install -r tools/requirements-artifacts.txt") from error

    target: dict[str, Any] = parse_target(args.url)
    run_id = uuid.uuid4().hex[:12]
    run_name = args.run_name or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{run_id}"
    run_dir = args.out_root / run_name
    if run_dir.exists() and any(run_dir.glob("*-worker-*.jsonl")):
        raise SystemExit(f"{run_dir} already holds a collected run; choose another --run-name.")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run={run_name} game={target['gameId']} vertical={target['vertical']} "
          f"workers={args.workers} rounds={args.rounds + args.bonus_rounds} "
          f"(base {args.rounds}, bought bonus {args.bonus_rounds}) stake={args.stake} "
          f"rate={args.rate}/s per IP proxies={args.proxies or 'none'} out={run_dir}",
          flush=True)

    started_utc = iso_now()
    state, results, elapsed, egress = asyncio.run(collect(args, run_dir, run_id, target))
    totals = {key: sum(item.get(key, 0) for item in results)
              for key in ("naturalBonuses", "recovered", "migrations", "outOfFunds")}
    manifest = {
        "run": run_name, "runId": run_id, "collector": "collect_maxwin",
        "platform": "maxwin", "startedUtc": started_utc, "url": args.url,
        "gameId": target["gameId"], "vertical": target["vertical"],
        "mathName": target.get("mathName"), "feature": target.get("feature"),
        "buyPrice": target.get("buyPrice"), "stake": args.stake,
        "workers": args.workers,
        "roundsRequested": {"base": args.rounds, "bonus": args.bonus_rounds},
        "roundsCollected": state.done,
        "roundsByMode": state.done_by,
        "roundsAbandoned": state.abandoned,
        "sessionFailures": state.session_failures,
        "throttled": state.throttled,
        **totals,
        "rate": args.rate, "egress": egress,
        "elapsedSeconds": round(elapsed, 3),
        "roundsPerSecond": round(state.done / elapsed, 3) if elapsed else None,
        "workerStats": sorted(results, key=lambda item: item["worker"]),
    }
    (run_dir / "run.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(f"collected={state.done} {state.done_by} natural_bonuses={totals['naturalBonuses']} "
          f"recovered={totals['recovered']} abandoned={state.abandoned} "
          f"session_failures={state.session_failures} throttled={state.throttled}")
    print(f"elapsed={elapsed:.1f}s run_dir={run_dir}")
    if state.done <= 0:
        return 1
    if not args.raw_only:
        try:
            from build_maxwin_artifacts import convert

            report = convert(run_dir)
            print("artifacts=" + json.dumps(report, ensure_ascii=False))
        except Exception as error:  # noqa: BLE001
            print(f"artifact export failed: {type(error).__name__}: {error}", file=sys.stderr)
            print("Raw collection is preserved; rerun build_maxwin_artifacts.py.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
