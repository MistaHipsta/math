"""Browser-backed Moon Sisters round collector.

The only input is the public game URL. Every worker is autonomous: it opens a
Chromium context, loads the game page (HTML only, no game client), reads the
demo token and backend endpoint the page embeds, runs `login` -> `start` and
then plays rounds with `fetch` from that page. Requests carry the browser's
own TLS fingerprint, cookies and the game origin.

The backend sits behind a Cloudflare rate limit counted per egress IP
(`error code: 1015`, HTTP 429 with a Retry-After of tens of minutes). Every
egress IP is a lane with its own request budget (`--rate`) and its own 429
cooldown. Without `--proxies` there is one direct lane shared by all workers.
With `--proxies` a background checker screens a proxy list and every worker
leases a working proxy per session, so throughput grows with the number of
working proxies instead of hitting one IP harder.

Rounds are never cut short. If a request fails on the wire (dead proxy,
timeout, 429, crashed page) the worker moves the SAME game session to another
lane and resends the SAME request: the backend deduplicates by `request_id`
and returns the original answer, and a session can be continued from any IP.
So a spin whose answer was lost, or a bonus halfway through its respins, is
finished elsewhere and recorded with its full step history.

The backend closes a session after about a minute without requests and then
answers GAME_REOPENED; a new login gets a new demo player, so an unfinished
bonus cannot be recovered after that. Moving the session is therefore a race:
several lanes are tried in parallel, proven ones first, and the whole move
must fit in `--resume-window` seconds after the last answer. A round that
still dies is written only as `session_error` with its steps in
`abandonedSteps`, never as `round_response`, so it cannot reach the
artifacts.

TLS is verified end to end, so a proxy cannot read or alter game traffic.

Output layout matches `collect_moon_http.py`, so `build_moon_artifacts.py`,
`convert_moon_raw_to_round_results.py` and `analyze_moon_jsonl.py` read it
unchanged. Each round additionally keeps every request and full response in
`response.steps`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import random
import re
import signal
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_URL = "https://3oaks.com/api/v1/games/moon_sisters/play?lang=en"
PROXIFLY_URL = (
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.json"
)
PROXY_SCHEMES = {"http", "https", "socks4", "socks5"}
SESSION_SEGMENT = re.compile(r"/desktop/([0-9A-Za-z]{8,})/demo/")
OK_CODES = {"OK", "SUCCESS", ""}
# Refusals tied to the egress IP, not to the game session.
LANE_BANNED_CODES = {"PLAYER_LOCKOUT"}
TRANSIENT_STATUS = {0, 408, 425, 500, 502, 503, 504}

FETCH_JS = """
async ([url, body, timeoutMs]) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {'content-type': 'text/plain'},
      body,
      signal: controller.signal,
    });
    return {
      status: response.status,
      text: await response.text(),
      retryAfter: response.headers.get('retry-after'),
    };
  } catch (error) {
    return {status: 0, text: String(error), retryAfter: null};
  } finally {
    clearTimeout(timer);
  }
}
"""


class SessionLost(Exception):
    """The current game session cannot continue; open a new one."""


class Transport(SessionLost):
    """The request may not have reached the game; resend it from another lane."""


class LaneBanned(Transport):
    """The backend refuses this egress IP; drop the lane for the rest of the run."""


class Throttled(Transport):
    """HTTP 429: this lane is cooling down for longer than a round should wait."""

    def __init__(self, retry_after: str | None, delay: float) -> None:
        super().__init__(f"HTTP 429, Retry-After {retry_after}, lane paused {delay:.0f}s")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_url(url: str) -> str:
    return SESSION_SEGMENT.sub("/desktop/{session}/demo/", url)


def redact_request(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: "[redacted]" if key in {"session_id", "token"} else value
        for key, value in payload.items()
    }


def backoff_delay(
    attempt: int,
    base: float,
    cap: float,
    retry_after: str | None = None,
) -> float:
    """Exponential backoff with jitter, capped.

    A numeric Retry-After is honoured in full, above the cap: retrying a
    Cloudflare ban early only extends it.
    """
    delay = min(cap, base * (2 ** max(0, attempt))) * random.uniform(0.75, 1.25)
    if retry_after:
        try:
            delay = max(delay, float(retry_after) + random.uniform(1, 10))
        except ValueError:
            pass
    return delay


def response_code(body: dict[str, Any]) -> str:
    status = body.get("status")
    if isinstance(status, dict):
        return str(status.get("code", "")).upper()
    return ""


def parse_launch(html: str) -> tuple[str, str]:
    """Return the desktop backend endpoint and demo token the game page embeds."""
    token = re.search(r'"token":\s*"([^"]+)"', html)
    queue = re.search(r'"queue":\s*"([^"]+)"', html)
    server = re.search(r'"server_url":\s*"([^"]*/desktop/[^"]*)"', html)
    if not (token and queue and server):
        raise SessionLost("game page has no launch config")
    endpoint = server.group(1).replace("{QUEUE}", queue.group(1))
    if endpoint.startswith("//"):
        endpoint = "https:" + endpoint
    return endpoint, token.group(1)


def load_proxy_list(source: str) -> list[str]:
    """Read proxies from a file or URL: proxifly JSON, JSON strings or text lines."""
    if source == "proxifly":
        source = PROXIFLY_URL
    if re.match(r"https?://", source) and not Path(source).exists():
        with urllib.request.urlopen(source, timeout=60) as response:
            text = response.read().decode("utf-8", errors="replace")
    else:
        text = Path(source).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = text.splitlines()

    servers = []
    for item in data if isinstance(data, list) else []:
        value = item.get("proxy") if isinstance(item, dict) else item
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if "://" not in value:
            value = "http://" + value
        try:
            parts = urlsplit(value)
            if parts.scheme.lower() in PROXY_SCHEMES and parts.hostname and parts.port:
                servers.append(value)
        except ValueError:
            continue
    return list(dict.fromkeys(servers))


def merge_round(steps: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Return converter-compatible RawJson and playHistory for one round.

    For a bonus round the closing response carries the final win, but the
    board that triggered the bonus lives in the first response, so it is
    restored, exactly as `collect_moon_http.py` stored it.
    """
    history = [copy.deepcopy(step["response"].get("context") or {}) for step in steps]
    final = copy.deepcopy(steps[-1]["response"])
    if len(steps) > 1:
        spins = final.setdefault("context", {}).setdefault("spins", {})
        board = (history[0].get("spins") or {}).get("board")
        if board is not None:
            spins["board"] = board
        spins["round_win"] = spins.get("total_win", spins.get("round_win"))
        spins["bonus_steps"] = len(steps) - 1
    return json.dumps(final, separators=(",", ":"), ensure_ascii=False), history


