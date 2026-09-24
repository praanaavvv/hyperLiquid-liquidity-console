# Hyperliquid API findings

Probed live on **2026-09-24** by `scripts/probe_api.py` and `scripts/probe_addendum.py`.
Raw payloads in `docs/samples/`. Where this contradicts the build spec, the API wins
(spec §15) and the consequence is stated under **Spec impact**.

---

## V1 — Do trades carry counterparty addresses? **YES**

WS `trades` message, per trade:

```json
{"coin":"BTC","side":"A","px":"84133.0","sz":"0.02279","time":1790231124037,
 "hash":"0x0000...0000","tid":140238147810435,
 "users":["0xbb160cb6...","0x48314750..."]}
```

`users` is a 2-element array. **The order is by direction, not by role** — verified by
cross-checking three trades against each counterparty's `userFills.crossed`
(`docs/samples/userFills_crosscheck.json`):

| trade `side` | `users[0]` fill side | `users[1]` fill side | who crossed |
|---|---|---|---|
| `B` | B (buy) | A (sell) | `users[0]` |
| `A` | B (buy) | A (sell) | `users[1]` |

So: **`users[0]` is the buyer, `users[1]` is the seller**, and `trades.side` is the
**aggressor's** side. Therefore

```
taker = users[0] if side == "B" else users[1]
maker = the other one
```

Confirmed independently by fees in the matched fills: the crossing side pays
(`fee: "0.340572"`), the resting side is rebated (`fee: "-0.008409"`).

The attribution layer (§8) runs at full resolution. The `userFills` fallback is not needed.

> **This inference must be policed, not trusted.** It is a positional convention that
> could change silently. Added as data-quality check #11: sample N trades/day, pull
> `userFills` for both addresses, assert the maker/taker derivation still matches
> `crossed`. If it ever fails, every attribution number downstream is wrong.

`hash` is all-zeroes for these fills — do not treat it as a usable tx id. `tid` is the key.
Resting-side fills carry `cloid`; crossing-side ones often don't.

## V2 — WS subscription limits: **≥200 `l2Book` subs on one connection**

Subscribed to all 200 attempted coins on a single socket with no error and no
disconnect. **No connection sharding needed for 14 coins.** Per-IP connection
limits were not probed (deliberately — not going to hammer a public endpoint to
find the ceiling); 1 connection is enough, so the question is moot for Phase 1.

## V3 — `l2Book` cadence: **throttled to ~5.4s. This is the biggest constraint in the project.**

Dedicated connection, BTC, 45s:

| channel | msgs | median gap | p90 gap | min gap | levels/side |
|---|---|---|---|---|---|
| `l2Book` | 9 | **5.37s** | 5.62s | 1.96s | 20 |
| `bbo` | 296 | **0.107s** | 0.322s | 0.005s | 1 (top only) |

The l2Book gap is flat at ~5.4s across BTC and SOL and across separate runs — that is a
server-side throttle, not quiet markets. REST `l2Book` is also capped at 20 levels/side
and is rate-limited, so polling does not rescue it (14 coins at a safe REST rate is worse
than 5.4s/coin).

Level shape is `{"px","sz","n"}` — the per-level order count the spec wants is present.

**Spec impact (§7):**
- Spread, microprice, top-of-book uptime and imbalance-at-touch → compute from **`bbo`** (~10 Hz). The spec's time-weighting is viable here.
- Depth bands, slippage curves, full imbalance → **`l2Book`**, ~11 snapshots/minute. Time-weighted, not mean, still matters, but resolution is coarse.
- **§7.3 resilience needs rewriting.** "Seconds until depth@10bps recovers to 90%" cannot be measured on a 5.4s grid — most recoveries will be faster than one sample. Proposal: report resilience on a top-of-book basis from `bbo` (time to restore best-level size and spread), and report depth@10bps recovery only as a coarse ≥5.4s-resolution figure with the sampling limit stated. Needs your call at the M3 boundary.
- 20 levels/side is a hard ceiling on the depth curve. For BTC, 20 levels ≈ 20 ticks ≈ 24 bps, so the 50 bps depth band will be **truncated, not zero**, on tight books. Must be recorded as truncated rather than reported as a depth number.

