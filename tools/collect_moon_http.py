"""Headless Moon Sisters demo round collector.

Sends POST `play` commands straight to the game backend without a browser,
fans the load out across worker threads, assigns a fresh `request_id` to
every call and stores each raw response in a per-run directory.

Output layout (one JSONL per worker, converter-compatible):

    <run-dir>/MoonSisters-worker-01-<runid>.jsonl
    <run-dir>/MoonSisters-worker-02-<runid>.jsonl
    <run-dir>/run.json

Every successful line is a `round_response` event whose `response.RawJson`
holds the untouched backend payload, so `convert_moon_raw_to_round_results.py`
and `analyze_moon_jsonl.py` consume this output unchanged.

Session material (URL session segment, `session_id`) is never hardcoded: it
is supplied per run via CLI or a sessions file and is redacted in logs.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import json
import random
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSION_SEGMENT = re.compile(r"/desktop/([0-9A-Za-z]{8,})/demo/")

REDACT_KEYS = {
    "session_id",
    "sessionid",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "jwt",
    "password",
}

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Backend codes meaning the session is gone and a new login is required.
SESSION_LOST = {
    "GAME_REOPENED",
    "SESSION_NOT_FOUND",
    "SESSION_EXPIRED",
    "SESSION_CLOSED",
    "INVALID_SESSION",
    "NO_SESSION",
}

DEFAULT_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "text/plain",
    "origin": "https://3oaks.com",
    "referer": "https://3oaks.com/",
    "priority": "u=1, i",
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", '
    '"Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_request_id() -> str:
    return uuid.uuid4().hex


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if str(key).lower() in REDACT_KEYS
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def path_session_of(url: str) -> str | None:
    match = SESSION_SEGMENT.search(url)
    return match.group(1) if match else None


def safe_url(url: str) -> str:
    return SESSION_SEGMENT.sub("/desktop/{session}/demo/", url)


class RoundBudget:
    """Claim-based budget so retries never overshoot the requested total."""

    def __init__(self, total: int) -> None:
        self.total = total
        self._claimed = 0
        self._done = 0
        self._failed = 0
        self._consecutive_failures = 0
        self._lock = threading.Lock()
        self.stop = threading.Event()

    def claim(self) -> bool:
        with self._lock:
            if self.stop.is_set() or self._claimed >= self.total:
                return False
            self._claimed += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._claimed -= 1

    def complete(self) -> int:
        with self._lock:
            self._done += 1
            self._consecutive_failures = 0
            done = self._done
        if done >= self.total:
            self.stop.set()
        return done

    def fail(self, limit: int) -> int:
        with self._lock:
            self._failed += 1
            self._consecutive_failures += 1
            streak = self._consecutive_failures
        if limit > 0 and streak >= limit:
            self.stop.set()
        return streak

    @property
    def done(self) -> int:
        with self._lock:
            return self._done

    @property
    def failed(self) -> int:
        with self._lock:
            return self._failed


def read_body(response: Any) -> str:
    raw = response.read()
    if response.headers.get("content-encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def post_play(
    url: str,
    body: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str, str]:
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                read_body(response),
                response.headers.get("content-type", ""),
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            read_body(error),
            error.headers.get("content-type", "") if error.headers else "",
        )


def send_command(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    """POST a game command and return the parsed payload, or raise."""
    command = payload["command"]
    endpoint = f"{url.split('?')[0]}?gsc={command}"
    status, raw, _ = post_play(
        endpoint,
        json.dumps(payload, separators=(",", ":")),
        headers,
        timeout,
    )
    if status != 200:
        raise RuntimeError(f"{command} failed with HTTP {status}")

    body = json.loads(raw)
    code = str((body.get("status") or {}).get("code", "")).upper()
    if code not in {"OK", "SUCCESS", ""}:
        raise RuntimeError(f"{command} rejected: {code}")
    return body


def open_session(
    url: str,
    token: str,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, str]:
    """Run the full `login` -> `start` handshake and return session material.

    The backend only accepts `play` once the session has been started, so
    skipping `start` makes every spin fail with SERVER_ERROR. Each worker
    performs its own handshake to avoid GAME_REOPENED collisions.
    """
    login_response = send_command(
        url,
        {
            "command": "login",
            "request_id": new_request_id(),
            "token": token,
            "language": "en",
            "client_command_timestamp": int(time.time() * 1000),
        },
        headers,
        timeout,
    )

    session_id = login_response.get("session_id")
    if not session_id:
        raise RuntimeError("login response contains no session_id")

    huid = (login_response.get("user") or {}).get("huid")
    modes = login_response.get("modes") or ["auto"]
    mode = "auto" if "auto" in modes else str(modes[0])

    send_command(
        url,
        {
            "command": "start",
            "request_id": new_request_id(),
            "session_id": session_id,
            "mode": mode,
            "huid": huid,
            "client_command_timestamp": int(time.time() * 1000),
        },
        headers,
        timeout,
    )

    return {"session_id": str(session_id), "huid": str(huid or ""), "mode": mode}


def build_action_payload(
    session_id: str,
    action: str,
    prev_command_ms: int,
) -> dict[str, Any]:
    """Body for a follow-up round action such as bonus_init or respin."""
    return {
        "command": "play",
        "request_id": new_request_id(),
        "session_id": session_id,
        "action": {"name": action, "params": {}},
        "set_denominator": 1,
        "quick_spin": 1,
        "sound": False,
        "autogame": False,
        "mobile": "0",
        "portrait": False,
        "fullscreen": True,
        "prev_client_command_time": prev_command_ms,
        "client_command_timestamp": int(time.time() * 1000),
    }


def classify_payload(raw: str) -> str | None:
    """Return None for a usable round, otherwise a short rejection reason.

    The backend answers HTTP 200 even for critical refusals such as
    `GAME_REOPENED`, so a round is only accepted when it actually carries
    `context.spins.board`.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "not_json"
    if not isinstance(payload, dict):
        return "not_object"
    if str(payload.get("command", "")).lower() != "play":
        return "not_play"

    status = payload.get("status")
    if isinstance(status, dict):
        code = str(status.get("code", ""))
        if code and code.upper() not in {"OK", "SUCCESS"}:
            return code.upper()

    spins = (payload.get("context") or {}).get("spins")
    if not isinstance(spins, dict) or not isinstance(spins.get("board"), list):
        return "no_board"
    return None