class RunState:
    """Round budget, stop flag and run-wide counters."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.claimed = 0
        self.done = 0
        self.abandoned = 0
        self.session_failures = 0
        self.throttled = 0
        self.stop = False

    def claim(self) -> bool:
        if self.stop or self.claimed >= self.total:
            return False
        self.claimed += 1
        return True

    def release(self) -> None:
        self.claimed -= 1

    def complete(self) -> int:
        self.done += 1
        if self.done >= self.total:
            self.stop = True
        return self.done

    async def nap(self, seconds: float) -> None:
        """Sleep in short slices so Ctrl+C stops workers promptly."""
        deadline = time.monotonic() + seconds
        while not self.stop and (left := deadline - time.monotonic()) > 0:
            await asyncio.sleep(min(left, 1.0))


class Lane:
    """One egress IP: its own request budget and 429 cooldown."""

    def __init__(self, server: str | None, rate: float, capacity: int) -> None:
        self.server = server
        self.label = urlsplit(server).netloc if server else "direct"
        self.interval = 1.0 / rate
        self.capacity = capacity
        self.in_use = 0
        self.next_slot = 0.0
        self.cooldown_until = 0.0
        self.throttle_streak = 0
        self.failures = 0
        self.dead = False
        self.rounds = 0

    def available(self, now: float) -> bool:
        return not self.dead and self.in_use < self.capacity and self.cooldown_until <= now

    def throttle(self, base: float, cap: float, retry_after: str | None) -> float:
        delay = backoff_delay(self.throttle_streak, base, cap, retry_after)
        self.throttle_streak += 1
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
        return delay

    async def acquire(self, state: RunState, cost: float = 1.0) -> None:
        """Wait out the cooldown, then take the next slot of this IP's budget."""
        while not state.stop and (left := self.cooldown_until - time.monotonic()) > 0:
            await asyncio.sleep(min(left, 1.0))
        now = time.monotonic()
        start = max(now, self.next_slot)
        self.next_slot = start + self.interval * cost * random.uniform(0.8, 1.2)
        await state.nap(start - now)

    def succeeded(self) -> None:
        self.failures = 0
        self.throttle_streak = 0
        self.rounds += 1

    def failed(self, limit: int) -> None:
        self.failures += 1
        if self.server and self.failures >= limit:
            self.dead = True


