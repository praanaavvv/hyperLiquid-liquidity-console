"""M1 crude collector: raw WS capture to hourly JSONL, atomic rename on roll.

Deliberately dumb - book depth cannot be backfilled, so this runs while M2's
real parquet collector is built. Raw payloads are loss-free, so nothing
collected here is wasted: M2 replays these files.

Usage: uv run --python 3.11 collector/crude.py BTC xyz:SP500
Retention: date= dirs older than HL_KEEP_DAYS (default 7) are deleted on each hour roll.
"""
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import websockets

WS = "wss://api.hyperliquid.xyz/ws"
CHANNELS = ("bbo", "l2Book", "trades", "activeAssetCtx")
ROOT = Path(__file__).resolve().parents[1] / "data" / "crude"
KEEP_DAYS = int(os.environ.get("HL_KEEP_DAYS", 7))


def prune(root: Path, keep_days: int, today: date | None = None) -> list[Path]:
    """Delete coin=*/date=YYYY-MM-DD dirs older than keep_days (UTC). Returns what was removed."""
    cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=keep_days)
    gone = []
    for d in root.glob("coin=*/date=*"):
        try:
            day = date.fromisoformat(d.name.removeprefix("date="))
        except ValueError:
            continue
        if day < cutoff:
            shutil.rmtree(d)
            gone.append(d)
    return gone


def path_for(coin: str, hour_epoch: int) -> Path:
    t = time.gmtime(hour_epoch * 3600)
    p = ROOT / f"coin={coin.replace(':', '_')}" / time.strftime("date=%Y-%m-%d", t)
    p.mkdir(parents=True, exist_ok=True)
    return p / time.strftime("hour=%H.jsonl", t)


class HourlyWriter:
    """One .tmp file per coin-hour, atomically renamed when the hour rolls."""

    def __init__(self) -> None:
        self.files: dict[tuple[str, int], object] = {}

    def write(self, coin: str, row: dict) -> None:
        hour = int(time.time()) // 3600
        key = (coin, hour)
        if key not in self.files:
            self.close_stale(hour)
            final = path_for(coin, hour)
            self.files[key] = (open(final.with_suffix(".jsonl.tmp"), "a"), final)
        f, _ = self.files[key]
        f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def close_stale(self, hour: int) -> None:
        # ponytail: rolls only when some coin writes in the new hour. A fully silent
        # set of coins leaves a .tmp open. Add a timer task if T3/T4 coins go quiet
        # across an hour boundary - harmless here since .tmp is readable and complete.
        for key in [k for k in self.files if k[1] != hour]:
            f, final = self.files.pop(key)
            f.flush()
            f.close()
            Path(f.name).rename(final)

    def flush(self) -> None:
        for f, _ in self.files.values():
            f.flush()

    def close_all(self) -> None:
        self.close_stale(-1)


async def run(coins: list[str]) -> None:
    w = HourlyWriter()
    ledger = ROOT / "uptime.jsonl"
    ROOT.mkdir(parents=True, exist_ok=True)

    def log(event: str, **kw) -> None:
        rec = {"ts": time.time_ns(), "event": event, **kw}
        with open(ledger, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(json.dumps(rec), flush=True)

    last_prune = -1
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS, max_size=None, ping_interval=20) as ws:
                for coin in coins:
                    for ch in CHANNELS:
                        await ws.send(json.dumps(
                            {"method": "subscribe", "subscription": {"type": ch, "coin": coin}}))
                log("connected", coins=coins, channels=list(CHANNELS))
                backoff = 1
                n, last_flush = 0, time.time()
                async for raw in ws:
                    msg = json.loads(raw)
                    ch = msg.get("channel")
                    if ch not in CHANNELS:
                        continue
                    ts = time.time_ns()
                    data = msg["data"]
                    # trades arrive as a list; everything else as one object
                    coin = data[0]["coin"] if isinstance(data, list) and data else (
                        data.get("coin") if isinstance(data, dict) else None)
                    if coin is None:
                        continue
                    w.write(coin, {"ts_local_ns": ts, "channel": ch, "data": data})
                    n += 1
                    hour = int(time.time()) // 3600
                    if hour != last_prune:
                        last_prune = hour
                        if gone := prune(ROOT, KEEP_DAYS):
                            log("pruned", dirs=[str(d.relative_to(ROOT)) for d in gone])
                    if time.time() - last_flush > 5:
                        w.flush()
                        last_flush = time.time()
        except Exception as e:  # network loss, venue restart, malformed frame
            log("disconnected", error=repr(e), backoff_s=backoff)
            w.flush()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    coins = sys.argv[1:] or ["BTC", "xyz:SP500"]
    try:
        asyncio.run(run(coins))
    except KeyboardInterrupt:
        pass
