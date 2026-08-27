#!/bin/bash
# Supervisor: tiene vivo il runner ATS. Venue: LIMITLESS (paper su prezzi reali; IG spento).
cd "$(dirname "$0")/.." || exit 1  # path-agnostico: vale su Mac e su VM
export ATS_EXECUTION_MODE=PAPER
export ATS_IG_ENABLED=0
export ATS_LIMITLESS_ENABLED=1
export ATS_LIMITLESS_LIVE=1
export ATS_LIMITLESS_ONCHAIN=1
export ATS_LIMITLESS_MAKER=1
export ATS_LIMITLESS_LIVE_MAX_USDC=25  # capacita' dei pool sottili, non prudenza: oltre, lo slippage mangia l'edge
# bankroll = PATRIMONIO reale mark-to-market (cash + inventario + posizioni AMM):
# il solo cash sotto-stima il capitale quando e' investito e strozza i cap di rischio
REAL_USDC=$(PYTHONPATH=src .venv/bin/python scripts/report_desk.py --total 2>/dev/null | sed -n 's/.*total=\([0-9.]*\).*/\1/p')
if [ -z "$REAL_USDC" ]; then  # fallback: solo cash on-chain
  REAL_USDC=$(PYTHONPATH=src .venv/bin/python scripts/check_balance.py 0x9BF9F4eD7C0538531432980643E3456fB7A93D13 2>/dev/null | sed -n 's/.*usdc=\([0-9.]*\).*/\1/p')
fi
export ATS_BANKROLL=${REAL_USDC:-10}
echo "$(date -u +%FT%TZ) supervisor: bankroll reale = ${ATS_BANKROLL} USDC" >> data/supervisor.log
# limiti ricalibrati per micro-capitale (assoluti comunque piccoli; cap live 2 USDC/ordine)
export ATS_MAX_RISK_PER_TRADE=0.25
export ATS_MAX_EVENT_RISK=0.25
export ATS_MAX_CORRELATED_EXPOSURE=0.25
export ATS_MAX_MARGIN_USAGE=0.60
export ATS_MIN_FREE_MARGIN=0.30
export ATS_STRESS_MIN_FREE_MARGIN=0.15
export ATS_MAX_OPEN_RISK=0.25
export ATS_MAX_DAILY_LOSS=0.25
export ATS_MAX_WEEKLY_DRAWDOWN=0.25
export ATS_MAX_DATA_STALENESS_S=180
export ATS_MAX_PUBLIC_DATA_STALENESS_S=300  # mercati a giorni: 5 min di quote-age non cambia l'edge  # quote scan 90s: tolleranza onesta per prediction market
export ATS_MAX_HOLDING_TIME_S=1209600  # 14 giorni: orizzonte prediction market
export PYTHONPATH=src
set -a; source data/limitless_secrets.env 2>/dev/null; set +a
mkdir -p data
while true; do
  # logrotate semplice: oltre 50MB il log ruota (tiene 1 archivio)
  if [ -f data/runner.log ] && [ "$(stat -f%z data/runner.log 2>/dev/null || echo 0)" -gt 52428800 ]; then
    mv data/runner.log "data/runner.log.1" && : > data/runner.log
    echo "$(date -u +%FT%TZ) supervisor: logrotate" >> data/supervisor.log
  fi
  if ! pgrep -f "python -u -m workers.runner" >/dev/null 2>&1; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) supervisor: avvio runner (limitless)" >> data/supervisor.log
    nohup .venv/bin/python -u -m workers.runner >> data/runner.log 2>&1 &
  fi
  sleep 30
done