class LanePool:
    """Direct lane, or proxies screened in the background and leased per session."""

    def __init__(self, playwright: Any, args: argparse.Namespace) -> None:
        self.playwright = playwright
        self.args = args
        self.direct = not args.proxies
        self.lanes: list[Lane] = []
        self.candidates: list[str] = []
        self.seen: set[str] = set()
        self.checked = 0
        self.loaded_at = float("-inf")
        if self.direct:
            self.lanes.append(Lane(None, args.rate, capacity=args.workers))

    def usable(self) -> int:
        return sum(not lane.dead for lane in self.lanes)

    async def lease(self, state: RunState) -> Lane | None:
        while not state.stop:
            leased = self.lease_now(1)
            if leased:
                return leased[0]
            await asyncio.sleep(1.0)
        return None

    def lease_now(self, count: int, rescue: bool = False,
                  exclude: Any = ()) -> list[Lane]:
        """Lease up to `count` free lanes at once, proven ones first.

        With `rescue` (moving a live session) proven lanes that are already
        busy may be shared too: the per-IP request budget still applies, and
        a session that waits for a free lane dies on the backend.
        """
        now = time.monotonic()
        free = [lane for lane in self.lanes
                if lane.available(now) and lane not in exclude]
        free.sort(key=lambda item: (item.rounds == 0, item.in_use, random.random()))
        chosen = free[:count]
        if rescue and len(chosen) < count:
            busy = [lane for lane in self.lanes
                    if lane not in chosen and lane not in exclude
                    and lane.rounds > 0 and not lane.dead
                    and lane.cooldown_until <= now]
            busy.sort(key=lambda item: (item.in_use, random.random()))
            chosen += busy[:count - len(chosen)]
        for lane in chosen:
            lane.in_use += 1
        return chosen

    @staticmethod
    def release(lane: Lane) -> None:
        lane.in_use -= 1

    async def check(self, server: str) -> bool:
        try:
            context = await self.playwright.request.new_context(
                proxy={"server": server},
                timeout=self.args.proxy_check_timeout * 1000,
            )
        except Exception:  # noqa: BLE001 - unusable proxy definition
            return False
        try:
            response = await context.get(self.args.url, max_redirects=3)
            # Cloudflare answers 403 to non-browser TLS, which still proves the
            # proxy reaches the site; 429 means this IP is already limited.
            return response.status in (200, 403)
        except Exception:  # noqa: BLE001 - dead or slow proxy
            return False
        finally:
            with contextlib.suppress(Exception):
                await context.dispose()

    async def run_checker(self, state: RunState) -> None:
        if self.direct:
            return

        async def screen(server: str) -> None:
            ok = await self.check(server)
            self.checked += 1
            if ok and not state.stop:
                self.lanes.append(Lane(server, self.args.rate, self.args.workers_per_proxy))

        while not state.stop:
            if time.monotonic() - self.loaded_at >= self.args.proxy_refresh:
                try:
                    servers = await asyncio.to_thread(load_proxy_list, self.args.proxies)
                    fresh = [server for server in servers if server not in self.seen]
                    self.seen.update(fresh)
                    random.shuffle(fresh)
                    self.candidates.extend(fresh)
                    self.loaded_at = time.monotonic()
                    print(f"proxies: +{len(fresh)} new, {len(self.candidates)} to check, "
                          f"{self.usable()} usable", flush=True)
                except Exception as error:  # noqa: BLE001 - retry the list soon
                    print(f"proxy list load failed: {error}", flush=True)
                    self.loaded_at = time.monotonic() - self.args.proxy_refresh + 30

            now = time.monotonic()
            spare = sum(lane.capacity - lane.in_use for lane in self.lanes
                        if lane.available(now))
            if not self.candidates or spare >= self.args.workers:
                await state.nap(5)
                continue
            batch = [self.candidates.pop()
                     for _ in range(min(len(self.candidates),
                                        self.args.proxy_check_concurrency))]
            await asyncio.gather(*(screen(server) for server in batch))

    def summary(self) -> dict[str, Any]:
        if self.direct:
            return {"mode": "direct"}
        busiest = sorted(self.lanes, key=lambda lane: lane.rounds, reverse=True)[:20]
        return {
            "mode": "proxies",
            "source": self.args.proxies,
            "listed": len(self.seen),
            "checked": self.checked,
            "passedCheck": len(self.lanes),
            "dead": sum(lane.dead for lane in self.lanes),
            "top": [{"proxy": lane.label, "rounds": lane.rounds} for lane in busiest],
        }


class GameSession:
    def __init__(self, lane: Lane, context: Any, page: Any, endpoint: str,
                 session_id: str, balance: int | None) -> None:
        self.lane = lane
        self.context = context
        self.page = page
        self.endpoint = endpoint
        self.session_id = session_id
        self.balance = balance
        self.last_ok = time.monotonic()
        self.migrations = 0

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.context.close()


class BrowserPool:
    """One shared Chromium; relaunched if it crashes."""

    def __init__(self, playwright: Any, headed: bool) -> None:
        self.playwright = playwright
        self.headed = headed
        self.browser: Any = None
        self.lock = asyncio.Lock()

    async def get(self) -> Any:
        async with self.lock:
            if self.browser is None or not self.browser.is_connected():
                self.browser = await self.playwright.chromium.launch(
                    headless=not self.headed
                )
            return self.browser

    async def close(self) -> None:
        if self.browser is not None:
            with contextlib.suppress(Exception):
                await self.browser.close()


async def post(page: Any, endpoint: str, payload: dict[str, Any],
               timeout: float) -> tuple[int, dict[str, Any] | None, str | None]:
    """POST one game command from the page; returns status, JSON body, Retry-After."""
    result = await page.evaluate(FETCH_JS, [
        f"{endpoint}?gsc={payload['command']}",
        json.dumps(payload, separators=(",", ":")),
        int(timeout * 1000),
    ])
    try:
        body = json.loads(result["text"]) if result["status"] == 200 else None
    except json.JSONDecodeError:
        body = None
    return result["status"], body if isinstance(body, dict) else None, result["retryAfter"]


