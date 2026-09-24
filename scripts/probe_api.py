"""Probe the live Hyperliquid API to answer V1-V7 from the build spec.

Dumps raw payloads to docs/samples/ and prints a summary dict.
Read-only, public endpoints only.
"""
import asyncio
import json
import time
from pathlib import Path

import httpx
import websockets

REST = "https://api.hyperliquid.xyz/info"
WS = "wss://api.hyperliquid.xyz/ws"
SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "samples"


def dump(name: str, obj) -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    (SAMPLES / f"{name}.json").write_text(json.dumps(obj, indent=2)[:2_000_000])


def post(client: httpx.Client, body: dict):
    r = client.post(REST, json=body, timeout=20)
    r.raise_for_status()
    return r.json()


def probe_rest() -> dict:
    out = {}
    with httpx.Client() as c:
        meta = post(c, {"type": "meta"})
        dump("meta", meta)
        ctxs = post(c, {"type": "metaAndAssetCtxs"})
        dump("metaAndAssetCtxs", ctxs)
        book = post(c, {"type": "l2Book", "coin": "BTC"})
        dump("l2Book_BTC_rest", book)

        universe = meta["universe"]
        out["V6_symbols_with_colon"] = [u["name"] for u in universe if ":" in u["name"]][:20]
        out["V6_universe_n"] = len(universe)
        out["V6_sample_entry"] = universe[0]
        out["V7_ctx_keys"] = sorted(ctxs[1][0].keys())
        out["V3_rest_levels"] = [len(book["levels"][0]), len(book["levels"][1])]
        out["V3_rest_level_shape"] = book["levels"][0][0]

        # V5: modest burst to observe rate-limit behaviour (60 requests, sequential)
        t0 = time.time()
        codes = []
        for _ in range(60):
            r = c.post(REST, json={"type": "l2Book", "coin": "BTC"}, timeout=20)
            codes.append(r.status_code)
            if r.status_code != 200:
                out["V5_limit_headers"] = dict(r.headers)
                break
        out["V5_burst"] = {
            "n": len(codes),
            "elapsed_s": round(time.time() - t0, 2),
            "rps": round(len(codes) / (time.time() - t0), 1),
            "non_200": [c_ for c_ in codes if c_ != 200],
        }
    return out


async def probe_ws() -> dict:
    out = {}
    async with websockets.connect(WS, max_size=None) as ws:
        # V1: trades counterparties + V3: l2Book cadence/levels + V7: activeAssetCtx
        for sub in (
            {"type": "trades", "coin": "BTC"},
            {"type": "l2Book", "coin": "BTC"},
            {"type": "activeAssetCtx", "coin": "BTC"},
        ):
            await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

        book_ts, trades, ctx_msgs, book_msgs = [], [], [], []
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - time.time()))
            except asyncio.TimeoutError:
                break
            ch = msg.get("channel")
            if ch == "l2Book":
                book_ts.append(time.time())
                book_msgs.append(msg)
            elif ch == "trades":
                trades.extend(msg["data"])
            elif ch == "activeAssetCtx":
                ctx_msgs.append(msg)

        dump("ws_trades_BTC", trades[:50])
        dump("ws_l2Book_BTC", book_msgs[:3])
        dump("ws_activeAssetCtx_BTC", ctx_msgs[:5])

        gaps = [round(b - a, 3) for a, b in zip(book_ts, book_ts[1:])]
        out["V3_ws"] = {
            "msgs_30s": len(book_ts),
            "median_gap_s": sorted(gaps)[len(gaps) // 2] if gaps else None,
            "levels_per_side": [len(book_msgs[0]["data"]["levels"][i]) for i in (0, 1)]
            if book_msgs else None,
        }
        out["V1"] = {
            "n_trades_30s": len(trades),
            "trade_keys": sorted(trades[0].keys()) if trades else None,
            "sample_trade": trades[0] if trades else None,
            "has_addresses": bool(trades) and any(
                isinstance(v, list) and v and isinstance(v[0], str) and v[0].startswith("0x")
                for v in trades[0].values()
            ),
        }
        out["V7_ws"] = {
            "msgs_30s": len(ctx_msgs),
            "ctx_keys": sorted(ctx_msgs[0]["data"]["ctx"].keys()) if ctx_msgs else None,
        }

    # V2: how many l2Book subs fit on one connection
    async with websockets.connect(WS, max_size=None) as ws:
        with httpx.Client() as c:
            coins = [u["name"] for u in post(c, {"type": "meta"})["universe"]][:200]
        ok, err = 0, None
        for coin in coins:
            await ws.send(json.dumps(
                {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}))
            try:
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
                err = f"{type(e).__name__} after {ok} subs"
                break
            if resp.get("channel") == "error":
                err = resp["data"]
                break
            ok += 1
        out["V2"] = {"subs_accepted": ok, "attempted": len(coins), "stop_reason": err}
    return out


def top_by_volume(n: int = 60):
    with httpx.Client() as c:
        meta, ctxs = post(c, {"type": "metaAndAssetCtxs"})
    rows = [
        {"coin": u["name"], "vol24h_usd": float(x["dayNtlVlm"]), "oi": float(x["openInterest"]),
         "szDecimals": u["szDecimals"], "maxLeverage": u["maxLeverage"]}
        for u, x in zip(meta["universe"], ctxs)
        if not u.get("isDelisted")
    ]
    rows.sort(key=lambda r: -r["vol24h_usd"])
    dump("volume_ranking", rows)
    return rows[:n]


async def main() -> None:
    summary = {"probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    summary.update(probe_rest())
    summary.update(await probe_ws())
    summary["top20_by_volume"] = [
        (r["coin"], round(r["vol24h_usd"] / 1e6, 1)) for r in top_by_volume(20)
    ]
    dump("probe_summary", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
