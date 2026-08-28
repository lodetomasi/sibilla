# SIBILLA — Autonomous Equities Trading Desk

SIBILLA is an autonomous trading desk for penny/small-cap equities on
[eToro](https://www.etoro.com), via eToro's Public API. It scans a stock
universe for mechanical momentum anomalies (price gap + relative volume
spike), asks an LLM to gate the move against real, dated news before ever
risking capital, sizes the trade with a deterministic risk engine, and
executes as a leveraged CFD market order. A read-only live dashboard exposes
every step: the raw per-instrument calculations, the LLM verdicts, open
positions, and account equity.

Named after the Cumaean Sibyl, who famously sold her prophecies at
ever-increasing prices.

## How it makes money

| Stage | What it does |
|---|---|
| **Universe** | pages through eToro's stock search (results are ordered by instrument ID, not relevance, so most of a single page is delisted/non-tradable — the collector paginates until it has a real pool of tradable names) |
| **Momentum screener** | pure arithmetic, no LLM: daily-candle gap % and volume vs. a 20-session average, per instrument |
| **Catalyst judge (LLM)** | the anti pump-and-dump gate — given a momentum candidate and recent dated news about it, decides whether a *verifiable* catalyst (earnings, M&A, regulatory, contract) explains the move, or whether it's unexplained/speculative. It never sees or sets size, stop, or leverage |
| **Risk engine** | deterministic: fixed % of live account equity per trade, hard stop/target distances, portfolio-level exposure and drawdown caps |
| **Execution** | CFD market order against eToro, fixed leverage, explicit stop-loss/take-profit rates attached at open; a hard time-stop closes everything before the US close |

News for the catalyst judge comes from an RSS pipeline (macro/market wires
plus a company press-release wire) polled independently of the trading loop,
so the judge is never blind by design — though it will (correctly) refuse a
trade when no matching news exists yet.

## Architecture

```
src/
  collectors/etoro/   paginated stock universe, live rates, daily candles
  collectors/news/    RSS ingestion (macro wires + company press releases), dedup
  strategies/         momentum screener (gap % + relative volume, pure functions)
  intelligence/       the catalyst judge (single LLM call, schema-constrained)
  execution/etoro/    thin HTTP client + gateway (open/close orders, positions, balance)
  risk/               deterministic sizing/limits engine + eToro-specific adapter
  workers/            the single asyncio runner: scan -> screen -> judge -> risk -> execute
  api/ dashboard/     FastAPI read-only dashboard (live broker data + log-tail feed)
scripts/              supervisor + dashboard launcher (systemd-managed on the VM)
tests/                unit tests, mocked broker/LLM/DB
```

Design rules learned the hard way (all enforced in code, discovered against
the real API — not from documentation alone):

- **eToro's own field casing is inconsistent across endpoints.** Rates use
  `instrumentID`, close-position uses `InstrumentID`, most other endpoints use
  `instrumentId`. Every response is parsed defensively; nothing assumes a name
  that hasn't been checked against a live call.
- **A successful order response does not confirm a fill.** `create-an-order`
  only returns an order id — execution is discovered on the next positions
  poll, never inferred from the submit response.
- **Stop-loss and take-profit are `*Rate` fields**, not bare `stopLoss`/
  `takeProfit` — the wrong name is silently dropped by the API, which opens an
  unprotected position instead of erroring.
- **Daily candles don't move intraday.** Without a per-instrument judge
  cooldown, the same static gap gets re-sent to the LLM every scan cycle for
  hours, at real cost, for an answer that cannot have changed.
- **The LLM never decides size or leverage.** Those are pure functions of
  account equity and fixed risk parameters — a bad LLM day never turns into a
  bad position size.

## Running it

```bash
make install                       # Python 3.12 venv + deps
cp env.example .env                # fill in your keys (never committed)
make initdb
make test                          # 126 tests, mocked broker/LLM/DB

PYTHONPATH=src .venv/bin/python -m workers.etoro_runner     # trading loop
PYTHONPATH=src .venv/bin/uvicorn api.etoro_app:app --port 8000   # dashboard
```

In production both run as systemd services on a small always-on VM
(`deploy/etoro.service`, `deploy/etoro-dashboard.service`), supervised for
auto-restart. The dashboard binds to `127.0.0.1:8000` only — reach it through
an SSH tunnel (`ssh -L 8000:127.0.0.1:8000 <host>`), never exposed publicly.

## Honest disclaimers

This is experimental software placing real orders (currently against eToro's
demo/practice environment) in adversarial markets. Nothing here is financial
advice; expected value is a hypothesis until proven by resolved trades, not a
promise. Every claimed outcome is meant to be verifiable against eToro's own
account history — no number here should be trusted on its own.

## License

MIT
