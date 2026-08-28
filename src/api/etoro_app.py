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


def _parse_structured_line(line: str) -> tuple[str, str, dict[str, str]] | None:
    m = _LINE_RE.match(line.strip())
    if not m:
        return None
    ts, _level, event, rest = m.groups()
    detail = {k: v.strip("'") for k, v in _KV_RE.findall(rest)}
    return ts[:19], event, detail


def parse_feed_line(line: str) -> dict[str, Any] | None:
    parsed = _parse_structured_line(line)
    if parsed is None:
        return None
    ts, event, detail = parsed
    label = _FEED_LABELS.get(event)
    if label is None:
        return None
    return {"ts": ts, "level": "info", "event": event, "label": label, "detail": detail}


def parse_calculation_line(line: str) -> dict[str, Any] | None:
    """Un calcolo dello screener momentum (etoro.momentum.evaluated): ogni
    strumento con storico sufficiente, qualificato o no - non solo l'aggregato."""
    parsed = _parse_structured_line(line)
    if parsed is None:
        return None
    ts, event, detail = parsed
    if event != "etoro.momentum.evaluated":
        return None
    return {
        "ts": ts,
        "instrument_id": detail.get("instrument_id"),
        "name": detail.get("name"),
        "gap_pct": float(detail["gap_pct"]) if "gap_pct" in detail else None,
        "relative_volume": float(detail["relative_volume"]) if "relative_volume" in detail else None,
        "qualifies": detail.get("qualifies") == "True",
    }


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


def _tail_lines(max_bytes: int) -> list[str]:
    path = PROJECT_ROOT / "data" / "etoro_runner.log"
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(max(0, path.stat().st_size - max_bytes))
        return fh.read().decode(errors="ignore").splitlines()


@app.get("/api/etoro/feed")
async def feed(limit: int = 60) -> list[dict[str, Any]]:
    lines = _tail_lines(300_000)
    rows = [r for r in (parse_feed_line(ln) for ln in lines) if r]
    return rows[-limit:][::-1]


@app.get("/api/etoro/calculations")
async def calculations(limit: int = 250) -> list[dict[str, Any]]:
    """Ogni calcolo dell'ultimo scan (o degli ultimi scan), incluse le esclusioni:
    finestra piu' ampia del feed principale perche' un solo ciclo puo' valutare
    fino a 200 strumenti (vedi collectors/etoro/instruments.py TARGET_UNIVERSE_SIZE)."""
    lines = _tail_lines(1_500_000)
    rows = [r for r in (parse_calculation_line(ln) for ln in lines) if r]
    return rows[-limit:][::-1]


@app.get("/", response_class=HTMLResponse)
async def dashboard_page() -> HTMLResponse:
    html_path = PROJECT_ROOT / "src" / "dashboard" / "templates" / "etoro_dashboard.html"
    return HTMLResponse(html_path.read_text())
