"""Run: uv run --python 3.11 analytics/test_report.py"""
from report import analyze


def bbo(t, bid, ask):
    return {"channel": "bbo", "data": {"time": t, "bbo": [{"px": str(bid)}, {"px": str(ask)}]}}


def trade(t, tid, side, px, users):
    return {"channel": "trades", "data": [{"time": t, "tid": tid, "side": side, "px": str(px), "sz": "1", "users": users}]}


msgs = [
    bbo(0, 99, 101),                           # mid 100
    trade(1000, 1, "B", 101, ["TAKER", "MAKER"]),  # taker buys at the ask
    trade(1000, 1, "B", 101, ["TAKER", "MAKER"]),  # reconnect replay: same tid, must dedupe
    bbo(2000, 101, 103),                       # mid 102 from here on
    bbo(400_000, 101, 103),
    bbo(500_000, 101, 103),                    # 100s silence before this = outage
    trade(450_000, 2, "A", 101, ["X", "Y"]),   # inside the outage: no markouts
]
r = analyze(msgs)
s = r["summary"]
assert s["fills"] == 2 and s["dup_fills"] == 1 and s["gaps"] == 2, s
tk = r["markout"]["taker"]
assert abs(tk[0] - 99.01) < 0.01, tk          # (102-101)/101 bps, only trade 1 counts
assert r["markout"]["maker_top"][0] == -tk[0]  # maker lost what the taker gained
b = r["size_buckets"][0]
assert b["orders"] == 2 and abs(b["impact_bps"] - 100) < 1e-9, b  # (101-100)/100; trade 2 excluded
assert r["makers"]["top"][0]["addr"] == "MAKER" and r["takers"]["n"] == 2
print("ok")
