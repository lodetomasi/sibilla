"""Dashboard read-only del motore eToro.

Il runner eToro non persiste su DB (emette solo eventi bus + log strutturato,
diversamente dal vecchio ExecutionEngine Limitless): qui i dati sono letti
LIVE dal broker (posizioni/saldo via EtoroGateway) e dalla coda del log
strutturato (feed attivita'), nessuno stato nuovo da mantenere.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from core.config import PROJECT_ROOT, get_settings
from execution.etoro.client import EtoroClient
from execution.etoro.gateway import EtoroGateway

app = FastAPI(title="SIBILLA - eToro desk")
_state: dict[str, Any] = {}

_FEED_LABELS = {
    "etoro.runner.started": "AVVIATO",
    "etoro.runner.stopped": "FERMATO",
    "etoro.runner.no_momentum_candidates": "SCAN: nessun candidato",
    "etoro.runner.no_catalyst": "SCARTATO (nessun catalizzatore)",
    "etoro.runner.risk_rejected": "RIFIUTATO (risk engine)",
    "etoro.runner.position_cap_reached": "CAP posizioni raggiunto",
    "etoro.runner.cycle_failed": "ERRORE CICLO",
    "etoro.universe.refreshed": "UNIVERSO aggiornato",
}

_LINE_RE = re.compile(r"^(\S+?)Z?\s+\[(\w+)\s*\]\s+(\S+)\s*(.*)$")
_KV_RE = re.compile(r"(\w+)=('[^']*'|\S+)")


def parse_feed_line(line: str) -> dict[str, Any] | None:
    m = _LINE_RE.match(line.strip())
    if not m:
        return None
    ts, level, event, rest = m.groups()
    label = _FEED_LABELS.get(event)
    if label is None:
        return None
    detail = {k: v.strip("'") for k, v in _KV_RE.findall(rest)}
    return {"ts": ts[:19], "level": level, "event": event, "label": label, "detail": detail}


def _gateway() -> EtoroGateway:
    gw = _state.get("gateway")
    if gw is None:
        client = EtoroClient(settings=get_settings())
        gw = EtoroGateway(client=client)
        _state["gateway"] = gw
    return gw


@app.get("/api/etoro/status")
async def status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "mode": settings.execution_mode.value,
        "max_penny_price_usd": settings.etoro.max_penny_price_usd,
        "etoro_configured": settings.etoro.configured,
    }


@app.get("/api/etoro/balance")
async def balance() -> dict[str, Any]:
    account = await _gateway().balances()
    return account.model_dump(mode="json")


@app.get("/api/etoro/positions")
async def positions() -> list[dict[str, Any]]:
    rows = await _gateway().positions()
    return [r.model_dump(mode="json") for r in rows]


@app.get("/api/etoro/feed")
async def feed(limit: int = 60) -> list[dict[str, Any]]:
    path = PROJECT_ROOT / "data" / "etoro_runner.log"
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(max(0, path.stat().st_size - 300_000))
        lines = fh.read().decode(errors="ignore").splitlines()
    rows = [r for r in (parse_feed_line(ln) for ln in lines) if r]
    return rows[-limit:][::-1]


@app.get("/", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    html_path = PROJECT_ROOT / "src" / "dashboard" / "templates" / "etoro_dashboard.html"
    return HTMLResponse(html_path.read_text())
