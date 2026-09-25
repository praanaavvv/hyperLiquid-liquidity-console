"""Research report: replays data/crude JSONL into web/report.js (window.REPORT = {...}).

Everything here needs the captured history - none of it comes off a live socket:
markouts (where the mid went after each trade), per-address maker/taker flow,
concentration, impact by size, intraday liquidity profile, collector coverage.

Run: uv run --python 3.11 analytics/report.py
"""
import bisect
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRUDE = ROOT / "data" / "crude"
OUT = ROOT / "web" / "report.js"
HORIZONS_S = (1, 5, 15, 60, 300)
BANDS_BPS = (1, 2, 5)  # 20-level l2Book rarely reaches past ~5bps on liquid books
SIZE_EDGES = (0, 1e3, 1e4, 1e5, 1e6)  # lower edges, $ per taker order
GAP_MS = 60_000  # bbo silent longer than this = collector outage, not a quiet book
TOP_N = 10
TOPN_RANKS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 5000)


def r(x, n=4):
    return None if x is None else float(f"{x:.{n}g}")


def messages(coin_dir: Path):
    files = sorted(coin_dir.glob("date=*/hour=*.jsonl*"), key=lambda p: (p.parent.name, p.name))
    for f in files:
        with open(f) as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:  # torn last line from a killed process
                    continue


class WMean:
    """Weighted means per horizon."""

    def __init__(self, k=len(HORIZONS_S)):
        self.s, self.w = [0.0] * k, [0.0] * k

    def add(self, i, x, w):
        self.s[i] += x * w
        self.w[i] += w

    def out(self):
        return [r(s / w) if w else None for s, w in zip(self.s, self.w)]


def concentration(vol: dict) -> dict:
    tot = sum(vol.values()) or 1
    ranked = sorted(vol.items(), key=lambda kv: -kv[1])
    shares = [v / tot for _, v in ranked]
    cum, acc = [], 0.0
    for s in shares:
        acc += s
        cum.append(acc)
    n = len(shares)
    return {
        "n": n,
        "hhi": r(sum((s * 100) ** 2 for s in shares)),
        "top1": r(sum(shares[:1])), "top5": r(sum(shares[:5])), "top10": r(sum(shares[:10])),
        "topn": [[k, r(cum[min(k, n) - 1])] for k in TOPN_RANKS] if n else [],
        "ranked": ranked,
    }