def build_payload(
    args: argparse.Namespace,
    session_id: str,
    request_id: str,
    prev_command_ms: int,
) -> str:
    payload = {
        "command": "play",
        "request_id": request_id,
        "session_id": session_id,
        "action": {
            "name": "spin",
            "params": {
                "bet_per_line": args.bet_per_line,
                "lines": args.lines,
            },
        },
        "set_denominator": args.set_denominator,
        "quick_spin": 1,
        "sound": False,
        "autogame": False,
        "mobile": "0",
        "portrait": False,
        "fullscreen": True,
        "prev_client_command_time": prev_command_ms,
        "client_command_timestamp": int(time.time() * 1000),
    }
    return json.dumps(payload, separators=(",", ":"))


class Reporter:
    def __init__(self, budget: RoundBudget, every: int) -> None:
        self.budget = budget
        self.every = max(1, every)
        self.started = time.monotonic()
        self._lock = threading.Lock()

    def tick(self, done: int) -> None:
        if done % self.every and done != self.budget.total:
            return
        elapsed = time.monotonic() - self.started
        rate = done / elapsed if elapsed > 0 else 0.0
        remaining = self.budget.total - done
        eta = remaining / rate if rate > 0 else float("inf")
        with self._lock:
            print(
                f"rounds={done}/{self.budget.total} "
                f"failed={self.budget.failed} "
                f"rate={rate:.2f}/s "
                f"elapsed={elapsed:.0f}s "
                f"eta={eta:.0f}s",
                flush=True,
            )