def throttle(lane: Lane, state: RunState, args: argparse.Namespace,
             retry_after: str | None) -> float:
    state.throttled += 1
    return lane.throttle(args.backoff, args.max_backoff, retry_after)


async def open_page(browsers: BrowserPool, lane: Lane, args: argparse.Namespace,
                    state: RunState, timeout: float | None = None) -> tuple[Any, Any, str]:
    """Open a context on the lane and load the game page HTML (no client)."""
    browser = await browsers.get()
    options: dict[str, Any] = {"viewport": {"width": 1280, "height": 720},
                               "locale": "en-US"}
    if lane.server:
        options["proxy"] = {"server": lane.server}
    context = await browser.new_context(**options)
    try:
        # Only the page itself and our own game commands go out: the game
        # client, assets and telemetry would cost proxy bandwidth and budget.
        async def gate(route: Any) -> None:
            request = route.request
            if request.resource_type == "document" or "gsc=" in request.url:
                await route.continue_()
            else:
                await route.abort()

        await context.route("**/*", gate)
        page = await context.new_page()
        try:
            response = await page.goto(args.url, wait_until="domcontentloaded",
                                       timeout=(timeout or args.page_timeout) * 1000)
        except Exception as error:
            raise Transport(f"game page: {error}".splitlines()[0]) from error
        if response is None:
            raise Transport("game page did not load")
        if response.status == 429:
            retry_after = await response.header_value("retry-after")
            raise Throttled(retry_after, throttle(lane, state, args, retry_after))
        if response.status != 200:
            raise Transport(f"game page HTTP {response.status}")
        return context, page, await response.text()
    except BaseException:
        with contextlib.suppress(Exception):
            await context.close()
        raise


async def open_session(browsers: BrowserPool, lane: Lane, args: argparse.Namespace,
                       state: RunState) -> GameSession:
    """Load the game HTML through the lane, then run login and start."""
    await lane.acquire(state, args.session_cost)
    context, page, html = await open_page(browsers, lane, args, state)
    try:
        endpoint, token = parse_launch(html)

        async def command(payload: dict[str, Any]) -> dict[str, Any]:
            status, body, retry_after = await post(page, endpoint, payload, args.timeout)
            if status == 429:
                raise Throttled(retry_after, throttle(lane, state, args, retry_after))
            if body is None:
                raise Transport(f"{payload['command']} HTTP {status}")
            code = response_code(body)
            if code in LANE_BANNED_CODES:
                lane.dead = True
                raise LaneBanned(f"{payload['command']} rejected {code}")
            if code not in OK_CODES:
                raise SessionLost(f"{payload['command']} rejected {code}")
            return body

        login = await command({
            "command": "login",
            "request_id": uuid.uuid4().hex,
            "token": token,
            "language": "en",
            "client_command_timestamp": int(time.time() * 1000),
        })
        session_id = login.get("session_id")
        if not session_id:
            raise SessionLost("login returned no session_id")
        user = login.get("user") or {}
        modes = login.get("modes") or ["auto"]
        start = await command({
            "command": "start",
            "request_id": uuid.uuid4().hex,
            "session_id": session_id,
            "mode": "auto" if "auto" in modes else str(modes[0]),
            "huid": user.get("huid"),
            "client_command_timestamp": int(time.time() * 1000),
        })
        return GameSession(lane, context, page, endpoint, str(session_id),
                           (start.get("user") or user).get("balance"))
    except BaseException:
        with contextlib.suppress(Exception):
            await context.close()
        raise


async def send_play(
    session: GameSession,
    payload: dict[str, Any],
    args: argparse.Namespace,
    state: RunState,
    log: Any,
) -> dict[str, Any]:
    """POST one play command, riding out short rate limits and transient errors.

    The same payload (same request_id) is resent on every retry, which the
    backend answers idempotently. Raises Transport when the lane should be
    abandoned; a proxy lane gets one quick retry, the direct lane several.
    """
    lane = session.lane
    retries = args.max_retries if lane.server is None else args.proxy_retries
    attempt = 0
    while True:
        await lane.acquire(state)
        if state.stop:
            raise SessionLost("stopped")
        try:
            status, body, retry_after = await post(session.page, session.endpoint,
                                                   payload, args.timeout)
        except Exception as error:  # noqa: BLE001 - page or browser died
            raise Transport(f"page: {error}".splitlines()[0]) from error
        if body is not None:
            code = response_code(body)
            if code in OK_CODES:
                session.last_ok = time.monotonic()
                return body
            if code in LANE_BANNED_CODES:
                lane.dead = True
                raise LaneBanned(f"rejected {code}")
            raise SessionLost(f"rejected {code}")
        if status == 429:
            # Waiting out a ban would let the session expire: move it instead.
            raise Throttled(retry_after, throttle(lane, state, args, retry_after))

        reason = "bad_json" if status == 200 else f"http_{status}"
        if status != 200 and status not in TRANSIENT_STATUS:
            raise SessionLost(reason)
        if attempt >= retries:
            raise Transport(f"retries exhausted ({reason})")
        delay = backoff_delay(attempt, args.backoff, args.max_backoff)
        attempt += 1
        log(f"{lane.label}: {reason}, retry {attempt}/{retries} in {delay:.1f}s")
        await state.nap(delay)