## V4 — Historical archive: **not verified, deferred**

Not probed this pass. Public references point at a requester-pays S3 bucket
(`hyperliquid-archive`), which needs AWS credentials and costs money to list, so it is a
decision rather than a probe. Not on the M1 critical path: the crude collector is already
capturing, which was the actual risk V4 was meant to mitigate. **Open question for you:
do you want me to test S3 access (and pay the request/egress) for backfill?**

## V5 — REST rate limits: **not reached at 5.3 req/s**

60 sequential `l2Book` POSTs in 11.35s (5.3 rps, latency-bound): all 200, no 429, no
rate-limit headers returned. The documented per-IP budget is weight-based per minute;
we did not probe the ceiling deliberately. Poller cadence in `config/settings.yaml`
(metaAndAssetCtxs 60s, meta 3600s) is far below anything observed to be a problem.

## V6 — HIP-3 builder markets: **`dex:SYMBOL`, and they are NOT in the base `meta`**

`{"type":"meta"}` returns 234 entries (178 with non-zero 24h volume) and **zero**
colon-prefixed symbols. Builder markets live behind:

- `{"type":"perpDexs"}` → 10 dexes: `xyz, flx, vntl, hyna, km, abcd, cash, para, mkts, io`
  (plus a leading `null` for the native dex — handle that).
- `{"type":"meta","dex":"xyz"}` / `{"type":"metaAndAssetCtxs","dex":"xyz"}` → that dex's universe.

291 builder markets total, **only 147 with non-zero volume, and activity is extremely
concentrated**:

| dex | top market | 24h vol |
|---|---|---|
| xyz | `xyz:SP500` | $247.4M |
| io | `io:SNDK` | $15.6M |
| mkts | `mkts:US500` | $9.5M |
| para | `para:10Y` | $1.1M |
| flx, vntl, hyna, km, abcd, cash | — | **$0** |

Six of ten deployed dexes have zero 24h volume. That is already a finding for §14.

WS subscriptions take the full prefixed name directly (`{"type":"l2Book","coin":"xyz:SP500"}`)
with **no** `dex` field — verified, the crude collector is capturing `xyz:SP500` now.
Only the REST `meta`/`metaAndAssetCtxs` calls need `dex`.

## V7 — `activeAssetCtx`: **yes, everything the funding/basis panel needs, at 1 Hz**

30 messages in 30s (~1/s). Keys, identical over WS and REST `metaAndAssetCtxs`:

```
dayBaseVlm, dayNtlVlm, funding, impactPxs, markPx, midPx,
openInterest, oraclePx, premium, prevDayPx
```

Covers the spec's `asset_ctx` schema exactly (`mark_px`, `oracle_px`, `funding_rate`,
`open_interest`, `premium`) with `impactPxs` as a bonus — impact prices are what funding
is actually computed from, so keep them; they make §7.4 checkable rather than assumed.

---

## Assumptions made, flagged per §15

1. **Maker/taker derivation from `users[]` order** is inferred from 3 trades (6 fills), not documented. Policed by check #11 above.
2. **Coin universe frozen on one snapshot** (`docs/samples/volume_ranking.json`, 2026-09-24). Rankings move; the list does not, by design.
3. **T4 selection favours cross-deployer comparison over pure volume ranking**: `xyz:SP500` and `mkts:US500` track the same underlying index on different deployers, which is a natural experiment for the §14 finding. `io:SNDK` is the third-largest dex's leader.
4. **Per-IP WS connection limit unprobed** — one connection suffices, so no ceiling test was run against a public endpoint.