def run_worker(
    worker: int,
    session: dict[str, str],
    args: argparse.Namespace,
    budget: RoundBudget,
    reporter: Reporter,
    run_dir: Path,
    run_id: str,
) -> dict[str, int]:
    url = session["url"]
    session_label = f"MoonSisters-worker-{worker:02d}-{run_id}"
    path = run_dir / f"{session_label}.jsonl"
    headers = dict(DEFAULT_HEADERS)
    display_url = safe_url(url)
    stats = {
        "ok": 0,
        "http_error": 0,
        "rejected": 0,
        "network_error": 0,
        "logins": 0,
    }
    prev_command_ms = 0

    try:
        opened = open_session(url, session["token"], headers, args.timeout)
        session_id = opened["session_id"]
        stats["logins"] += 1
    except Exception as error:  # noqa: BLE001 - reported, worker gives up
        print(f"[worker-{worker:02d}] handshake failed: {error}", flush=True)
        stats["login_error"] = 1
        return stats

    with path.open("w", encoding="utf-8") as stream:

        def write(record: dict[str, Any]) -> None:
            stream.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            stream.flush()

        while budget.claim():
            request_id = new_request_id()
            body = build_payload(args, session_id, request_id, prev_command_ms)
            started = time.monotonic()
            status = 0
            raw = ""
            content_type = ""
            failure = None

            for attempt in range(args.max_retries + 1):
                if budget.stop.is_set():
                    break
                try:
                    status, raw, content_type = post_play(
                        url, body, headers, args.timeout
                    )
                    failure = None
                    if status not in RETRY_STATUS:
                        break
                    failure = f"http_{status}"
                except (urllib.error.URLError, TimeoutError, OSError) as error:
                    failure = type(error).__name__
                    status = 0

                if attempt < args.max_retries:
                    backoff = args.retry_backoff * (2**attempt)
                    time.sleep(backoff + random.uniform(0, backoff / 2))

            prev_command_ms = int((time.monotonic() - started) * 1000)

            if failure is not None:
                budget.release()
                streak = budget.fail(args.max_consecutive_failures)
                stats["network_error" if status == 0 else "http_error"] += 1
                write(
                    {
                        "schemaVersion": 1,
                        "timestamp": iso_now(),
                        "type": "round_error",
                        "sessionId": session_label,
                        "correlationId": request_id,
                        "error": {"code": failure, "status": status},
                    }
                )
                if budget.stop.is_set():
                    print(
                        f"[worker-{worker:02d}] stopping after "
                        f"{streak} consecutive failures ({failure}).",
                        flush=True,
                    )
                    break
                time.sleep(args.delay)
                continue

            reason = classify_payload(raw)

            # A round may continue into a bonus: keep following the offered
            # actions (bonus_init -> respin... -> bonus_spins_stop) until the
            # backend marks it finished, otherwise the bonus win is lost and
            # the unfinished round poisons the session.
            bonus_steps = 0
            play_history: list[dict[str, Any]] = []
            if reason is None:
                spin_board = (
                    (json.loads(raw).get("context") or {})
                    .get("spins", {})
                    .get("board")
                )
                context = json.loads(raw).get("context") or {}
                play_history.append(copy.deepcopy(context))

                while (
                    context.get("round_finished") is False
                    and bonus_steps < args.max_bonus_steps
                ):
                    actions = context.get("actions") or []
                    if not actions:
                        break
                    bonus_steps += 1
                    action_started = time.monotonic()
                    try:
                        status, raw, content_type = post_play(
                            url,
                            json.dumps(
                                build_action_payload(
                                    session_id, actions[0], prev_command_ms
                                ),
                                separators=(",", ":"),
                            ),
                            headers,
                            args.timeout,
                        )
                    except (urllib.error.URLError, TimeoutError, OSError):
                        reason = "bonus_request_failed"
                        break
                    prev_command_ms = int(
                        (time.monotonic() - action_started) * 1000
                    )
                    if status != 200:
                        reason = f"bonus_http_{status}"
                        break
                    context = json.loads(raw).get("context") or {}
                    play_history.append(copy.deepcopy(context))

                if context.get("round_finished") is False and reason is None:
                    reason = "round_unfinished"

                if reason is None and bonus_steps:
                    # Preserve the spin board that triggered the bonus; the
                    # closing response carries the final total_win.
                    final = json.loads(raw)
                    spins = final.setdefault("context", {}).setdefault(
                        "spins", {}
                    )
                    if spin_board is not None:
                        spins["board"] = spin_board
                    spins["round_win"] = spins.get(
                        "total_win", spins.get("round_win")
                    )
                    spins["bonus_steps"] = bonus_steps
                    raw = json.dumps(final, separators=(",", ":"))
                    stats["bonus_rounds"] = stats.get("bonus_rounds", 0) + 1

            record = {
                "schemaVersion": 1,
                "timestamp": iso_now(),
                "type": "round_response" if reason is None else
                        "unparsed_response",
                "sessionId": session_label,
                "response": {
                    "TimestampUtc": iso_now(),
                    "CorrelationId": request_id,
                    "Url": display_url,
                    "Method": "POST",
                    "Status": status,
                    "ContentType": content_type,
                    "BodyBytes": len(raw.encode("utf-8")),
                    "RawJson": raw[: args.max_response_bytes],
                    "playHistory": play_history,
                    "Error": None if reason is None else {"code": reason},
                },
            }
            write(record)

            if reason is not None:
                budget.release()
                stats["rejected"] = stats.get("rejected", 0) + 1
                stats[f"reason_{reason}"] = stats.get(f"reason_{reason}", 0) + 1

                if args.max_logins <= 0 or stats["logins"] <= args.max_logins:
                    # Any rejected spin (lost session, drained demo balance,
                    # server error) is recovered by opening a new session on
                    # this same worker instead of burning the failure budget.
                    try:
                        opened = open_session(
                            url, session["token"], headers, args.timeout
                        )
                        session_id = opened["session_id"]
                        stats["logins"] += 1
                        time.sleep(args.delay)
                        continue
                    except Exception as error:  # noqa: BLE001
                        print(
                            f"[worker-{worker:02d}] re-login failed: {error}",
                            flush=True,
                        )

                budget.fail(args.max_consecutive_failures)
                if budget.stop.is_set():
                    print(
                        f"[worker-{worker:02d}] stopping: backend rejected "
                        f"spins ({reason}). The session is likely bound to a "
                        "single concurrent player.",
                        flush=True,
                    )
                    break
                time.sleep(args.delay)
                continue

            stats["ok"] += 1
            reporter.tick(budget.complete())

            # The demo wallet is finite, so refresh the session before it runs
            # dry instead of waiting for the backend to start rejecting spins.
            balance = (json.loads(raw).get("user") or {}).get("balance")
            stake = args.bet_per_line * args.lines
            if balance is not None and balance < stake * args.min_balance_spins:
                try:
                    opened = open_session(
                        url, session["token"], headers, args.timeout
                    )
                    session_id = opened["session_id"]
                    stats["logins"] += 1
                    stats["balance_refresh"] = stats.get("balance_refresh", 0) + 1
                except Exception as error:  # noqa: BLE001
                    print(
                        f"[worker-{worker:02d}] balance re-login failed: {error}",
                        flush=True,
                    )

            if args.delay > 0:
                time.sleep(args.delay)

    stats["file"] = str(path)
    return stats