class HedgePool:
    """Warm standby pages on other lanes, opened when a bonus starts.

    If the session's lane fails mid-bonus, a standby page takes over at once
    and the unanswered step is resent from it, instead of racing to load a
    new page before the backend drops the silent session. Steps are never
    sent in parallel: a slow copy of step N reaching the backend after step
    N+1 makes it close the session with GAME_REOPENED.
    """

    def __init__(self, browsers: BrowserPool, lanes: LanePool,
                 args: argparse.Namespace, state: RunState, log: Any) -> None:
        self.browsers = browsers
        self.lanes = lanes
        self.args = args
        self.state = state
        self.log = log
        self.ready: list[tuple[Lane, Any, Any]] = []
        self.pending: asyncio.Task | None = None
        self.closed = False
        self.promotions = 0

    def top_up(self, primary: Lane) -> None:
        """Start opening standby pages in the background, if any are missing."""
        if self.closed or (self.pending is not None and not self.pending.done()):
            return
        missing = self.args.bonus_hedges - len(self.ready)
        if missing <= 0:
            return
        exclude = {primary} | {lane for lane, _, _ in self.ready}
        leased = self.lanes.lease_now(missing, rescue=True, exclude=exclude)
        if leased:
            self.pending = asyncio.create_task(self._open(leased))

    async def _open(self, leased: list[Lane]) -> None:
        async def one(lane: Lane) -> None:
            try:
                await lane.acquire(self.state)
                context, page, _ = await open_page(
                    self.browsers, lane, self.args, self.state,
                    timeout=self.args.hedge_page_timeout)
            except BaseException as error:
                if isinstance(error, Exception) and not isinstance(error, Throttled):
                    lane.failed(self.args.proxy_max_failures)
                self.lanes.release(lane)
                if not isinstance(error, Exception):
                    raise
                return
            if self.closed:
                with contextlib.suppress(Exception):
                    await context.close()
                self.lanes.release(lane)
                return
            self.ready.append((lane, context, page))

        await asyncio.gather(*(one(lane) for lane in leased))

    async def wait_ready(self, timeout: float) -> None:
        if self.pending is not None and not self.pending.done():
            await asyncio.wait({self.pending}, timeout=timeout)

    def _drop_later(self, lane: Lane, context: Any) -> None:
        self.lanes.release(lane)
        if context is not None:
            asyncio.ensure_future(self._close_quietly(context))

    @staticmethod
    async def _close_quietly(context: Any) -> None:
        with contextlib.suppress(Exception):
            await context.close()

    def promote(self, session: GameSession) -> bool:
        """Make a standby page the session's page; False if none is ready."""
        if not self.ready:
            return False
        lane, context, page = self.ready.pop(0)
        old_lane, old_context = session.lane, session.context
        session.lane, session.context, session.page = lane, context, page
        session.migrations += 1
        self.promotions += 1
        old_lane.failed(self.args.proxy_max_failures)
        self._drop_later(old_lane, old_context)
        self.log(f"{old_lane.label} failed; standby {lane.label} took over")
        return True

    async def close(self) -> None:
        self.closed = True
        if self.pending is not None and not self.pending.done():
            self.pending.cancel()
            with contextlib.suppress(BaseException):
                await self.pending
        for lane, context, _ in self.ready:
            self.lanes.release(lane)
            await self._close_quietly(context)
        self.ready.clear()


def play_payload(session_id: str, action: str, params: dict[str, Any],
                 args: argparse.Namespace) -> dict[str, Any]:
    return {
        "command": "play",
        "request_id": uuid.uuid4().hex,
        "session_id": session_id,
        "action": {"name": action, "params": params},
        "set_denominator": args.set_denominator,
        "quick_spin": 1,
        "sound": False,
        "autogame": False,
        "mobile": "0",
        "portrait": False,
        "fullscreen": True,
        "viewportSize": "1280x720",
        "client_command_timestamp": int(time.time() * 1000),
    }