def analyze(msgs) -> dict:
    bbo, books, ctx, fills = [], [], [], {}
    counts, fill_rows = Counter(), 0
    for m in msgs:
        ch, d = m["channel"], m["data"]
        counts[ch] += 1
        if ch == "bbo":
            b, a = d["bbo"]
            if b and a:
                bp, ap = float(b["px"]), float(a["px"])
                mp = (bp + ap) / 2
                bbo.append((d["time"], mp, (ap - bp) / mp * 1e4))
        elif ch == "l2Book":
            bids, asks = d["levels"]
            if not bids or not asks:
                continue
            mp = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
            dep, tr = [], []
            for bps in BANDS_BPS:
                s, trunc = 0.0, False
                for side in (bids, asks):
                    for lv in side:
                        p = float(lv["px"])
                        if abs(p - mp) / mp * 1e4 <= bps:
                            s += p * float(lv["sz"])
                    # 20-level cap: book ends inside the band -> depth is a lower bound
                    trunc |= abs(float(side[-1]["px"]) - mp) / mp * 1e4 < bps
                dep.append(s)
                tr.append(trunc)
            reach = min(abs(float(side[-1]["px"]) - mp) / mp * 1e4 for side in (bids, asks))
            books.append((d["time"], dep, tr, reach))
        elif ch == "trades":
            for t in d:
                fill_rows += 1
                buyer, seller = t.get("users") or (None, None)
                buy = t["side"] == "B"  # side = aggressor; users = [buyer, seller] (API_FINDINGS V1)
                fills[t["tid"]] = (t["time"], float(t["px"]), float(t["sz"]), 1 if buy else -1,
                                   buyer if buy else seller, seller if buy else buyer)
        elif ch == "activeAssetCtx":
            c = d["ctx"]
            ctx.append((m["ts_local_ns"] // 1_000_000, float(c["funding"]), float(c["premium"])))

    if len(bbo) < 2:
        return None
    bbo.sort()
    bt = [x[0] for x in bbo]
    gaps = [(bt[i], bt[i + 1]) for i in range(len(bt) - 1) if bt[i + 1] - bt[i] > GAP_MS]
    gap_starts = [g[0] for g in gaps]

    def outage(a, b):  # any collector gap overlapping [a, b]
        i = bisect.bisect_left(gap_starts, b) - 1
        return i >= 0 and gaps[i][1] > a

    def mid_at(t):
        i = bisect.bisect_right(bt, t) - 1
        return bbo[i][1] if i >= 0 else None

    # --- time-weighted spread, overall and by UTC hour ---
    hw, hs = [0.0] * 24, [0.0] * 24
    for (t, _, sp), t2 in zip(bbo, bt[1:]):
        dt = t2 - t
        if dt <= GAP_MS:
            h = t // 3_600_000 % 24
            hw[h] += dt
            hs[h] += dt * sp
    covered = sum(hw)

    # --- taker orders: fills sharing (taker, ms, side) are one order sweeping levels ---
    orders = defaultdict(lambda: [0.0, 0.0])
    for t, px, sz, s, tk, _ in fills.values():
        o = orders[(tk, t, s)]
        o[0] += px * sz
        o[1] += sz

    taker_curve, buckets = WMean(), [[0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in SIZE_EDGES]
    taker_vol, taker_m60 = defaultdict(float), defaultdict(lambda: [0.0, 0.0])
    hour_vol = [0.0] * 24
    h60 = HORIZONS_S.index(60)
    for (tk, t, s), (ntl, sz) in orders.items():
        vwap = ntl / sz
        taker_vol[tk] += ntl
        hour_vol[t // 3_600_000 % 24] += ntl
        b = buckets[bisect.bisect_right(SIZE_EDGES, ntl) - 1]
        b[0] += 1
        b[1] += ntl
        pre = mid_at(t - 1)
        if pre and not outage(t - 1, t):
            b[2] += ntl * s * (vwap - pre) / pre * 1e4
            b[3] += ntl
        for i, h in enumerate(HORIZONS_S):
            if t < bt[0] or t + h * 1000 > bt[-1] or outage(t, t + h * 1000):
                continue
            mk = s * (mid_at(t + h * 1000) - vwap) / vwap * 1e4
            taker_curve.add(i, mk, ntl)
            if i == h60:
                b[4] += ntl * mk
                b[5] += ntl
                taker_m60[tk][0] += ntl * mk
                taker_m60[tk][1] += ntl

    # --- makers: P&L of each resting fill as the mid moves afterwards ---
    maker_vol, maker_fills = defaultdict(float), Counter()
    maker_pnl = defaultdict(WMean)
    for t, px, sz, s, _, mk in fills.values():
        maker_vol[mk] += px * sz
        maker_fills[mk] += 1
        for i, h in enumerate(HORIZONS_S):
            if bt[0] <= t and t + h * 1000 <= bt[-1] and not outage(t, t + h * 1000):
                maker_pnl[mk].add(i, -s * (mid_at(t + h * 1000) - px) / px * 1e4, px * sz)
    mc, tc = concentration(maker_vol), concentration(taker_vol)
    top_makers = {a for a, _ in mc["ranked"][:TOP_N]}
    cohort = {"top": WMean(), "rest": WMean()}
    for a, w in maker_pnl.items():
        c = cohort["top" if a in top_makers else "rest"]
        for i in range(len(HORIZONS_S)):
            c.s[i] += w.s[i]
            c.w[i] += w.w[i]
    mtot, ttot = sum(maker_vol.values()) or 1, sum(taker_vol.values()) or 1
    i5 = HORIZONS_S.index(5)

    # --- depth ---
    books.sort()
    depth_by_hour = defaultdict(list)
    for t, dep, *_ in books:
        depth_by_hour[t // 3_600_000 % 24].append(dep[1])
    l2_gaps = [b - a for a, b in zip([x[0] for x in books], [x[0] for x in books[1:]]) if b - a <= GAP_MS]

    # --- funding / premium, hourly ---
    fh = defaultdict(list)
    for t, f, p in ctx:
        fh[t // 3_600_000].append((f, p))
    funding = [[h * 3_600_000, r(statistics.fmean(f for f, _ in v) * 24 * 365 * 100),
                r(statistics.fmean(p for _, p in v) * 1e4)] for h, v in sorted(fh.items())]

    return {
        "summary": {
            "start": bt[0], "end": bt[-1], "span_h": r((bt[-1] - bt[0]) / 3.6e6),
            "coverage": r(covered / (bt[-1] - bt[0])),
            "tw_spread_bps": r(sum(hs) / covered) if covered else None,
            "fills": len(fills), "dup_fills": fill_rows - len(fills), "orders": len(orders),
            "notional": r(mtot), "gaps": len(gaps),
            "longest_gap_s": r(max((b - a for a, b in gaps), default=0) / 1000),
            "l2_cadence_s": r(statistics.median(l2_gaps) / 1000) if l2_gaps else None,
            "msgs": dict(counts),
        },
        "depth": {
            "bands": list(BANDS_BPS),
            "median": [r(statistics.median(b[1][i] for b in books)) if books else None for i in range(len(BANDS_BPS))],
            "reach_bps": r(statistics.median(b[3] for b in books)) if books else None,
            "trunc_rate": [r(sum(b[2][i] for b in books) / len(books)) if books else None for i in range(len(BANDS_BPS))],
        },
        "horizons": list(HORIZONS_S),
        "markout": {"taker": taker_curve.out(), "maker_top": cohort["top"].out(), "maker_rest": cohort["rest"].out()},
        "size_buckets": [{
            "lo": lo, "orders": b[0], "share": r(b[1] / mtot),
            "impact_bps": r(b[2] / b[3]) if b[3] else None,
            "markout60_bps": r(b[4] / b[5]) if b[5] else None,
        } for lo, b in zip(SIZE_EDGES, buckets)],
        "makers": {**{k: v for k, v in mc.items() if k != "ranked"}, "top": [{
            "addr": a, "share": r(v / mtot), "fills": maker_fills[a],
            "pnl5": maker_pnl[a].out()[i5], "pnl60": maker_pnl[a].out()[h60],
        } for a, v in mc["ranked"][:TOP_N]]},
        "takers": {**{k: v for k, v in tc.items() if k != "ranked"}, "top": [{
            "addr": a, "share": r(v / ttot), "orders": sum(1 for k in orders if k[0] == a),
            "markout60": r(taker_m60[a][0] / taker_m60[a][1]) if taker_m60[a][1] else None,
        } for a, v in tc["ranked"][:TOP_N]]},
        "hourly": {
            "spread": [r(s / w) if w else None for s, w in zip(hs, hw)],
            "depth": [r(statistics.median(depth_by_hour[h])) if depth_by_hour[h] else None for h in range(24)],
            "vol_per_h": [r(v / (w / 3.6e6)) if w else None for v, w in zip(hour_vol, hw)],
        },
        "funding": funding,
    }


def ledger() -> dict:
    p = CRUDE / "uptime.jsonl"
    errs, n = Counter(), 0
    if p.exists():
        for line in open(p):
            e = json.loads(line)
            if e["event"] == "disconnected":
                n += 1
                errs[e["error"].split("(")[0]] += 1
    return {"disconnects": n, "errors": errs.most_common()}


def main():
    coins = {}
    for d in sorted(CRUDE.glob("coin=*")):
        coin = d.name.removeprefix("coin=").replace("_", ":", 1)
        t0 = time.time()
        # ponytail: full replay every run, fine for days of data; go incremental on M2 parquet.
        res = analyze(messages(d))
        if res:
            coins[coin] = res
        print(f"{coin}: {time.time() - t0:.1f}s", flush=True)
    report = {"generated_at": int(time.time() * 1000), "ledger": ledger(), "coins": coins}
    tmp = OUT.with_suffix(".js.tmp")
    tmp.write_text("window.REPORT = " + json.dumps(report, separators=(",", ":")) + ";\n")
    os.replace(tmp, OUT)  # page never sees a half-written report
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e3:.0f} KB)")


if __name__ == "__main__":
    main()