def load_sessions(args: argparse.Namespace) -> list[dict[str, str]]:
    """Return the endpoints each worker logs in against.

    Only the launch URL and the demo token are needed: every worker opens its
    own session via `?gsc=login`, so no session_id is carried in from outside.
    """
    if args.sessions_file:
        data = json.loads(Path(args.sessions_file).read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            raise SystemExit("--sessions-file must contain a non-empty array.")
        sessions = []
        for entry in data:
            url = str(entry["url"])
            token = str(entry.get("token") or args.token or "")
            if not token:
                raise SystemExit(
                    "Each sessions-file entry needs a token, or pass --token."
                )
            sessions.append({"url": url, "token": token})
        return sessions

    if not args.url or not args.token:
        raise SystemExit("Provide --url and --token, or --sessions-file.")
    return [{"url": args.url, "token": args.token}]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Moon Sisters demo rounds over plain HTTP."
    )
    parser.add_argument("--url", help="Full play URL including ?gsc=play")
    parser.add_argument(
        "--token",
        help="Demo launch token; each worker logs in and gets its own session",
    )
    parser.add_argument(
        "--sessions-file",
        help="JSON array of {\"url\":..., \"token\":...} for multi-endpoint runs",
    )
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument(
        "--min-balance-spins",
        type=int,
        default=20,
        help="Re-open the session when fewer than N spins remain affordable",
    )
    parser.add_argument(
        "--max-logins",
        type=int,
        default=0,
        help="Maximum session re-opens per worker; 0 means unlimited",
    )
    parser.add_argument("--rounds", type=int, default=10000)
    parser.add_argument("--bet-per-line", type=int, default=4)
    parser.add_argument("--lines", type=int, default=25)
    parser.add_argument("--set-denominator", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-backoff", type=float, default=0.5)
    parser.add_argument("--max-consecutive-failures", type=int, default=25)
    parser.add_argument(
        "--max-bonus-steps",
        type=int,
        default=60,
        help="Maximum follow-up actions used to finish one bonus round",
    )
    parser.add_argument("--max-response-bytes", type=int, default=1_048_576)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("output/http-runs"),
        help="Parent folder; each run gets its own timestamped subfolder",
    )
    parser.add_argument("--run-name", help="Override the run folder name")
    parser.add_argument(
        "--raw-only",
        action="store_true",
        help="Collect raw responses without building the Stake and Artube artifacts",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.workers < 1 or args.rounds < 1:
        raise SystemExit("--workers and --rounds must be positive.")
    if not args.raw_only:
        try:
            import zstandard  # noqa: F401
        except ImportError as error:
            raise SystemExit(
                "Artifact export needs zstandard. Install it before collection: "
                "python -m pip install -r tools/requirements-artifacts.txt"
            ) from error

    sessions = load_sessions(args)
    run_id = uuid.uuid4().hex[:12]
    run_name = args.run_name or (
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{run_id}"
    )
    run_dir = args.out_root / run_name
    if run_dir.exists() and any(run_dir.glob("MoonSisters-worker-*.jsonl")):
        # Reusing a run name would silently mix two collections in one folder
        # and inflate every downstream count.
        raise SystemExit(
            f"{run_dir} already holds a collected run; "
            "choose another --run-name or remove the folder."
        )
    run_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"each worker opens its own session via ?gsc=login "
        f"({len(sessions)} endpoint(s), {args.workers} worker(s))",
        flush=True,
    )

    budget = RoundBudget(args.rounds)
    reporter = Reporter(budget, args.progress_every)

    def on_signal(*_: Any) -> None:
        if not budget.stop.is_set():
            print("cancellation requested; stopping workers.", flush=True)
            budget.stop.set()

    signal.signal(signal.SIGINT, on_signal)

    print(
        f"run={run_name} workers={args.workers} rounds={args.rounds} "
        f"out={run_dir}",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    results_lock = threading.Lock()
    threads: list[threading.Thread] = []

    def target(worker: int) -> None:
        stats = run_worker(
            worker,
            sessions[(worker - 1) % len(sessions)],
            args,
            budget,
            reporter,
            run_dir,
            run_id,
        )
        stats["worker"] = worker
        with results_lock:
            results.append(stats)

    started = time.monotonic()
    for worker in range(1, args.workers + 1):
        thread = threading.Thread(target=target, args=(worker,), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started

    manifest = {
        "run": run_name,
        "runId": run_id,
        "startedUtc": iso_now(),
        "workers": args.workers,
        "roundsRequested": args.rounds,
        "roundsCollected": budget.done,
        "failures": budget.failed,
        "elapsedSeconds": round(elapsed, 3),
        "roundsPerSecond": round(budget.done / elapsed, 3) if elapsed else None,
        "url": safe_url(sessions[0]["url"]),
        "bet": {
            "betPerLine": args.bet_per_line,
            "lines": args.lines,
            "setDenominator": args.set_denominator,
        },
        "workerStats": sorted(results, key=lambda item: item["worker"]),
    }
    (run_dir / "run.json").write_text(
        json.dumps(redact(manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"collected={budget.done} failed={budget.failed}")
    print(f"elapsed={elapsed:.1f}s")
    print(f"run_dir={run_dir}")
    if budget.done <= 0:
        return 1
    if not args.raw_only:
        try:
            from build_moon_artifacts import convert

            report = convert(run_dir, args.workers)
            print("artifacts=" + json.dumps(report, ensure_ascii=False))
            print(f"stake_artifact={run_dir / 'converted-artifact' / 'stake'}")
            print(f"artube_artifact={run_dir / 'converted-artifact' / 'artube'}")
        except Exception as error:  # noqa: BLE001
            print(f"artifact export failed: {type(error).__name__}: {error}", file=sys.stderr)
            print("Raw collection is preserved; rerun build_moon_artifacts.py after fixing the issue.", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
