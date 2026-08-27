#!/bin/bash
# Vista live del desk Limitless: giudizi comitato, decisioni risk, ordini, errori.
cd /Users/detomasi/automatic-trading-bot || exit 1
echo "=== ATS LIVE — ctrl+C per uscire ==="
tail -n 40 -F data/runner.log | grep --line-buffered -E "limitless\.(scan|judged|executed|risk_rejected|triage_skip|judge_failed|live)|risk\.decision|kind=ORDER_|kill_switch|runner.started" | sed -u \
  -e 's/.*limitless.executed.*/\x1b[42;30m&\x1b[0m/' \
  -e 's/.*live\.filled.*/\x1b[42;30m&\x1b[0m/' \
  -e 's/.*risk.decision.*approved=True.*/\x1b[32m&\x1b[0m/' \
  -e 's/.*risk_rejected.*/\x1b[31m&\x1b[0m/' \
  -e 's/.*judge_failed.*/\x1b[31m&\x1b[0m/' \
  -e 's/.*live\.failed.*/\x1b[41;97m&\x1b[0m/' \
  -e 's/.*limitless.judged.*/\x1b[36m&\x1b[0m/' \
  -e 's/.*triage_skip.*/\x1b[90m&\x1b[0m/'
