# Hyperliquid Liquidity Operations Console

Phase 1 (backend). Read-only, public endpoints only — no trading, no signed requests.

## Status: M1 complete

- `scripts/probe_api.py`, `scripts/probe_addendum.py` — answer V1–V7 against the live API
- `docs/API_FINDINGS.md` — findings, spec impacts, flagged assumptions
- `docs/samples/` — raw payloads, the schema reference for parsers and golden tests
- `config/coins.yaml` — 14 coins, frozen 2026-09-24
- `collector/crude.py` — crude 2-coin raw capture, running now (book depth can't be backfilled)

M2 (production collector) is next and not started.

## Run

```sh
uv run --python 3.11 scripts/probe_api.py          # re-probe
uv run --python 3.11 collector/crude.py BTC xyz:SP500
```

Capture lands in `data/crude/coin=<coin>/date=<d>/hour=<HH>.jsonl`, with a
connect/disconnect ledger in `data/crude/uptime.jsonl`.
