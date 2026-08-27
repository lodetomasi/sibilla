"""Tool tipizzati per gli agenti (sez. 20/21, patch utente).

    search_news, get_official_source, get_polymarket_market, get_polymarket_history,
    get_wallet_positions, get_ig_price, get_ig_history, get_ig_market_details,
    get_macro_data, get_economic_calendar, get_related_assets, get_cross_asset_moves,
    get_portfolio, get_positions, calculate_volatility, calculate_correlation,
    calculate_transaction_cost, calculate_position_risk

Nessun tool espone secret, nessun tool invia ordini (submit passa solo dal Risk
Engine tramite codice, mai da un tool LLM - sez. 21).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from core.clock import utcnow
from core.db import session_scope
from core.enums import Direction
from core.repository import Repository
from intelligence.llm import ToolSpec
from market.instrument_registry import InstrumentRegistry, correlation_proxy, get_registry
from market.prices import PriceService, get_price_service
from quant.features import realized_volatility, return_between
from quant.residual_alpha import estimate_costs
from risk.limits import current_limits


def _obj(**properties: Any) -> dict[str, Any]:
    required = [k for k, v in properties.items() if not v.get("_optional")]
    props = {k: {kk: vv for kk, vv in v.items() if kk != "_optional"} for k, v in properties.items()}
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _str(desc: str, optional: bool = False) -> dict[str, Any]:
    return {"type": "string", "description": desc, "_optional": optional}


def _num(desc: str, optional: bool = False) -> dict[str, Any]:
    return {"type": "number", "description": desc, "_optional": optional}


def _int(desc: str, optional: bool = False) -> dict[str, Any]:
    return {"type": "integer", "description": desc, "_optional": optional}


class AgentToolbox:
    """Costruisce la lista di ToolSpec con accesso a DB, prezzi, registry e portafoglio."""

    def __init__(self, *, prices: PriceService | None = None, registry: InstrumentRegistry | None = None, engine: Any | None = None, event_ts: datetime | None = None):
        self.prices = prices or get_price_service()
        self.registry = registry or get_registry()
        self.engine = engine
        self.event_ts = event_ts

    # ------------------------------------------------------------- news
    async def search_news(self, query: str, minutes: int = 240, limit: int = 15) -> dict[str, Any]:
        async with session_scope() as session:
            rows = await Repository(session).recent_news(minutes=minutes, query=query, limit=limit)
            if not rows:
                rows = await Repository(session).recent_news(minutes=minutes, limit=limit * 3)
                terms = [t for t in query.lower().split() if len(t) > 2]
                rows = [r for r in rows if any(t in (r.title or "").lower() or t in (r.summary or "").lower() for t in terms)][:limit]
        return {"query": query, "results": [
            {"title": r.title, "source": r.source_name, "tier": r.tier, "reliability": r.reliability, "url": r.url,
             "published_at": (r.published_at or r.retrieved_at).isoformat(), "age_seconds": round((utcnow() - (r.published_at or r.retrieved_at)).total_seconds()),
             "independent_confirmations": r.independent_confirmations, "is_original": r.is_original, "summary": (r.summary or "")[:300]}
            for r in rows]}

    async def get_official_source(self, url: str) -> dict[str, Any]:
        from collectors.news.official import fetch_official_source

        return await fetch_official_source(url)

    # -------------------------------------------------------- polymarket
    async def get_polymarket_market(self, query: str) -> dict[str, Any]:
        async with session_scope() as session:
            repo = Repository(session)
            rows = await repo.search_markets(query, limit=5)
            out = []
            for m in rows:
                price = await repo.latest_price(m.id)
                out.append({"market_id": m.external_id, "question": m.question, "category": m.category, "status": m.status, "volume": m.volume, "liquidity": m.liquidity,
                            "price": price.price if price else None, "price_ts": price.ts.isoformat() if price else None, "outcomes": m.outcomes, "resolution_date": m.resolution_date.isoformat() if m.resolution_date else None})
        return {"query": query, "markets": out}

    async def get_polymarket_history(self, market_id: str, hours: int = 24) -> dict[str, Any]:
        async with session_scope() as session:
            repo = Repository(session)
            market = await repo.get_market("polymarket", market_id)
            if market is None:
                return {"error": f"mercato {market_id} non trovato"}
            rows = await repo.price_history(market.id, since=utcnow() - timedelta(hours=hours), limit=500)
        series = [{"ts": r.ts.isoformat(), "price": r.price} for r in rows]
        change = (rows[-1].price - rows[0].price) if len(rows) >= 2 else None
        return {"market_id": market_id, "question": market.question, "points": len(series), "first": series[0] if series else None, "last": series[-1] if series else None, "change_pp": round(change * 100, 2) if change is not None else None, "series": series[-120:]}

    async def get_wallet_positions(self, market_id: str | None = None, min_score: float = 0.55, limit: int = 20) -> dict[str, Any]:
        from wallet.scoring import qualified_wallets

        wallets = await qualified_wallets(min_score=min_score, limit=limit)
        addresses = [w["address"] for w in wallets]
        async with session_scope() as session:
            repo = Repository(session)
            trades = await repo.recent_wallet_trades(minutes=180, addresses=addresses or None, limit=300)
        rows = [t for t in trades if market_id is None or t.condition_id == market_id]
        return {"qualified_wallets": wallets[:limit], "recent_trades": [
            {"wallet": t.wallet_address, "market": t.condition_id, "question": t.market_question, "outcome": t.outcome, "side": t.side, "price": t.price, "usd_size": t.usd_size, "ts": t.ts.isoformat()} for t in rows[:50]]}

    # --------------------------------------------------------------- IG
    def _resolve(self, instrument: str) -> Any:
        return self.registry.resolve(instrument) or self.registry.get(instrument)

    async def get_ig_price(self, instrument: str) -> dict[str, Any]:
        inst = self._resolve(instrument)
        if inst is None:
            return {"error": f"strumento '{instrument}' non nel registry", "available": self.registry.names()}
        quote = await self.prices.quote(inst.epic)
        return {"instrument": inst.name, "epic": inst.epic, "bid": quote.bid, "offer": quote.offer, "mid": quote.mid, "spread": quote.spread, "spread_pct": round(quote.spread_pct, 6),
                "market_status": quote.market_status.value, "ts": quote.ts.isoformat(), "age_seconds": round(quote.age_seconds(), 1), "source": quote.source, "change_pct_today": quote.change_pct}

    async def get_ig_history(self, instrument: str, minutes: int = 240) -> dict[str, Any]:
        inst = self._resolve(instrument)
        if inst is None:
            return {"error": f"strumento '{instrument}' non nel registry"}
        since = utcnow() - timedelta(minutes=minutes)
        series = await self.prices.price_series(inst.epic, since=since)
        if len(series) < 2:
            candles = await self.prices.candles(inst.epic, minutes=minutes)
            series = [(c.ts, c.close) for c in candles]
        sampled = series[:: max(1, len(series) // 60)] if series else []
        summary: dict[str, Any] = {"instrument": inst.name, "epic": inst.epic, "points": len(series), "series": [{"ts": ts.isoformat(), "mid": round(v, 5)} for ts, v in sampled]}
        if series:
            values = [v for _, v in series]
            summary.update({"first": round(values[0], 5), "last": round(values[-1], 5), "high": round(max(values), 5), "low": round(min(values), 5), "return_pct": round(values[-1] / values[0] - 1, 6) if values[0] else None})
            if self.event_ts:
                summary["return_since_event_pct"] = return_between(series, self.event_ts - timedelta(seconds=1), utcnow())
                summary["price_before_event"] = next((v for ts, v in reversed(series) if ts < self.event_ts), None)
        return summary

    async def get_ig_market_details(self, instrument: str) -> dict[str, Any]:
        inst = self._resolve(instrument)
        if inst is None:
            return {"error": f"strumento '{instrument}' non nel registry"}
        return {k: v for k, v in inst.model_dump(mode="json").items() if k not in ("raw", "aliases")}

    # ------------------------------------------------------------ macro
    async def get_macro_data(self, indicator: str | None = None, hours: int = 72) -> dict[str, Any]:
        async with session_scope() as session:
            rows = await Repository(session).upcoming_macro_releases(hours=hours)
        out = []
        for r in rows:
            if indicator and indicator.upper() not in (r.indicator, r.name.upper()):
                continue
            surprise = (r.actual - r.consensus) if r.actual is not None and r.consensus is not None else ((r.actual - r.previous) if r.actual is not None and r.previous is not None else None)
            out.append({"indicator": r.indicator, "name": r.name, "country": r.country, "release_time": r.release_time.isoformat(), "actual": r.actual, "consensus": r.consensus, "previous": r.previous, "surprise": surprise, "surprise_vs": "consensus" if r.consensus is not None else "previous", "unit": r.unit, "source": r.source})
        return {"releases": out}

    async def get_economic_calendar(self, hours: int = 48) -> dict[str, Any]:
        async with session_scope() as session:
            rows = await Repository(session).upcoming_macro_releases(hours=hours)
        return {"events": [{"indicator": r.indicator, "name": r.name, "release_time": r.release_time.isoformat(), "consensus": r.consensus, "previous": r.previous, "released": r.actual is not None} for r in rows if r.release_time >= utcnow() - timedelta(hours=1)]}

    # ------------------------------------------------------- cross asset
    async def get_related_assets(self, instrument: str) -> dict[str, Any]:
        inst = self._resolve(instrument)
        if inst is None:
            return {"error": f"strumento '{instrument}' non nel registry"}
        return {"instrument": inst.name, "factors": {k.value: v for k, v in inst.factors.items()}, "related": [{"instrument": other.name, "epic": other.epic, "structural_correlation": round(score, 3)} for other, score in self.registry.related(inst.epic)]}

    async def get_cross_asset_moves(self, minutes: int | None = None) -> dict[str, Any]:
        anchor = self.event_ts if (minutes is None and self.event_ts) else utcnow() - timedelta(minutes=minutes or 30)
        now = utcnow()
        out = []
        for inst in self.registry.all():
            series = await self.prices.price_series(inst.epic, since=anchor - timedelta(minutes=5))
            r = return_between(series, anchor - timedelta(seconds=1), now) if len(series) >= 2 else None
            if r is None:
                continue
            out.append({"instrument": inst.name, "epic": inst.epic, "return_pct": round(r, 6), "asset_class": inst.asset_class.value})
        out.sort(key=lambda x: -abs(x["return_pct"]))
        return {"anchor": anchor.isoformat(), "now": now.isoformat(), "moves": out}

    # -------------------------------------------------------- portfolio
    async def get_portfolio(self) -> dict[str, Any]:
        if self.engine is None:
            return {"error": "execution engine non disponibile"}
        context = await self.engine.portfolio_context()
        from risk.correlation import build_exposure, dominant_factor_exposure

        report = build_exposure(context.open_positions)
        limits = current_limits()
        eq = context.account.equity
        return {"equity": round(eq, 2), "balance": round(context.account.balance, 2), "margin_used": round(context.account.margin_used, 2), "free_margin_ratio": round(context.account.free_margin_ratio, 3),
                "open_risk_eur": round(context.open_risk_eur, 2), "realized_pnl_today": round(context.realized_pnl_today, 2), "open_positions": len(context.open_positions),
                "factor_exposure_vs_equity": dominant_factor_exposure(report, eq), "trades_today": context.trades_today,
                "hard_limits": {"max_risk_per_trade_eur": round(min(eq * limits.max_risk_per_trade, limits.max_stake_abs), 2), "max_open_risk_eur": round(eq * limits.max_open_risk, 2), "max_daily_loss_eur": round(eq * limits.max_daily_loss, 2), "min_reward_risk": limits.min_reward_risk, "max_holding_time_s": limits.max_holding_time_s, "max_margin_usage": limits.max_margin_usage}}

    async def get_positions(self) -> dict[str, Any]:
        mode = self.engine.mode.value if self.engine else None
        async with session_scope() as session:
            rows = await Repository(session).open_positions(mode)
        return {"positions": [{"trade_id": r.trade_id, "epic": r.epic, "instrument": r.instrument_name, "direction": r.direction, "size": float(r.size), "entry": r.entry_price, "current": r.current_price, "stop": r.stop_level, "limit": r.limit_level, "risk_eur": float(r.risk_eur or 0), "unrealized_pnl": float(r.unrealized_pnl or 0), "opened_at": r.opened_at.isoformat(), "max_holding_until": r.max_holding_until.isoformat() if r.max_holding_until else None, "event_id": r.event_id, "invalidation_conditions": r.invalidation_conditions} for r in rows]}

    # ------------------------------------------------------ calcolatori
    async def calculate_volatility(self, instrument: str, window_minutes: int = 60) -> dict[str, Any]:
        inst = self._resolve(instrument)
        if inst is None:
            return {"error": f"strumento '{instrument}' non nel registry"}
        now = utcnow()
        series = await self.prices.price_series(inst.epic, since=now - timedelta(minutes=window_minutes + 5))
        if len(series) < 5:
            candles = await self.prices.candles(inst.epic, minutes=window_minutes + 5)
            series = [(c.ts, c.close) for c in candles]
        vol = realized_volatility(series, window_s=window_minutes * 60, now=now)
        per_sec = realized_volatility(series, window_s=window_minutes * 60, now=now, per_second=True)
        quote = await self.prices.quote(inst.epic)
        horizon = {h: (per_sec * (s ** 0.5) if per_sec else None) for h, s in (("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600))}
        return {"instrument": inst.name, "points": len(series), "vol_per_bar": vol, "expected_move_pct_by_horizon": {k: round(v, 6) if v else None for k, v in horizon.items()}, "expected_move_points_15m": round(quote.mid * horizon["15m"], 4) if horizon["15m"] else None}

    async def calculate_correlation(self, instrument_a: str, instrument_b: str, minutes: int = 240) -> dict[str, Any]:
        a, b = self._resolve(instrument_a), self._resolve(instrument_b)
        if a is None or b is None:
            return {"error": "strumento non nel registry"}
        structural = correlation_proxy(a.factors, b.factors)
        since = utcnow() - timedelta(minutes=minutes)
        sa = await self.prices.price_series(a.epic, since=since)
        sb = await self.prices.price_series(b.epic, since=since)
        empirical = _empirical_corr(sa, sb)
        return {"a": a.name, "b": b.name, "structural_correlation": round(structural, 3), "empirical_correlation": round(empirical, 3) if empirical is not None else None, "points": min(len(sa), len(sb))}

    async def calculate_transaction_cost(self, instrument: str, holding_seconds: int = 900) -> dict[str, Any]:
        inst = self._resolve(instrument)
        if inst is None:
            return {"error": f"strumento '{instrument}' non nel registry"}
        quote = await self.prices.quote(inst.epic)
        costs = estimate_costs(inst, quote, holding_seconds=holding_seconds)
        return {"instrument": inst.name, **{k: round(v, 6) for k, v in costs.model_dump().items()}, "total_pct": round(costs.total_pct, 6), "note": "round-trip, frazione del prezzo"}

    async def calculate_position_risk(self, instrument: str, direction: str, stop_distance_pct: float, risk_eur: float, target_distance_pct: float | None = None) -> dict[str, Any]:
        inst = self._resolve(instrument)
        if inst is None:
            return {"error": f"strumento '{instrument}' non nel registry"}
        quote = await self.prices.quote(inst.epic)
        d = Direction.parse(direction)
        entry = quote.price_for(d)
        stop_points = entry * stop_distance_pct
        from risk.sizing import compute_size

        equity = (await self.engine.account_state()).equity if self.engine else current_limits().bankroll
        sizing = compute_size(instrument=inst, quote=quote, direction=d, stop_distance=stop_points, limits=current_limits(), equity=equity, requested_risk_eur=risk_eur, limit_distance=entry * target_distance_pct if target_distance_pct else None)
        return {"instrument": inst.name, "entry": entry, "stop_level": round(sizing.stop_level, 5), "limit_level": round(sizing.limit_level, 5) if sizing.limit_level else None, "size": sizing.size, "risk_eur": round(sizing.risk_eur, 2), "risk_budget_eur": round(sizing.risk_budget_eur, 2), "notional": round(sizing.notional, 2), "margin_required": round(sizing.margin_required, 2), "reward_risk": round(sizing.reward_risk, 2) if sizing.reward_risk else None, "capped_by": sizing.capped_by, "min_size": inst.min_size, "note": "la size finale la decide il Risk Engine; questo e' solo un calcolo indicativo"}

    # ------------------------------------------------------------ specs
    def specs(self, *, include_portfolio: bool = True) -> list[ToolSpec]:
        specs = [
            ToolSpec("search_news", "Cerca news recenti nel database (titolo, fonte, tier, eta in secondi).", _obj(query=_str("parole chiave"), minutes=_int("finestra in minuti", True), limit=_int("max risultati", True)), self.search_news),
            ToolSpec("get_official_source", "Scarica e legge una pagina di fonte ufficiale (BLS, Fed, ECB, SEC...).", _obj(url=_str("URL della fonte")), self.get_official_source),
            ToolSpec("get_polymarket_market", "Cerca mercati Polymarket per testo e ritorna prezzo/probabilita corrente.", _obj(query=_str("testo della domanda")), self.get_polymarket_market),
            ToolSpec("get_polymarket_history", "Storico probabilita di un mercato Polymarket.", _obj(market_id=_str("condition id"), hours=_int("ore", True)), self.get_polymarket_history),
            ToolSpec("get_wallet_positions", "Wallet Polymarket qualificati e loro trade recenti.", _obj(market_id=_str("condition id", True), min_score=_num("score minimo", True), limit=_int("max wallet", True)), self.get_wallet_positions),
            ToolSpec("get_ig_price", "Prezzo live bid/offer di uno strumento IG (nome, alias o epic).", _obj(instrument=_str("es. 'US Tech 100', 'EUR/USD', 'Spot Gold'")), self.get_ig_price),
            ToolSpec("get_ig_history", "Serie prezzi recente di uno strumento e return dall'evento.", _obj(instrument=_str("strumento"), minutes=_int("minuti", True)), self.get_ig_history),
            ToolSpec("get_ig_market_details", "Dettagli strumento: min size, margine, spread, orari, stop minimo.", _obj(instrument=_str("strumento")), self.get_ig_market_details),
            ToolSpec("get_macro_data", "Dati macro rilasciati/attesi (actual, consensus, previous, surprise).", _obj(indicator=_str("CPI, NFP, PCE...", True), hours=_int("finestra ore", True)), self.get_macro_data),
            ToolSpec("get_economic_calendar", "Calendario macro prossime ore.", _obj(hours=_int("ore", True)), self.get_economic_calendar),
            ToolSpec("get_related_assets", "Strumenti correlati strutturalmente (fattori).", _obj(instrument=_str("strumento")), self.get_related_assets),
            ToolSpec("get_cross_asset_moves", "Movimenti di tutti gli strumenti dall'evento (o ultimi N minuti).", _obj(minutes=_int("minuti; omesso = dall'evento", True)), self.get_cross_asset_moves),
            ToolSpec("calculate_volatility", "Volatilita realizzata e movimento atteso per orizzonte.", _obj(instrument=_str("strumento"), window_minutes=_int("finestra", True)), self.calculate_volatility),
            ToolSpec("calculate_correlation", "Correlazione strutturale ed empirica fra due strumenti.", _obj(instrument_a=_str("strumento A"), instrument_b=_str("strumento B"), minutes=_int("finestra", True)), self.calculate_correlation),
            ToolSpec("calculate_transaction_cost", "Costi round-trip (spread, commissioni, slippage, financing).", _obj(instrument=_str("strumento"), holding_seconds=_int("orizzonte", True)), self.calculate_transaction_cost),
            ToolSpec("calculate_position_risk", "Stop/target/size indicativi dato rischio EUR e stop %.", _obj(instrument=_str("strumento"), direction=_str("BUY|SELL"), stop_distance_pct=_num("stop in frazione"), risk_eur=_num("rischio EUR"), target_distance_pct=_num("target in frazione", True)), self.calculate_position_risk),
        ]
        if include_portfolio:
            specs.append(ToolSpec("get_portfolio", "Stato conto, esposizione fattoriale, limiti hard.", _obj(), self.get_portfolio))
            specs.append(ToolSpec("get_positions", "Posizioni aperte con stop/target/invalidazione.", _obj(), self.get_positions))
        return specs


def _empirical_corr(sa: list[tuple[datetime, float]], sb: list[tuple[datetime, float]]) -> float | None:
    if len(sa) < 10 or len(sb) < 10:
        return None
    # allinea per minuto
    def per_minute(series: list[tuple[datetime, float]]) -> dict[datetime, float]:
        out: dict[datetime, float] = {}
        for ts, v in series:
            out[ts.replace(second=0, microsecond=0)] = v
        return out
    ma, mb = per_minute(sa), per_minute(sb)
    keys = sorted(set(ma) & set(mb))
    if len(keys) < 10:
        return None
    ra = [ma[k2] / ma[k1] - 1 for k1, k2 in zip(keys, keys[1:], strict=False) if ma[k1]]
    rb = [mb[k2] / mb[k1] - 1 for k1, k2 in zip(keys, keys[1:], strict=False) if mb[k1]]
    n = min(len(ra), len(rb))
    if n < 5:
        return None
    ra, rb = ra[:n], rb[:n]
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(ra, rb, strict=False))
    var_a = sum((a - mean_a) ** 2 for a in ra)
    var_b = sum((b - mean_b) ** 2 for b in rb)
    if var_a <= 0 or var_b <= 0:
        return None
    return cov / (var_a ** 0.5 * var_b ** 0.5)
