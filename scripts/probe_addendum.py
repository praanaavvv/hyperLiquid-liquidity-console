"""Follow-ups the first probe left open: maker/taker order in trades.users,
true l2Book cadence on a dedicated connection, HIP-3 (builder-deployed) markets."""
import asyncio, json, time
from pathlib import Path
import httpx, websockets

REST = "https://api.hyperliquid.xyz/info"
WS = "wss://api.hyperliquid.xyz/ws"
SAMPLES = Path(__file__).resolve().parents[1] / "docs" / "samples"

def post(c, body):
    r = c.post(REST, json=body, timeout=20); r.raise_for_status(); return r.json()

def dump(name, obj):
    (SAMPLES / f"{name}.json").write_text(json.dumps(obj, indent=2)[:2_000_000])

async def collect_trades(coin="BTC", secs=20):
    async with websockets.connect(WS, max_size=None) as ws:
        await ws.send(json.dumps({"method":"subscribe","subscription":{"type":"trades","coin":coin}}))
        out, end = [], time.time()+secs
        while time.time() < end:
            try: m = json.loads(await asyncio.wait_for(ws.recv(), timeout=end-time.time()))
            except asyncio.TimeoutError: break
            if m.get("channel") == "trades": out.extend(m["data"])
        return out

async def book_cadence(coin, secs=30):
    async with websockets.connect(WS, max_size=None) as ws:
        await ws.send(json.dumps({"method":"subscribe","subscription":{"type":"l2Book","coin":coin}}))
        ts, first = [], None
        end = time.time()+secs
        while time.time() < end:
            try: m = json.loads(await asyncio.wait_for(ws.recv(), timeout=end-time.time()))
            except asyncio.TimeoutError: break
            if m.get("channel") == "l2Book":
                ts.append(time.time()); first = first or m
        gaps = sorted(round(b-a,3) for a,b in zip(ts, ts[1:]))
        return {"coin": coin, "msgs": len(ts), "secs": secs,
                "median_gap_s": gaps[len(gaps)//2] if gaps else None,
                "min_gap_s": gaps[0] if gaps else None, "max_gap_s": gaps[-1] if gaps else None,
                "levels": [len(first["data"]["levels"][i]) for i in (0,1)] if first else None,
                "exch_ts_field": first["data"].get("time") if first else None}

async def main():
    out = {}
    # --- V1b: which element of users[] is the maker? cross-check against userFills.crossed
    trades = await collect_trades()
    with httpx.Client() as c:
        checks = []
        for t in trades[:3]:
            for idx, addr in enumerate(t["users"]):
                fills = post(c, {"type": "userFills", "user": addr})
                f = next((f for f in fills if f.get("tid") == t["tid"]), None)
                if f:
                    checks.append({"tid": t["tid"], "trade_side": t["side"], "users_index": idx,
                                   "fill_crossed": f.get("crossed"), "fill_side": f.get("side"),
                                   "fill_fee": f.get("fee"), "fill_keys": sorted(f.keys())})
            if len(checks) >= 4: break
        dump("userFills_crosscheck", checks)
        out["V1b_maker_taker"] = checks
        # --- V6: builder-deployed (HIP-3) perp dexs
        try:
            dexs = post(c, {"type": "perpDexs"})
            dump("perpDexs", dexs)
            out["V6_perpDexs"] = dexs
            names = [d["name"] for d in dexs if d]
            out["V6_dex_universes"] = {}
            for n in names[:8]:
                m = post(c, {"type": "meta", "dex": n})
                out["V6_dex_universes"][n] = [u["name"] for u in m["universe"]]
                dump(f"meta_dex_{n}", m)
        except Exception as e:
            out["V6_perpDexs_error"] = repr(e)
    # --- V3b: dedicated-connection cadence, liquid vs thin
    out["V3b_cadence"] = [await book_cadence(c_) for c_ in ("BTC", "SOL")]
    dump("probe_addendum", out)
    print(json.dumps(out, indent=2)[:6000])

asyncio.run(main())
