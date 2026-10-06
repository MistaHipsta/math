"""Live metrics for a running `collect_moon_browser.py` run.

Reads only the bytes appended since the previous call (offsets are cached in
`<run>/.status-cache.json`), so it stays fast on multi-gigabyte runs and is
safe to call while the collector is writing.

    python tools/run_status.py output/browser-runs/run-1m
    python tools/run_status.py output/browser-runs/run-1m --watch 60
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

PROGRESS = re.compile(r"^rounds=(\d+)/(\d+) .*?proxies=(\d+)")


def fresh_counters() -> dict[str, Any]:
    return {"rounds": 0, "bonus": 0, "bonusSteps": 0, "maxBonusSteps": 0,
            "bonusAcrossIps": 0, "stake": 0, "payout": 0, "maxPayout": 0,
            "migrations": 0, "midBonusMigrations": 0, "sessionErrors": 0,
            "abandoned": 0, "throttled": 0, "badLines": 0,
            "egress": {}, "offsets": {}, "samples": []}


def absorb(counters: dict[str, Any], event: dict[str, Any], now: float) -> None:
    kind = event.get("type")
    if kind == "round_response":
        response = event["response"]
        steps = response.get("steps") or []
        counters["rounds"] += 1
        try:
            spins = json.loads(response["RawJson"])["context"]["spins"]
            payout = int(spins.get("round_win") or 0)
            counters["stake"] += int(spins.get("round_bet") or 0)
            counters["payout"] += payout
            counters["maxPayout"] = max(counters["maxPayout"], payout)
        except (KeyError, ValueError, TypeError):
            counters["badLines"] += 1
        if len(steps) > 1:
            counters["bonus"] += 1
            counters["bonusSteps"] += len(steps)
            counters["maxBonusSteps"] = max(counters["maxBonusSteps"], len(steps))
            if len({step.get("egress") for step in steps}) > 1:
                counters["bonusAcrossIps"] += 1
        egress = event.get("egress")
        if egress:
            counters["egress"][egress] = now
    elif kind == "migration":
        counters["migrations"] += 1
        counters["midBonusMigrations"] += bool(event.get("midBonus"))
    elif kind == "session_error":
        counters["sessionErrors"] += 1
        counters["abandoned"] += bool(event.get("abandonedSteps"))
    elif kind == "throttled":
        counters["throttled"] += 1


def scan(run_dir: Path) -> tuple[dict[str, Any], float]:
    cache_path = run_dir / ".status-cache.json"
    try:
        counters = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        counters = fresh_counters()
    now = time.time()
    last_write = 0.0
    for path in sorted(run_dir.glob("*-worker-*.jsonl")):
        last_write = max(last_write, path.stat().st_mtime)
        offset = counters["offsets"].get(path.name, 0)
        with path.open("rb") as stream:
            stream.seek(offset)
            for line in stream:
                if not line.endswith(b"\n"):
                    break  # still being written; read it next time
                offset += len(line)
                try:
                    absorb(counters, json.loads(line), now)
                except ValueError:
                    counters["badLines"] += 1
        counters["offsets"][path.name] = offset
    counters["samples"] = [s for s in counters["samples"] if now - s[0] <= 3600]
    counters["samples"].append([now, counters["rounds"]])
    counters["egress"] = {ip: seen for ip, seen in counters["egress"].items()
                          if now - seen <= 3600}
    cache_path.write_text(json.dumps(counters), encoding="utf-8")
    return counters, last_write


def rate_since(samples: list[list[float]], seconds: float) -> float | None:
    if len(samples) < 2:
        return None
    now, rounds = samples[-1]
    older = [s for s in samples if now - s[0] >= seconds] or samples[:1]
    then, before = older[-1]
    return (rounds - before) / (now - then) if now > then else None


def log_tail(log: Path) -> tuple[str | None, str | None, int | None]:
    """Last progress line, last proxy-pool line and the target round count."""
    if not log.exists():
        return None, None, None
    with log.open("rb") as stream:
        stream.seek(max(0, log.stat().st_size - 200_000))
        lines = stream.read().decode("utf-8", errors="replace").splitlines()
    progress = next((l for l in reversed(lines) if l.startswith("rounds=")), None)
    pool = next((l for l in reversed(lines) if l.startswith("proxies:")), None)
    total = None
    head = log.open(encoding="utf-8", errors="replace").readline()
    match = re.search(r"rounds=(\d+)", head)
    if match:
        total = int(match.group(1))
    return progress, pool, total


def human(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def report(run_dir: Path) -> str:
    counters, last_write = scan(run_dir)
    progress, pool, total = log_tail(run_dir.parent / f"{run_dir.name}.log")
    rounds = counters["rounds"]
    now = time.time()
    rate_10m = rate_since(counters["samples"], 600)
    remaining = (total - rounds) if total else None
    eta = remaining / rate_10m if remaining and rate_10m else None
    size = sum(p.stat().st_size for p in run_dir.glob("*-worker-*.jsonl"))
    finished = (run_dir / "run.json").exists()
    idle = now - last_write if last_write else None
    status = ("FINISHED" if finished else
              "STALLED?" if idle is not None and idle > 300 else "running")
    bonus = counters["bonus"]
    rtp = counters["payout"] / counters["stake"] if counters["stake"] else None
    active_ips = sum(now - seen <= 600 for seen in counters["egress"].values())

    lines = [
        f"run {run_dir.name}  [{status}]  {time.strftime('%H:%M:%S')}",
        f"rounds        {rounds:,}" + (f" / {total:,} ({rounds / total:.2%})" if total else ""),
        f"speed         {rate_10m:.2f}/s over 10 min" if rate_10m is not None
        else "speed         ? (call again in a minute)",
        f"ETA           {human(eta)}",
        f"bonus rounds  {bonus:,} ({bonus / rounds:.2%})" if rounds else "bonus rounds  0",
        f"bonus steps   avg {counters['bonusSteps'] / bonus:.1f}, max {counters['maxBonusSteps']}"
        if bonus else "bonus steps   -",
        f"RTP so far    {rtp:.4f}  (max win {counters['maxPayout']:,} credits)" if rtp else "RTP so far    -",
        f"abandoned     {counters['abandoned']}   <- rounds lost mid-bonus, must stay 0",
        f"migrations    {counters['migrations']:,} (mid-bonus {counters['midBonusMigrations']}, "
        f"bonuses finished on another IP {counters['bonusAcrossIps']})",
        f"session errs  {counters['sessionErrors']:,}   throttled {counters['throttled']}",
        f"proxies       {active_ips} produced rounds in last 10 min",
        f"disk          {size / 2**30:.2f} GiB, last write {int(idle) if idle is not None else '?'}s ago",
    ]
    if counters["badLines"]:
        lines.append(f"bad lines     {counters['badLines']}")
    if progress:
        lines.append(f"log           {progress}")
    if pool:
        lines.append(f"pool          {pool}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--watch", type=float, help="Refresh every N seconds")
    args = parser.parse_args()
    if not args.run_dir.is_dir():
        raise SystemExit(f"{args.run_dir} is not a run folder")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    while True:
        text = report(args.run_dir)
        if args.watch:
            os.system("cls" if os.name == "nt" else "clear")
        print(text, flush=True)
        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