async def play_round(
    session: GameSession,
    migrate: Any,
    args: argparse.Namespace,
    state: RunState,
    log: Any,
    hedges: HedgePool | None = None,
) -> list[dict[str, Any]]:
    """Spin and follow bonus actions until the backend finishes the round.

    If the session's lane is lost, a warm standby page (`hedges`) takes over,
    or else `migrate(reason, mid_bonus)` moves the session to another lane;
    the unanswered request is then resent unchanged, so no step is skipped
    or repeated.
    """
    steps: list[dict[str, Any]] = []
    action = "spin"
    params: dict[str, Any] = {"bet_per_line": args.bet_per_line, "lines": args.lines}
    try:
        while True:
            payload = play_payload(session.session_id, action, params, args)
            while True:
                try:
                    response = await send_play(session, payload, args, state, log)
                    egress = session.lane.label
                    break
                except Transport as error:
                    if state.stop:
                        raise
                    if hedges is not None and hedges.promote(session):
                        hedges.top_up(session.lane)
                        continue
                    await migrate(f"{type(error).__name__}: {error}", bool(steps))
            context = response.get("context") or {}
            if action == "spin" and not isinstance(
                    (context.get("spins") or {}).get("board"), list):
                raise SessionLost("spin response has no board")
            if steps and context.get("last_action") not in (None, action):
                raise SessionLost(
                    f"step mismatch: sent {action}, got {context.get('last_action')}")
            steps.append({"action": action, "request": redact_request(payload),
                          "egress": egress, "response": response})

            if context.get("round_finished") is not False:
                session.balance = (response.get("user") or {}).get("balance")
                return steps
            actions = context.get("actions") or []
            if not actions or len(steps) > args.max_bonus_steps:
                raise SessionLost("round cannot be finished")
            action, params = str(actions[0]), {}
            if hedges is not None and len(steps) == 1:
                # A bonus just started: get standby pages up before its first step.
                hedges.top_up(session.lane)
                await hedges.wait_ready(args.hedge_wait)
    except Exception as error:
        # Steps of a round that died mid-bonus are kept for diagnostics.
        error.partial_steps = steps  # type: ignore[attr-defined]
        raise
    finally:
        if hedges is not None:
            await hedges.close()


def round_record(label: str, endpoint: str, steps: list[dict[str, Any]],
                 egress: str | None = None) -> dict[str, Any]:
    raw, history = merge_round(steps)
    record = {
        "schemaVersion": 2,
        "timestamp": iso_now(),
        "type": "round_response",
        "sessionId": label,
        "response": {
            "TimestampUtc": iso_now(),
            "CorrelationId": steps[0]["request"]["request_id"],
            "Url": safe_url(endpoint),
            "Method": "POST",
            "Status": 200,
            "ContentType": "application/json",
            "BodyBytes": len(raw.encode("utf-8")),
            "RawJson": raw,
            "playHistory": history,
            "steps": steps,
            "Error": None,
        },
    }
    if egress:
        record["egress"] = egress
    return record


class Reporter:
    def __init__(self, state: RunState, lanes: LanePool, every: int) -> None:
        self.state = state
        self.lanes = lanes
        self.every = max(1, every)
        self.started = time.monotonic()

    def tick(self, done: int) -> None:
        if done % self.every and done != self.state.total:
            return
        elapsed = time.monotonic() - self.started
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (self.state.total - done) / rate if rate > 0 else float("inf")
        egress = "" if self.lanes.direct else f"proxies={self.lanes.usable()} "
        print(
            f"rounds={done}/{self.state.total} rate={rate:.2f}/s "
            f"elapsed={elapsed:.0f}s eta={eta:.0f}s {egress}"
            f"sessions_failed={self.state.session_failures} "
            f"abandoned={self.state.abandoned} throttled={self.state.throttled}",
            flush=True,
        )


