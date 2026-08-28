#!/bin/bash
# Supervisor: tiene vivo il runner eToro. Venue: eToro (azioni/penny stock).
cd "$(dirname "$0")/.." || exit 1
export ATS_EXECUTION_MODE=${ATS_EXECUTION_MODE:-DEMO}

# RiskLimits di default sono calibrati per il micro-bankroll prediction-market
# (max_stake_abs=5.0 assoluto): per eToro il rischio e' derivato dall'equity
# reale del conto (2% per trade, leva 5x cap ESMA retail — decisione utente
# 28/8, vedi risk/etoro_adapter.py RISK_FRACTION_OF_EQUITY/LEVERAGE), quindi
# il cap assoluto va alzato per non strozzare ogni trade a 5 USD indipendentemente
# dall'equity.
export ATS_MAX_RISK_PER_TRADE=0.02
export ATS_MAX_STAKE_ABS=10000       # cap assoluto alto: il vincolo reale e' il %/trade
export ATS_MAX_OPEN_RISK=0.07        # 3 posizioni x 2% ciascuna (6%) + margine
export ATS_MAX_EVENT_RISK=0.02       # = risk/trade: max 1 posizione/simbolo (default), 1 evento = 1 trade
export ATS_MAX_CORRELATED_EXPOSURE=0.07
# max_open_positions (default 10), max_positions_per_asset (default 1, gia'
# coerente col design) e max_trades_per_day (default 20) NON sono overridabili
# da env flat (assenti da _FLAT_MAP in core/config.py): i default sono comunque
# piu' permissivi del target di design (max 3 posizioni, 1/simbolo), quindi non
# servono override — restano un ceiling di sicurezza, non il vincolo operativo.
export ATS_MAX_DAILY_LOSS=0.05
export ATS_MAX_WEEKLY_DRAWDOWN=0.10
# default RiskLimits.max_holding_time_s = 4h (calibrato sul vecchio motore
# Limitless): questo motore tiene una posizione fino al time-stop EOD (15:40
# NY), fino a ~6h30 dall'apertura mercato. Scoperto in TDD (Task 9/10): senza
# questo override ogni trade verrebbe rifiutato dal check horizon_within_max.
export ATS_MAX_HOLDING_TIME_S=28800  # 8h, margine sopra le ~6h30 reali
# effective_leverage qui e' il rapporto nozionale-totale/equity del portafoglio
# (NON il moltiplicatore di leva CFD per-posizione, che e' fisso a 5 in
# risk/etoro_adapter.py::LEVERAGE). Con 3 posizioni a rischio 2%/stop 7% =
# ~28.6% nozionale ciascuna, nozionale totale atteso ~85.7% equity: 1.5 da
# margine di sicurezza.
export ATS_MAX_EFFECTIVE_LEVERAGE=1.5
export ATS_MAX_ASSET_EXPOSURE=1.0
export ATS_MAX_ASSET_CLASS_EXPOSURE=1.0
# margin_factor=20 (leva 5x, risk/etoro_adapter.py): margine per posizione =
# nozionale/5 ≈ 5.7% equity ciascuna, ~17% su 3 posizioni — 0.80 e' ampio
# margine di sicurezza, non il vincolo operativo stretto.
export ATS_MAX_MARGIN_USAGE=0.80
export ATS_MIN_FREE_MARGIN=0.20
# RiskLimits._coherent impone max_margin_usage + min_free_margin <= 1.0 (src/core/config.py):
# 0.80 + 0.20 = 1.00, esattamente al limite. NON alzare MAX_MARGIN_USAGE senza abbassare
# MIN_FREE_MARGIN della stessa quantita', o Settings() solleva ValueError all'avvio.
export PYTHONPATH=src

set -a; source data/etoro_secrets.env 2>/dev/null; set +a
mkdir -p data

while true; do
  if [ -f data/etoro_runner.log ] && [ "$(stat -f%z data/etoro_runner.log 2>/dev/null || stat -c%s data/etoro_runner.log 2>/dev/null || echo 0)" -gt 52428800 ]; then
    mv data/etoro_runner.log "data/etoro_runner.log.1" && : > data/etoro_runner.log
    echo "$(date -u +%FT%TZ) supervisor: logrotate" >> data/etoro_supervisor.log
  fi
  if ! pgrep -f "python -u -m workers.etoro_runner" >/dev/null 2>&1; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) supervisor: avvio runner (etoro)" >> data/etoro_supervisor.log
    nohup .venv/bin/python -u -m workers.etoro_runner >> data/etoro_runner.log 2>&1 &
  fi
  sleep 30
done
