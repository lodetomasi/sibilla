"""API FastAPI (sez. 45, 48, 49, 71): dashboard data + controlli umani + health."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.audit import audit, cost_summary, profit_after_information_cost
from core.clock import utcnow
from core.config import PROJECT_ROOT, get_settings
from core.db import create_all, get_db
from core.enums import ExitReason, SystemState
from core.repository import Repository
from evaluation.alpha import post_signal_alpha_report
from evaluation.attribution import ablation_report, attribution_report
from evaluation.metrics import failure_detection, performance_by_domain
from evaluation.pnl import execution_quality, realized_performance
from risk.kill_switch import get_kill_switch
from risk.limits import current_limits, update_limits_human

app = FastAPI(title="ATS - AI Market Intelligence & Autonomous Trading", version="0.1.0")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "src" / "dashboard" / "templates"))
_state: dict[str, Any] = {"engine": None, "runner": None}


def attach(engine: Any = None, runner: Any = None) -> None:
    _state["engine"] = engine
    _state["runner"] = runner


@app.on_event("startup")
async def _startup() -> None:
    await create_all()


def _mode() -> str:
    return get_settings().execution_mode.value


# ----------------------------------------------------------------- health
@app.get("/health")
async def health() -> dict[str, Any]:
    runner = _state.get("runner")
    return {"status": "ok", "ts": utcnow().isoformat(), "mode": _mode(), "kill_switch": get_kill_switch().snapshot(), "runner": runner.snapshot() if runner else None}


# --------------------------------------------------------------- overview
@app.get("/api/overview")
async def overview(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    repo = Repository(db)
    mode = _mode()
    snapshot = await repo.latest_portfolio(mode)
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    perf = await realized_performance(mode=mode)
    open_positions = await repo.open_positions(mode)
    return {
        "mode": mode, "autonomy_level": get_settings().autonomy_level.value,
        "bankroll": float(snapshot.balance) if snapshot else current_limits().bankroll, "equity": float(snapshot.equity) if snapshot else current_limits().bankroll,
        "todays_pnl": await repo.realized_pnl_since(today, mode), "total_pnl": perf.get("pnl", 0.0),
        "open_exposure": float(snapshot.open_notional) if snapshot else 0.0, "open_risk": float(snapshot.open_risk) if snapshot else 0.0,
        "open_positions": len(open_positions), "daily_drawdown": float(snapshot.daily_drawdown) if snapshot else 0.0, "weekly_drawdown": float(snapshot.weekly_drawdown) if snapshot else 0.0,
        "win_rate": perf.get("win_rate"), "expectancy": perf.get("expectancy"), "sharpe": perf.get("sharpe"), "n_trades": perf.get("n", 0),
        "margin_used": float(snapshot.margin_used) if snapshot else 0.0, "factor_exposure": snapshot.factor_exposure if snapshot else {},
        "kill_switch": get_kill_switch().snapshot(), "limits": current_limits().model_dump(),
    }


@app.get("/api/opportunities")
async def opportunities(limit: int = 30, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """Patch sez. 37: evento, fonte, eta, Polymarket change, asset, direzione, bid/ask, expected/realized/residual, critic, risk, stop, target, decisione."""
    repo = Repository(db)
    entries = await repo.journal_entries(limit=limit)
    out = []
    for e in entries:
        pm = e.portfolio_output or {}
        critic = e.critic_output or {}
        evidence = (e.evidence or [{}])[0] if e.evidence else {}
        out.append({
            "trade_id": e.trade_id, "ts": e.ts.isoformat(), "event": e.event_title, "source": evidence.get("source"), "source_tier": evidence.get("source_tier"),
            "age_s": round((e.ts - _parse(evidence.get("timestamp"))).total_seconds()) if evidence.get("timestamp") else None,
            "polymarket_change": (e.features or {}).get("polymarket_probability_change"), "asset": e.instrument_name, "epic": e.epic, "direction": e.direction,
            "expected_move_pct": e.expected_move_pct, "already_realized_pct": e.realized_move_pct, "residual_pct": e.residual_alpha_pct, "costs_pct": e.costs_pct, "net_alpha_pct": e.net_alpha_pct,
            "llm_confidence": e.confidence, "critic_verdict": critic.get("verdict"), "critic_score": critic.get("critic_score"), "risk_eur": float(e.risk_eur) if e.risk_eur else None, "size": float(e.size) if e.size else None,
            "stop": e.stop_level, "target": e.limit_level, "decision": pm.get("decision"), "outcome": e.outcome, "pnl": float(e.pnl) if e.pnl is not None else None, "explanation": e.explanation, "price_source": e.price_source,
        })
    return out


_FEED_EVENTS = {"maker.redeemed": "INCASSATO", "limitless.onchain.filled": "COMPRATO",
                "limitless.clob.filled": "COMPRATO",
                "limitless.onchain.sold": "VENDUTO", "maker.set_completion": "SET COMPLETATO",
                "limitless.news_exit": "NEWS EXIT"}


def _parse_feed_line(line: str) -> dict[str, Any] | None:
    """Riga structlog -> voce feed (solo operazioni REALI: compri, vendite, redeem)."""
    import re
    m = re.match(r"^(\S+?)Z?\s+\[\w+\s*\]\s+(\S+)\s+(.*)$", line.strip())
    if not m or m.group(2) not in _FEED_EVENTS:
        return None
    ts, event, rest = m.groups()
    kv = {k: v.strip("'") for k, v in re.findall(r"(\w+)=('[^']*'|\S+)", rest)}
    nice = {k: kv[k] for k in ("usdc", "expectedShares", "usdcReceived", "sharesSold",
                               "price", "size", "tx", "status") if k in kv}
    return {"ts": ts[:19], "kind": _FEED_EVENTS[event], "market": kv.get("market", ""),
            "side": kv.get("side", ""), "detail": " · ".join(f"{k} {v}" for k, v in nice.items())}


@app.get("/api/feed")
async def trade_feed() -> list[dict[str, Any]]:
    """Ultime operazioni reali del desk, dalla coda del log (nessun nuovo stato da mantenere)."""
    from pathlib import Path
    path = Path("data/runner.log")
    if not path.exists():
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(max(0, path.stat().st_size - 512_000))
            lines = fh.read().decode(errors="ignore").splitlines()
    except OSError:
        return []
    rows = [r for r in (_parse_feed_line(ln) for ln in lines) if r]
    return rows[-40:][::-1]


@app.get("/api/positions")
async def positions(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await Repository(db).open_positions(_mode())
    return [{"trade_id": r.trade_id, "epic": r.epic, "instrument": r.instrument_name, "direction": r.direction, "size": float(r.size), "entry": r.entry_price, "current": r.current_price, "pnl": float(r.unrealized_pnl or 0), "stop": r.stop_level, "limit": r.limit_level, "risk_eur": float(r.risk_eur or 0), "reason": r.reason, "exit_criteria": r.exit_criteria, "invalidation_conditions": r.invalidation_conditions, "opened_at": r.opened_at.isoformat(), "max_holding_until": r.max_holding_until.isoformat() if r.max_holding_until else None, "deal_id": r.deal_id, "mode": r.mode} for r in rows]


@app.get("/api/agent-activity")
async def agent_activity(limit: int = 40, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await Repository(db).recent_llm_decisions(limit=limit)
    return [{"ts": r.ts.isoformat(), "agent": r.agent, "model": r.model, "confidence": r.confidence, "latency_ms": r.latency_ms, "cost_usd": r.cost_usd, "tools_used": r.tools_used, "summary": _summary(r.structured_output), "error": r.error} for r in rows]


@app.get("/api/events")
async def events(limit: int = 50, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await Repository(db).recent_detected_events(minutes=24 * 60, limit=limit)
    return [{"event_id": r.event_id, "kind": r.kind, "title": r.title, "category": r.category, "detected_at": r.detected_at.isoformat(), "reliability": r.source_reliability, "verified": r.is_verified, "status": r.status, "outcome": (r.impact_map or {}).get("outcome")} for r in rows]


@app.get("/api/wallets")
async def wallets(category: str = "ALL", limit: int = 50, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await Repository(db).top_wallets(category=category, min_sample=5, limit=limit)
    return [{"address": r.wallet_address, "category": r.category, "score": r.score, "roi": r.roi, "win_rate": r.win_rate, "pnl": r.pnl, "drawdown": r.max_drawdown, "sample_size": r.sample_size, "clv": r.clv_edge, "persistence": r.persistence_score, "as_of": r.as_of.isoformat(), "metrics": {k: (r.metrics or {}).get(k) for k in ("payoff_ratio", "sharpe_like", "category_distribution", "avg_holding_time_s")}} for r in rows]


@app.get("/api/wallets/{address}")
async def wallet_detail(address: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    repo = Repository(db)
    wallet = await repo.get_wallet(address.lower())
    if wallet is None:
        raise HTTPException(404, "wallet non trovato")
    trades = await repo.wallet_trades(address.lower(), limit=200)
    positions = await repo.wallet_positions(address.lower())
    scores = [await repo.wallet_score(address.lower(), c) for c in ("ALL",)]
    return {"address": wallet.address, "label": wallet.label, "n_trades": wallet.n_trades, "volume": wallet.total_volume, "realized_pnl": wallet.realized_pnl, "unrealized_pnl": wallet.unrealized_pnl,
            "score": scores[0].score if scores and scores[0] else None, "recent_activity": [{"ts": t.ts.isoformat(), "market": t.market_question, "side": t.side, "price": t.price, "usd": t.usd_size, "category": t.category} for t in trades[-30:]],
            "open_positions": [{"market": p.market_question, "outcome": p.outcome, "size": p.size, "avg_price": p.avg_price, "current": p.current_price, "pnl": p.unrealized_pnl} for p in positions]}


# ------------------------------------------------------------- evaluation
@app.get("/api/performance")
async def performance() -> dict[str, Any]:
    mode = _mode()
    return {"performance": await realized_performance(mode=mode), "execution": await execution_quality(mode=mode), "post_signal_alpha": await post_signal_alpha_report(mode=mode)}


@app.get("/api/attribution")
async def attribution() -> dict[str, Any]:
    mode = _mode()
    return {"attribution": await attribution_report(mode=mode), "ablation": await ablation_report(mode=mode), "model_reliability": await performance_by_domain()}


@app.get("/api/costs")
async def costs() -> dict[str, Any]:
    return {"costs_30d": await cost_summary(30), "profit_after_information_cost": await profit_after_information_cost(30)}


@app.get("/api/failures")
async def failures() -> dict[str, Any]:
    return await failure_detection(mode=_mode())


@app.get("/api/audit")
async def audit_trail(limit: int = 100, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await Repository(db).audit_trail(limit=limit)
    return [{"ts": r.ts.isoformat(), "actor": r.actor, "action": r.action, "entity": r.entity, "before": r.before, "after": r.after, "note": r.note} for r in rows]


@app.get("/api/alerts")
async def alerts(limit: int = 50, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await Repository(db).recent_alerts(limit=limit)
    return [{"ts": r.ts.isoformat(), "kind": r.kind, "severity": r.severity, "title": r.title, "delivered": r.delivered} for r in rows]


@app.get("/api/strategies")
async def strategies(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = await Repository(db).list_strategies()
    return [{"strategy_id": r.strategy_id, "version": r.version, "status": r.status, "description": r.description, "capital_limit": float(r.capital_limit), "config": r.config} for r in rows]


# ------------------------------------------------------- human override
class Actor(BaseModel):
    actor: str
    note: str | None = None


class LimitsUpdate(Actor):
    changes: dict[str, Any]


class StrategyStatusUpdate(Actor):
    status: str


def _require_human(actor: str) -> None:
    if not actor or actor.lower().startswith(("llm", "agent", "system")):
        raise HTTPException(403, "richiesto un operatore umano")


@app.post("/api/control/pause")
async def pause(body: Actor) -> dict[str, Any]:
    _require_human(body.actor)
    await get_kill_switch().set_state(SystemState.PAUSED, by=body.actor)
    return get_kill_switch().snapshot()


@app.post("/api/control/stop")
async def stop(body: Actor) -> dict[str, Any]:
    _require_human(body.actor)
    await get_kill_switch().set_state(SystemState.STOPPED, by=body.actor)
    return get_kill_switch().snapshot()


@app.post("/api/control/resume")
async def resume(body: Actor) -> dict[str, Any]:
    _require_human(body.actor)
    await get_kill_switch().clear(by=body.actor)
    return get_kill_switch().snapshot()


@app.post("/api/control/close-all")
async def close_all(body: Actor) -> dict[str, Any]:
    _require_human(body.actor)
    engine = _state.get("engine")
    if engine is None:
        raise HTTPException(503, "execution engine non attivo in questo processo")
    results = await engine.close_all(by=body.actor, reason=ExitReason.MANUAL)
    await audit("manual_override", actor=body.actor, entity="positions", after={"closed": len(results)}, note=body.note)
    return {"closed": len(results)}


@app.post("/api/control/close/{trade_id}")
async def close_one(trade_id: str, body: Actor) -> dict[str, Any]:
    _require_human(body.actor)
    engine = _state.get("engine")
    if engine is None:
        raise HTTPException(503, "execution engine non attivo in questo processo")
    result = await engine.close_position(trade_id, reason=ExitReason.MANUAL, by=body.actor)
    return result.model_dump(mode="json") if result else {"closed": False}


@app.post("/api/control/strategy/{strategy_id}")
async def set_strategy_status(strategy_id: str, body: StrategyStatusUpdate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    _require_human(body.actor)
    repo = Repository(db)
    row = await repo.get_strategy(strategy_id)
    if row is None:
        raise HTTPException(404, "strategia non trovata")
    before = row.status
    row.status = body.status
    await audit("strategy_status_changed", actor=body.actor, entity="strategy", entity_id=strategy_id, before={"status": before}, after={"status": body.status}, note=body.note)
    return {"strategy_id": strategy_id, "status": body.status}


@app.post("/api/control/limits")
async def set_limits(body: LimitsUpdate) -> dict[str, Any]:
    _require_human(body.actor)
    limits = await update_limits_human(body.actor, body.changes, note=body.note)
    return limits.model_dump()


# --------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html", {"mode": _mode()})


def _summary(output: dict[str, Any]) -> str:
    if not output:
        return ""
    for key in ("decision", "verdict", "summary", "one_line_summary", "catalyst", "synthesis_of_committee", "strongest_case_against"):
        if output.get(key):
            return f"{key}: {str(output[key])[:220]}"
    return str(output)[:220]


def _parse(value: Any):
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except Exception:  # noqa: BLE001
        return utcnow() - timedelta(0)