async def run_worker(
    worker: int,
    browsers: BrowserPool,
    lanes: LanePool,
    args: argparse.Namespace,
    state: RunState,
    reporter: Reporter,
    path: Path,
) -> dict[str, Any]:
    label = path.stem
    stats: dict[str, Any] = {"worker": worker, "ok": 0, "sessions": 0,
                             "sessionFailures": 0, "throttled": 0, "abandoned": 0,
                             "balanceRefresh": 0, "bonusRounds": 0,
                             "migrations": 0, "bonusResumed": 0,
                             "standbyTakeovers": 0}
    stake = args.bet_per_line * args.lines * args.set_denominator

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
            session: GameSession | None = None

            async def migrate(reason: str, mid_bonus: bool) -> None:
                """Move the live game session to a fresh page on another lane.

                Several lanes race to load the page; the first one wins and
                the rest are closed. It must finish before the backend drops
                the silent session, `--resume-window` after the last answer.
                """
                nonlocal lane
                assert session is not None
                old = session.lane
                old.failed(args.proxy_max_failures)
                write({"schemaVersion": 2, "timestamp": iso_now(), "type": "migration",
                       "sessionId": label, "egress": old.label,
                       "midBonus": mid_bonus, "error": {"message": reason[:200]}})
                log(f"{old.label}: {reason[:120]}; moving session"
                    f"{' mid-bonus' if mid_bonus else ''}")
                await session.close()
                lanes.release(old)
                lane = None  # type: ignore[assignment]
                deadline = session.last_ok + args.resume_window
                started = time.monotonic()

                async def attempt(candidate: Lane, left: float) -> tuple[Lane, Any, Any]:
                    await candidate.acquire(state)
                    context, page, _ = await open_page(browsers, candidate, args, state,
                                                       timeout=left)
                    return candidate, context, page

                while not state.stop and (left := deadline - time.monotonic()) > 1:
                    candidates = lanes.lease_now(args.resume_parallel, rescue=True)
                    if not candidates:
                        await asyncio.sleep(0.5)
                        continue
                    tasks = {asyncio.create_task(attempt(c, left)): c for c in candidates}
                    winner = None
                    try:
                        for finished in asyncio.as_completed(tasks, timeout=left):
                            try:
                                winner = await finished
                                break
                            except Exception:  # noqa: BLE001 - next finisher
                                continue
                    except asyncio.TimeoutError:
                        pass
                    for task, candidate in tasks.items():
                        if winner is not None and candidate is winner[0]:
                            continue
                        if not task.done():
                            task.cancel()
                        with contextlib.suppress(BaseException):
                            result = await task
                            await result[1].close()  # loaded too late: not used
                        error = task.exception() if not task.cancelled() else None
                        if error is not None and not isinstance(error, Throttled):
                            candidate.failed(args.proxy_max_failures)
                        lanes.release(candidate)
                    if winner is not None:
                        lane, context, page = winner
                        session.lane, session.context, session.page = lane, context, page
                        session.migrations += 1
                        stats["migrations"] += 1
                        log(f"session moved to {lane.label} in "
                            f"{time.monotonic() - started:.1f}s")
                        return
                lane = old
                old.in_use += 1  # balanced by the release in finally
                raise SessionLost(
                    f"no lane within {args.resume_window:.0f}s of the last answer")

            try:
                session = await open_session(browsers, lane, args, state)
                stats["sessions"] += 1

                while state.claim():
                    migrations_before = session.migrations
                    hedges = HedgePool(browsers, lanes, args, state, log)
                    try:
                        steps = await play_round(session, migrate, args, state, log, hedges)
                    except BaseException:
                        state.release()
                        raise
                    finally:
                        stats["standbyTakeovers"] += hedges.promotions
                    write(round_record(label, session.endpoint, steps,
                                       None if lanes.direct else session.lane.label))
                    failures = 0
                    session.lane.succeeded()
                    stats["ok"] += 1
                    if len(steps) > 1:
                        stats["bonusRounds"] += 1
                        if session.migrations > migrations_before:
                            stats["bonusResumed"] += 1
                    reporter.tick(state.complete())

                    if session.balance is not None and \
                            session.balance < stake * args.min_balance_spins:
                        stats["balanceRefresh"] += 1
                        break
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - any failure reopens the session
                if state.stop:
                    break
                reason = f"{type(error).__name__}: {error}".splitlines()[0][:200]
                # A round that dies mid-bonus is lost with its demo account;
                # count it so a biased sample is visible in run.json.
                partial = getattr(error, "partial_steps", None) or []
                if partial:
                    state.abandoned += 1
                    stats["abandoned"] += 1
                active = session.lane if session is not None else lane
                throttled = isinstance(error, Throttled)
                if throttled:
                    stats["throttled"] += 1
                else:
                    failures += 1
                    state.session_failures += 1
                    stats["sessionFailures"] += 1
                    if isinstance(error, Transport):
                        active.failed(args.proxy_max_failures)
                write({"schemaVersion": 2, "timestamp": iso_now(),
                       "type": "throttled" if throttled else "session_error",
                       "sessionId": label, "egress": active.label,
                       "error": {"message": reason, "consecutive": failures},
                       "abandonedSteps": partial})
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
        description="Collect Moon Sisters demo rounds through autonomous browser workers."
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Public game URL")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=10000,
                        help="Total rounds across all workers")
    parser.add_argument("--bet-per-line", type=int, default=4)
    parser.add_argument("--lines", type=int, default=25)
    parser.add_argument("--set-denominator", type=int, default=1)
    parser.add_argument("--rate", type=float, default=1.0,
                        help="Backend requests per second per egress IP")
    parser.add_argument("--session-cost", type=float, default=3.0,
                        help="Request budget charged for opening one session")
    parser.add_argument("--proxies",
                        help="Proxy list: file, URL, or 'proxifly' for the free proxifly list")
    parser.add_argument("--workers-per-proxy", type=int, default=1)
    parser.add_argument("--proxy-check-concurrency", type=int, default=50)
    parser.add_argument("--proxy-check-timeout", type=float, default=15.0)
    parser.add_argument("--proxy-refresh", type=float, default=600.0,
                        help="Reload the proxy list every N seconds")
    parser.add_argument("--proxy-max-failures", type=int, default=2,
                        help="Drop a proxy after N network failures in a row")
    parser.add_argument("--proxy-retries", type=int, default=0,
                        help="Retries on the same proxy before moving the session")
    parser.add_argument("--resume-window", type=float, default=45.0,
                        help="Seconds after the last answer within which a session must "
                             "be moved to another lane; the backend drops a silent "
                             "session after about 60")
    parser.add_argument("--resume-parallel", type=int, default=4,
                        help="Lanes tried at once when moving a session")
    parser.add_argument("--bonus-hedges", type=int, default=2,
                        help="Warm standby pages on other lanes kept during a bonus")
    parser.add_argument("--hedge-wait", type=float, default=12.0,
                        help="Seconds to wait for standby pages when a bonus starts")
    parser.add_argument("--hedge-page-timeout", type=float, default=15.0,
                        help="Page load timeout for standby pages, seconds")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="Single request timeout, seconds")
    parser.add_argument("--page-timeout", type=float, default=45.0,
                        help="Game page load timeout, seconds")
    parser.add_argument("--max-retries", type=int, default=6,
                        help="Retries of one request on transient errors before a new session")
    parser.add_argument("--backoff", type=float, default=2.0,
                        help="Base backoff, seconds; doubles per attempt")
    parser.add_argument("--max-backoff", type=float, default=300.0)
    parser.add_argument("--startup-stagger", type=float, default=3.0,
                        help="Delay between worker starts, seconds")
    parser.add_argument("--min-balance-spins", type=int, default=20,
                        help="Open a new session when fewer than N bets remain")
    parser.add_argument("--max-bonus-steps", type=int, default=60)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--label", default="MoonSisters",
                        help="File prefix; the artifact builder expects MoonSisters")
    parser.add_argument("--out-root", type=Path, default=Path("output/browser-runs"))
    parser.add_argument("--run-name")
    parser.add_argument("--raw-only", action="store_true",
                        help="Skip building the Stake and Artube artifacts")
    return parser.parse_args(argv)


