# SIBILLA — Autonomous Prediction Market Trading Desk

SIBILLA is a fully autonomous trading desk for on-chain prediction markets
(currently [Limitless Exchange](https://limitless.exchange) on Base). It hunts
mispriced probabilities on long-tail markets with an LLM committee, runs a
fair-value market-making probe on hourly crypto markets, executes with real
USDC directly against on-chain AMM pools, and settles itself: every position
is redeemed automatically at resolution. Every claimed outcome corresponds to
a verifiable on-chain transaction.

Named after the Cumaean Sibyl, who famously sold her prophecies at
ever-increasing prices.

## How it makes money

| Engine | Edge | Mechanism |
|---|---|---|
| **AI long-tail** | informational | scan ~500 markets/90s → cheap LLM triage → two independent probability estimates → a judge; trades only when net edge ≥ 5% after fees. Quantitative priors are injected where they exist (ClubElo 1X2 for football, Tennis Abstract Elo for tennis incl. Challengers) |
| **News latency** | speed on slow books | breaking news → fuzzy match against live markets → immediate committee judgment, minutes before thin long-tail books reprice. The same channel re-checks *open* positions and exits when the updated edge dies |
| **Complete-set maker (probe)** | structural | posts YES+NO bids summing to 0.955 around a fair value derived from the underlying (digital-option pricing on live spot + realized vol); a filled pair redeems at 1.00. Single-leg fills are completed at a hard total-cost cap of 0.99 — locked profit, never a directional bet |
| **Auto-redeem** | capital velocity | a cumulative registry of touched markets is swept every tick; resolved positions are redeemed on-chain without supervision |

An LLM never chooses size or leverage. A deterministic risk engine owns the
caps: max risk per trade, max open risk, exposure and drawdown brakes, with
the bankroll re-read from the wallet's true mark-to-market at every boot.

## Architecture

```
src/
  collectors/    market scanners (Limitless universe + on-chain repricing of
                 placeholder quotes), news/RSS, macro calendar, cross-venue intel
  intelligence/  LLM committee (triage → analysts → judge), decision pipeline,
                 news-latency channel, quantitative sports priors, calibration
  execution/     on-chain AMM gateway (buy/sell vs FPMM pools, slippage-capped),
                 CLOB maker with set-completion, paper mirror, position monitor
  risk/          deterministic limits, sizing, margin stress, kill switch
  evaluation/    realized P&L, post-signal alpha, model reliability
  workers/       single asyncio runner: collectors, deciders, maker, API
  api/ dashboard/ FastAPI + a dense terminal-style live dashboard
scripts/         ops: supervisor, desk report, balance check, position exit
tests/           unit + integration (mocked venues), iron-rule invariants
```

Design rules learned the hard way (all enforced in code):

- **Never trust a single RPC.** Reads probe multiple endpoints; transaction
  broadcasts use dedicated ones; nonces take the max across nodes; a
  suspicious zero is a skip, never a delete.
- **Never price from a placeholder.** Listing payloads carry fake 50/50
  quotes; every entry is re-priced against the executable pool state, and a
  market with no pool is never judged at all.
- **The mirror lies if you let it.** Paper mirrors of failed live orders are
  closed automatically; phantom drawdowns from cash→inventory conversion are
  neutralized by marking the bankroll to total wealth.
- **Exit is a feature.** Any AMM position can be liquidated against its pool
  in one call when its thesis dies.

## Running it

```bash
make install                 # Python 3.12 venv + deps
cp .env.example .env         # fill in your keys (never committed)
make initdb
make test
bash scripts/supervise.sh    # supervisor: auto-restart, log rotation,
                             # bankroll read on-chain at boot
```

The desk is designed to live unattended on a small VM under systemd, with a
daily restart timer to re-sync the bankroll, and exposes a read-only terminal
dashboard on `localhost:8000`.

## Honest disclaimers

This is real-money experimental software operating in adversarial,
thinly-traded markets. Nothing here is financial advice; expected value is a
hypothesis until proven by resolved trades; drawdowns are structural, not
accidental. Strategies documented as -EV for small retail capital (cross-venue
latency racing, AMM liquidity provision, rebate farming) were tested or
researched and deliberately left out.

## License

MIT