async def collect(
    args: argparse.Namespace, run_dir: Path, run_id: str,
) -> tuple[RunState, list[dict[str, Any]], float, dict[str, Any]]:
    from playwright.async_api import async_playwright

    state = RunState(args.rounds)

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
                run_worker(
                    worker, browsers, lanes, args, state, reporter,
                    run_dir / f"{args.label}-worker-{worker:02d}-{run_id}.jsonl",
                )
                for worker in range(1, args.workers + 1)
            ))
        finally:
            state.stop = True
            checker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await checker
            await browsers.close()
    return state, list(results), time.monotonic() - started, lanes.summary()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.workers < 1 or args.rounds < 1 or args.rate <= 0 or args.workers_per_proxy < 1:
        raise SystemExit("--workers, --rounds, --rate and --workers-per-proxy must be positive.")
    if not args.raw_only:
        try:
            import zstandard  # noqa: F401
        except ImportError as error:
            raise SystemExit(
                "Artifact export needs zstandard: "
                "python -m pip install -r tools/requirements-artifacts.txt"
            ) from error

    run_id = uuid.uuid4().hex[:12]
    run_name = args.run_name or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{run_id}"
    run_dir = args.out_root / run_name
    if run_dir.exists() and any(run_dir.glob(f"{args.label}-worker-*.jsonl")):
        raise SystemExit(f"{run_dir} already holds a collected run; choose another --run-name.")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run={run_name} workers={args.workers} rounds={args.rounds} "
          f"rate={args.rate}/s per IP proxies={args.proxies or 'none'} out={run_dir}",
          flush=True)

    started_utc = iso_now()
    state, results, elapsed, egress = asyncio.run(collect(args, run_dir, run_id))

    manifest = {
        "run": run_name,
        "runId": run_id,
        "collector": "collect_moon_browser",
        "startedUtc": started_utc,
        "url": args.url,
        "workers": args.workers,
        "roundsRequested": args.rounds,
        "roundsCollected": state.done,
        "roundsAbandoned": state.abandoned,
        "sessionFailures": state.session_failures,
        "throttled": state.throttled,
        "rate": args.rate,
        "egress": egress,
        "failures": state.session_failures,
        "elapsedSeconds": round(elapsed, 3),
        "roundsPerSecond": round(state.done / elapsed, 3) if elapsed else None,
        "bet": {"betPerLine": args.bet_per_line, "lines": args.lines,
                "setDenominator": args.set_denominator},
        "workerStats": sorted(results, key=lambda item: item["worker"]),
    }
    (run_dir / "run.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"collected={state.done} abandoned={state.abandoned} "
          f"session_failures={state.session_failures} throttled={state.throttled}")
    print(f"elapsed={elapsed:.1f}s run_dir={run_dir}")
    if state.done <= 0:
        return 1
    if not args.raw_only:
        try:
            from build_moon_artifacts import convert

            report = convert(run_dir, args.workers)
            print("artifacts=" + json.dumps(report, ensure_ascii=False))
        except Exception as error:  # noqa: BLE001
            print(f"artifact export failed: {type(error).__name__}: {error}", file=sys.stderr)
            print("Raw collection is preserved; rerun build_moon_artifacts.py.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
