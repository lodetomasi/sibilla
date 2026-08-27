"""Decision loop Limitless: model-vs-market su TUTTO l'universo disponibile.

Triage economico (flash) -> due stime indipendenti -> giudice finale -> edge netto
vs prezzo -> Risk Engine deterministico -> esecuzione (PAPER ora, ordini delegati
quando il wallet e' finanziato). Size e rischio NON li decide l'LLM.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.clock import utcnow
from core.enums import Direction, EntryType, ReasonCode, SignalType
from core.logging import get_logger
from core.schemas import TradeProposal
from risk.kill_switch import get_kill_switch


def elo_win_prob(elo_a: float, elo_b: float) -> float:
    """P(A batte B) dalla differenza Elo (formula standard, base 400)."""
    return 1.0 / (1.0 + 10 ** (-(elo_a - elo_b) / 400.0))


TA_ROW_RE = r'player\.cgi\?p=[^"]+">([^<]+)</a></td><td[^>]*>[\d.]+</td><td[^>]*>([\d.]+)<'


def held_edge(p_yes: float, side: str, quote_bid: float, quote_offer: float, fee: float = 0.03) -> float:
    """Edge residuo del lato DETENUTO ai prezzi di USCITA correnti (bid per YES, 1-offer per NO).

    Sotto zero il mercato paga la posizione piu' di quanto la probabilita' aggiornata
    giustifichi: tenere e' -EV, si vende. Regola meccanica: l'LLM aggiorna solo p."""
    if side.upper() == "YES":
        return p_yes - quote_bid - fee
    return (1.0 - p_yes) - (1.0 - quote_offer) - fee


def elo_1x2(row: dict[str, str]) -> tuple[float, float, float]:
    """(p_home, p_draw, p_away) dalla riga Fixtures di ClubElo (distribuzione goal-difference)."""
    gd = {k: float(v or 0) for k, v in row.items() if k.startswith("GD")}
    p_home = gd.get("GD>5", 0.0) + sum(v for k, v in gd.items() if "=" in k and int(k.split("=")[1]) > 0)
    p_away = gd.get("GD<-5", 0.0) + sum(v for k, v in gd.items() if "=" in k and int(k.split("=")[1]) < 0)
    return p_home, gd.get("GD=0", 0.0), p_away

log = get_logger("intelligence.limitless")

STRATEGY_ID = "F_LIMITLESS_MISPRICING"


class TriageOut(BaseModel):
    judgeable: bool
    reason: str = ""


class ProbEstimate(BaseModel):
    probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    key_reasons: list[str] = Field(default_factory=list)


class JudgeOut(BaseModel):
    probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    decision: Literal["TRADE_YES", "TRADE_NO", "PASS"]
    rationale: str = ""


def compute_edge(p_model: float, yes_bid: float, yes_offer: float, fee_bps: int) -> dict[str, float | str]:
    """Edge netto in punti probabilita' su entrambi i lati, costi inclusi.

    BUY YES paga l'ask del YES; BUY NO equivale a vendere YES al bid (ask NO = 1 - bid YES).
    La fee (taker, bps sul nozionale) viene convertita in punti sul prezzo di ingresso.
    """
    fee = fee_bps / 10_000
    ask_yes = yes_offer
    ask_no = 1.0 - yes_bid
    edge_yes = p_model - ask_yes - fee * ask_yes
    edge_no = (1.0 - p_model) - ask_no - fee * ask_no
    if edge_yes >= edge_no:
        return {"side": "YES", "entry": ask_yes, "edge": edge_yes, "target": p_model}
    return {"side": "NO", "entry": ask_no, "edge": edge_no, "target": 1.0 - p_model}


def _fmt_expiry(expiry: datetime | None) -> str:
    if expiry is None:
        return "senza scadenza dichiarata"
    hours = (expiry - utcnow()).total_seconds() / 3600
    return f"{expiry.date().isoformat()} (~{hours:.0f}h da adesso)"


def _market_brief(cand: Any) -> str:
    return (
        f"Prediction market (Limitless, binary YES/NO resolution):\n"
        f"TITLE: {cand.title}\n"
        f"CATEGORIES: {', '.join(cand.categories) or 'n/a'}\n"
        f"CURRENT YES PRICE: {cand.yes_price:.3f} (= market-implied probability)\n"
        f"VOLUME: {cand.volume:,.0f} USDC\n"
        f"EXPIRY: {_fmt_expiry(cand.expiry)}\n"
        f"TODAY'S DATE: {utcnow().date().isoformat()}"
    )


class LimitlessDecisionLoop:
    def __init__(self, *, engine: Any, llm: Any, prices: Any, registry: Any, settings: Any, collector: Any):
        self.engine = engine
        self.llm = llm
        self.prices = prices
        self.registry = registry
        self.settings = settings
        self.collector = collector
        self.kill_switch = get_kill_switch()
        self.live_gateway = None
        _cfg = settings.limitless
        if _cfg.live and _cfg.onchain and _cfg.private_key:
            from execution.limitless.onchain import OnchainAmmGateway

            self.live_gateway = OnchainAmmGateway(
                private_key=_cfg.private_key.get_secret_value(), market_client=collector.client,
                max_usdc_per_order=_cfg.live_max_usdc_per_order)
            log.info("limitless.live.enabled", mode="onchain", wallet=self.live_gateway.address,
                     max_usdc_per_order=_cfg.live_max_usdc_per_order)
        elif _cfg.live and _cfg.api_key and _cfg.api_secret:
            from execution.limitless.orders import LimitlessLiveGateway

            self.live_gateway = LimitlessLiveGateway(
                api_key=_cfg.api_key.get_secret_value(), api_secret=_cfg.api_secret.get_secret_value(),
                owner_id=1440909, max_usdc_per_order=_cfg.live_max_usdc_per_order)
            log.info("limitless.live.enabled", mode="delegated", max_usdc_per_order=_cfg.live_max_usdc_per_order)
        self._cooldown_until: dict[str, datetime] = {}
        self.last_outcomes: list[dict[str, Any]] = []

    def _in_cooldown(self, market_id: str) -> bool:
        until = self._cooldown_until.get(market_id)
        return until is not None and utcnow() < until

    def _set_cooldown(self, market_id: str, seconds: float) -> None:
        from datetime import timedelta

        self._cooldown_until[market_id] = utcnow() + timedelta(seconds=seconds)

    async def on_event(self, detected: Any) -> dict[str, Any] | None:
        """News-latency sui long-tail: un evento affidabile giudica SUBITO il mercato che matcha.

        I book long-tail si riprezzano in minuti, non millisecondi: arrivare col giudizio
        prima del book e' l'unico edge di latenza alla portata di questo desk. Budget rigido
        (4 giudizi news-driven/ora) e match fuzzy >=80: il rumore non paga LLM."""
        cfg = self.settings.limitless
        if self.kill_switch.active or getattr(detected, "kind", "") not in ("NEWS", "MACRO_RELEASE"):
            return None
        # prima di cercare NUOVI ingressi: la news minaccia una posizione APERTA? (lezione Alvarez)
        try:
            await self._news_position_check(detected)
        except Exception as exc:  # noqa: BLE001 - il re-check non deve mai bloccare il flusso
            log.debug("limitless.pos_check_failed", error=str(exc)[:100])
        h = utcnow().hour
        if getattr(self, "_news_h", None) != h:
            self._news_h, self._news_n = h, 0
        if self._news_n >= 6:
            return None
        from rapidfuzz import fuzz
        title = (detected.title or "").lower()
        best, score = None, 0.0
        for cand in self.collector.candidates:
            if "up or down" in cand.title.lower():
                continue  # sugli hourly la gara di latenza e' persa comunque
            s = fuzz.token_set_ratio(title, cand.title.lower())
            if s > score:
                best, score = cand, s
        if best is None or score < 80 or self._in_cooldown(best.market_id):
            return None
        quote = self.prices.cached(best.epic)
        if quote is None:
            return None  # _judge_one ri-prezza comunque prima dell'ingresso
        self._news_n += 1
        log.info("limitless.news_trigger", market=best.market_id, match=round(score),
                 news=detected.title[:90])
        return await self._judge_one(best, quote, cfg)

    async def _news_position_check(self, detected: Any) -> None:
        """Se la news matcha una posizione reale aperta: p aggiornata dal giudice, poi regola
        meccanica held_edge < -2% => vendita sul pool + chiusura del mirror. (Lezione Alvarez:
        le news devono ri-giudicare le posizioni, non solo i nuovi ingressi.)"""
        gw = self.live_gateway
        if gw is None or not hasattr(gw, "sell_amm"):
            return
        import json as _json
        from pathlib import Path as _Path

        from rapidfuzz import fuzz
        reg = _Path("data/real_positions.json")
        if not reg.exists():
            return
        data = _json.loads(reg.read_text())
        title = (detected.title or "").lower()
        for pos in list(data.get("positions", [])):
            ref = str(pos.get("title") or pos.get("market_slug") or "").lower().replace("-", " ")
            if not ref or fuzz.token_set_ratio(title, ref) < 75:
                continue
            slug, side = pos.get("market_slug"), str(pos.get("side") or "YES")
            cand = next((c for c in self.collector.candidates if c.slug == slug), None)
            quote = await gw.fresh_quote(market_slug=slug, epic=(cand.epic if cand else f"LMTS:{slug[:12]}"))
            judge = await self.llm.complete("final_portfolio_manager", [{"role": "user", "content": (
                f"BREAKING NEWS: {detected.title}. {str(getattr(detected, 'summary', ''))[:300]}\n"
                f"Prediction market: '{pos.get('title') or slug}'. Current YES price "
                f"bid {quote.bid:.2f} / ask {quote.offer:.2f}. We hold {float(pos.get('shares') or 0):.1f} "
                f"{side} shares. In light of the news, produce the updated probability (p that the market "
                f"resolves YES), confidence, decision.")}], schema=JudgeOut)
            if not judge.parsed:
                continue
            edge = held_edge(judge.parsed.probability, side, quote.bid, quote.offer)
            log.info("limitless.position_recheck", market=str(slug)[:40], side=side,
                     p=round(judge.parsed.probability, 3), edge=round(edge, 3), news=detected.title[:70])
            if edge >= -0.02:
                continue
            out = await gw.sell_amm(side=side, market_slug=slug)
            log.info("limitless.news_exit", market=str(slug)[:40], side=side,
                     **{k: str(v)[:40] for k, v in out.items()})
            if cand is not None:
                try:
                    from core.enums import ExitReason
                    for op in list(self.engine.paper.broker_positions()):
                        if getattr(op, "epic", None) == cand.epic:
                            await self.engine.close_position(op.trade_id, reason=ExitReason.THESIS_INVALIDATED,
                                                             by="news_exit", quote=quote)
                except Exception as exc:  # noqa: BLE001
                    log.debug("limitless.mirror_close_failed", error=str(exc)[:80])

    async def cycle(self) -> dict[str, Any]:
        cfg = self.settings.limitless
        if self.kill_switch.active:
            return {"skipped": "kill_switch"}
        open_epics = {p.epic for p in self.engine.paper.broker_positions()} if self.engine.paper else set()
        if len(open_epics) >= cfg.max_open_positions:
            return {"skipped": "max_positions", "open": len(open_epics)}

        judged = 0
        opened = 0
        outcomes: list[dict[str, Any]] = []
        category_seen: dict[str, int] = {}  # max 2 giudizi/ciclo per categoria: diversificazione
        for cand in self.collector.candidates:
            if judged >= cfg.max_judged_per_cycle:
                break
            if cand.epic in open_epics or self._in_cooldown(cand.market_id):
                continue
            cat = (cand.categories[0].lower() if cand.categories else "other")
            if category_seen.get(cat, 0) >= 3:
                continue
            category_seen[cat] = category_seen.get(cat, 0) + 1
            quote = self.prices.cached(cand.epic)
            if quote is None or quote.age_seconds() > 300:
                continue
            judged += 1
            try:
                outcome = await self._judge_one(cand, quote, cfg)
            except Exception as exc:  # noqa: BLE001 - budget/timeout: fermati, riprova al prossimo giro
                log.warning("limitless.judge_failed", market=cand.market_id, error=str(exc)[:200])
                self._set_cooldown(cand.market_id, 1800)
                break
            outcomes.append(outcome)
            if outcome.get("executed"):
                opened += 1
                open_epics.add(cand.epic)
        self.last_outcomes = outcomes[-20:]
        return {"judged": judged, "opened": opened, "candidates": len(self.collector.candidates)}

    async def _sports_prior(self, cand: Any) -> str:
        """Prior quantitativo ClubElo (1X2 da rating Elo) per i mercati calcio match-style.

        Fail-closed: se le squadre non matchano il titolo, nessun prior — il comitato
        giudica come prima. CSV Fixtures cache 6h (file piccolo, API senza chiave)."""
        title = (cand.title or "").lower()
        if " vs " not in title and " beat " not in title and " win " not in title:
            return ""
        import csv
        import io

        import httpx
        try:
            if not hasattr(self, "_elo_rows") or (utcnow().timestamp() - getattr(self, "_elo_ts", 0)) > 21600:
                async with httpx.AsyncClient(timeout=10) as cli:
                    r = await cli.get("http://api.clubelo.com/Fixtures")
                self._elo_rows = list(csv.DictReader(io.StringIO(r.text)))
                self._elo_ts = utcnow().timestamp()
            for row in self._elo_rows:
                h, a = row.get("Home", ""), row.get("Away", "")
                if len(h) > 3 and len(a) > 3 and h.lower() in title and a.lower() in title:
                    ph, pd, pa = elo_1x2(row)
                    log.info("limitless.sports_prior", market=cand.market_id, home=h, away=a,
                             p=f"{ph:.2f}/{pd:.2f}/{pa:.2f}")
                    return (f"\nQUANTITATIVE PRIOR (ClubElo, historical Elo ratings): {h} wins {ph:.0%}, "
                            f"draw {pd:.0%}, {a} wins {pa:.0%}. Anchor on this prior unless there is "
                            f"very strong news (key injuries, heavy squad rotation).")
        except Exception as exc:  # noqa: BLE001 - prior opzionale: mai bloccare il giudizio
            log.debug("limitless.sports_prior_failed", error=str(exc)[:80])
        # --- tennis: Elo Tennis Abstract (ATP+WTA, copre Challenger e ITF 50K+), cache 24h
        try:
            if " vs " in title:
                elo = await self._tennis_elo()
                names = [s.strip() for s in title.split(":")[0].split(" vs ")[:2]]
                if len(names) == 2 and elo:
                    found = []
                    for n in names:
                        inv = " ".join(reversed(n.split()))  # nomi asiatici: ordine invertito
                        found.append(elo.get(n) or elo.get(inv))
                    if all(found):
                        p = elo_win_prob(found[0], found[1])
                        log.info("limitless.sports_prior", market=cand.market_id, kind="tennis",
                                 a=names[0][:20], b=names[1][:20], p=round(p, 2))
                        return (f"\nQUANTITATIVE PRIOR (Tennis Abstract Elo, updated weekly): "
                                f"{names[0]} beats {names[1]} with probability {p:.0%} (Elo {found[0]:.0f} "
                                f"vs {found[1]:.0f}). Anchor on this prior unless there is strong news "
                                f"(retirement, injury, unusual surface).")
        except Exception as exc:  # noqa: BLE001
            log.debug("limitless.tennis_prior_failed", error=str(exc)[:80])
        return ""

    async def _tennis_elo(self) -> dict[str, float]:
        """Rating Elo correnti ATP+WTA da Tennis Abstract (scrape leggero, cache 24h)."""
        import re as _re

        import httpx
        if hasattr(self, "_ta_elo") and (utcnow().timestamp() - getattr(self, "_ta_ts", 0)) < 86400:
            return self._ta_elo
        out: dict[str, float] = {}
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as cli:
            for url in ("https://tennisabstract.com/reports/atp_elo_ratings.html",
                        "https://tennisabstract.com/reports/wta_elo_ratings.html"):
                try:
                    html = (await cli.get(url)).text
                except Exception:  # noqa: BLE001 - una pagina giu' non ferma l'altra
                    continue
                for name, elo in _re.findall(TA_ROW_RE, html):
                    clean = name.replace("&nbsp;", " ").replace("\xa0", " ").strip().lower()
                    out[clean] = float(elo)
        if out:
            self._ta_elo, self._ta_ts = out, utcnow().timestamp()
        return out or getattr(self, "_ta_elo", {})

    async def _cross_intel(self, cand: Any) -> str:
        """Prezzi Polymarket di mercati simili (dal nostro DB): prior cross-venue per il giudice."""
        from core.db import session_scope
        from core.repository import Repository

        try:
            words = [w for w in cand.title.replace("?", "").split() if len(w) > 3][:4]
            if not words:
                return ""
            async with session_scope(write=False) as session:
                repo = Repository(session)
                rows = await repo.search_markets(" ".join(words), limit=6)
                lines = []
                for r in rows:
                    if r.venue != "polymarket":
                        continue
                    price = None
                    try:
                        lp = await repo.latest_price(r.id)
                        price = getattr(lp, "price", None) if lp else None
                    except Exception:  # noqa: BLE001
                        price = None
                    if price is None:
                        price = (r.raw or {}).get("yes_price") if isinstance(r.raw, dict) else None
                    lines.append(f"- Polymarket: \"{(r.question or '')[:90]}\" -> YES {price if price is not None else 'n/d'}")
                    if len(lines) >= 2:
                        break
            if lines:
                return ("\nSEGNALI CROSS-VENUE (Polymarket, liquidita' profonda — verifica che si tratti "
                        "DAVVERO dello stesso evento prima di usarli):\n" + "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            log.debug("limitless.cross_intel_failed", error=str(exc)[:100])
        return ""

    async def _journal(self, cand: Any, *, trade_id: str, outcome: str, judge: JudgeOut | None = None,
                       direction: str | None = None, entry: float | None = None, size: float | None = None,
                       risk_eur: float | None = None, stop: float | None = None, target: float | None = None,
                       net_edge: float | None = None, detail: str = "") -> None:
        """Ogni giudizio del comitato finisce nel journal (alimenta la dashboard Opportunities)."""
        from core.db import session_scope
        from core.repository import Repository

        try:
            async with session_scope() as session:
                await Repository(session).add_journal_entry(
                    trade_id=trade_id,
                    mode=self.settings.execution_mode.value,
                    strategy_id=STRATEGY_ID,
                    signal_type=SignalType.LIMITLESS_MISPRICING.value,
                    event_id=f"lmts-{cand.market_id}",
                    event_title=cand.title[:500],
                    epic=cand.epic,
                    instrument_name=cand.title[:200],
                    direction=direction,
                    entry_price=entry,
                    size=size,
                    risk_eur=risk_eur,
                    stop_level=stop,
                    limit_level=target,
                    probability=judge.probability if judge else None,
                    llm_probability=judge.probability if judge else None,
                    confidence=judge.confidence if judge else None,
                    expected_move_pct=net_edge,
                    net_alpha_pct=net_edge,
                    costs_pct=self.settings.limitless.fee_bps / 10_000,
                    features={"yes_price": cand.yes_price, "volume": cand.volume, "net_edge": net_edge},
                    evidence=[{"source": "limitless", "source_tier": "market", "timestamp": utcnow().isoformat()}],
                    portfolio_output={"decision": judge.decision if judge else None, "rationale": (judge.rationale if judge else detail)[:500]},
                    explanation=[detail[:200]] if detail else [],
                    outcome=outcome,
                )
        except Exception as exc:  # noqa: BLE001 - il journal non deve fermare il desk
            log.warning("limitless.journal_failed", error=str(exc)[:150])

    async def _fresh_quote(self, cand: Any, fallback: Any) -> Any:
        """Ri-prezza il mercato un istante prima di ordinare (iron rule: prezzo vero, mai inventato).

        Prima scelta: prezzo eseguibile dal pool FPMM on-chain (view call gratuita);
        fallback: endpoint API singolo mercato; ultima spiaggia: quote di scan."""
        from core.enums import MarketStatus
        from core.schemas import Quote
        from execution.limitless.client import parse_market

        if self.live_gateway is not None and hasattr(self.live_gateway, "fresh_quote") and cand.slug:
            try:
                q = await self.live_gateway.fresh_quote(market_slug=cand.slug, epic=cand.epic)
                self.prices.push_live(q)
                return q
            except Exception as exc:  # noqa: BLE001
                log.warning("limitless.fpmm_quote_failed", market=cand.market_id, error=str(exc)[:120])
                if "FPMM assente" in str(exc):
                    # mercato senza pool on-chain: NON eseguibile dal desk reale e il prezzo API
                    # e' quasi certamente il placeholder 50/50 -> nessun giudizio, nessun mirror
                    return None
        try:
            m = await self.collector.client.market(cand.slug or cand.market_id)
            parsed = parse_market(m) if m else None
            yes = parsed.get("yes_price") if parsed else None
            if yes is not None:
                q = Quote(epic=cand.epic, bid=max(0.001, yes - 0.01), offer=min(0.999, yes + 0.01),
                          ts=utcnow(), market_status=MarketStatus.TRADEABLE, source="limitless")
                self.prices.push_live(q)
                return q
        except Exception as exc:  # noqa: BLE001
            log.warning("limitless.requote_failed", market=cand.market_id, error=str(exc)[:120])
        # nessun prezzo VERO disponibile: il fallback di scan puo' essere il placeholder 50/50
        # (iron rule: mai calcolare edge su un prezzo inventato) -> il chiamante salta il trade
        return None

    async def _judge_one(self, cand: Any, quote: Any, cfg: Any) -> dict[str, Any]:
        sports_prior = await self._sports_prior(cand)
        brief = _market_brief(cand) + await self._cross_intel(cand) + sports_prior
        base: dict[str, Any] = {"market": cand.market_id, "title": cand.title[:80], "executed": False}

        if sports_prior:
            # prior quantitativo presente (rating Elo): il mercato e' giudicabile per
            # costruzione — dritto al comitato, senza pagare (ne' rischiare) il triage LLM
            log.info("limitless.triage_bypass", market=cand.market_id, reason="quantitative_prior")
            return await self._judge_core(cand, brief, base, cfg, fast=True)

        triage = await self.llm.complete(
            "high_volume_filter",
            [{"role": "user", "content": (
                "You are the triage desk of a prediction-market trading firm. Assess ONLY whether this "
                "market is judgeable using public knowledge and reasoning (base rates, known news, event "
                "structure) BEFORE expiry. Not judgeable: purely random very-short-term outcomes (hourly "
                "candles), manipulable outcomes, ambiguous resolution rules.\n\n" + brief)}],
            schema=TriageOut,
        )
        if not triage.parsed or not triage.parsed.judgeable:
            self._set_cooldown(cand.market_id, 12 * 3600)
            log.info("limitless.triage_skip", market=cand.market_id, reason=(triage.parsed.reason if triage.parsed else "no-parse")[:120])
            return base | {"stage": "TRIAGE_SKIP"}
        return await self._judge_core(cand, brief, base, cfg)

    async def _judge_core(self, cand: Any, brief: str, base: dict[str, Any], cfg: Any,
                          fast: bool = False) -> dict[str, Any]:
        """Comitato fino all'eventuale esecuzione. fast=True (prior quantitativo presente):
        un SOLO giudizio ancorato al prior — il numero lo ha gia' dato il rating, le due
        stime LLM sarebbero ridondanti e lente."""
        if fast:
            judge = await self.llm.complete(
                "final_portfolio_manager",
                [{"role": "user", "content": (
                    f"{brief}\n\nA QUANTITATIVE PRIOR (Elo rating) is embedded above. Produce the final "
                    "calibrated probability anchored on that prior — adjust only for strong, concrete "
                    "public news (injury, retirement, lineup) — plus the confidence, and decision: "
                    "TRADE_YES if the market underprices YES, TRADE_NO if it overprices it, PASS "
                    "otherwise. The desk pays ~3% fees plus spread: a REAL pricing error vs the current "
                    "price is required. Make sure YES is mapped to the correct side of the matchup.")}],
                schema=JudgeOut,
            )
            if not judge.parsed:
                self._set_cooldown(cand.market_id, 1800)
                return base | {"stage": "JUDGE_FAILED"}
            return await self._decide_and_execute(cand, judge.parsed, base, cfg)
        est_prompt = (
            "Estimate the probability that this market resolves YES. ALWAYS start from the historical "
            "base rate of the category, then update on evidence: for sports transfer rumors, anticipated "
            "announcements and media speculation, the large majority does NOT materialize before expiry — "
            "media hype is not evidence. Weigh remaining time: outcomes requiring multiple steps (club "
            "agreement + player consent + medicals) within days carry compound probabilities, not single "
            "ones. Do NOT anchor on the market price: an independent estimate is required. Reply with "
            "probability (0-1), confidence (0-1) and up to 3 key_reasons.\n\n" + brief
        )
        est_a = await self.llm.complete("independent_analyst", [{"role": "user", "content": est_prompt}], schema=ProbEstimate)
        est_b = await self.llm.complete("contrarian_agent", [{"role": "user", "content": est_prompt + "\n\nYou are the contrarian: actively look for reasons the consensus could be wrong."}], schema=ProbEstimate)
        if not est_a.parsed or not est_b.parsed:
            self._set_cooldown(cand.market_id, 3600)
            return base | {"stage": "ESTIMATE_FAILED"}

        judge = await self.llm.complete(
            "final_portfolio_manager",
            [{"role": "user", "content": (
                f"{brief}\n\nINDEPENDENT COMMITTEE ESTIMATES:\n"
                f"- analyst: p={est_a.parsed.probability:.3f} conf={est_a.parsed.confidence:.2f} ({'; '.join(est_a.parsed.key_reasons[:3])})\n"
                f"- contrarian: p={est_b.parsed.probability:.3f} conf={est_b.parsed.confidence:.2f} ({'; '.join(est_b.parsed.key_reasons[:3])})\n\n"
                "You are the desk's final judge. Produce the final calibrated probability, the confidence, "
                "and decision: TRADE_YES if the market underprices YES, TRADE_NO if it overprices it, PASS "
                "if the edge is doubtful or the event cannot be judged better than the market. The desk "
                "pays ~3% fees plus spread: a REAL pricing error is required, not a nuance.")}],
            schema=JudgeOut,
        )
        if not judge.parsed:
            self._set_cooldown(cand.market_id, 3600)
            return base | {"stage": "JUDGE_FAILED"}

        return await self._decide_and_execute(cand, judge.parsed, base, cfg)

    async def _decide_and_execute(self, cand: Any, j: JudgeOut, base: dict[str, Any], cfg: Any) -> dict[str, Any]:
        """Dal giudizio all'ordine: reprice eseguibile -> edge -> soglie -> esecuzione."""
        # il giudizio richiede tempo: ri-prezza il mercato (prezzo ESEGUIBILE) prima dell'ingresso
        quote = await self._fresh_quote(cand, None)
        if quote is None:
            self._set_cooldown(cand.market_id, cfg.judged_cooldown_s)
            log.info("limitless.reprice_skip", market=cand.market_id, title=cand.title[:70])
            return base | {"stage": "REPRICE_FAILED"}
        edge_info = compute_edge(j.probability, quote.bid, quote.offer, cfg.fee_bps)
        net_edge = float(edge_info["edge"])
        side = str(edge_info["side"])
        log.info("limitless.judged", market=cand.market_id, title=cand.title[:70], p=round(j.probability, 3),
                 conf=round(j.confidence, 2), decision=j.decision, side=side, net_edge=round(net_edge, 4),
                 yes_price=round(cand.yes_price, 3))

        wants_trade = j.decision != "PASS" and ((j.decision == "TRADE_YES") == (side == "YES"))
        if not wants_trade or net_edge < cfg.min_edge or j.confidence < cfg.min_confidence:
            self._set_cooldown(cand.market_id, cfg.judged_cooldown_s)
            await self._journal(cand, trade_id=f"LMTSJ{uuid.uuid4().hex[:15]}", outcome="PASS", judge=j,
                                direction=side, net_edge=net_edge,
                                detail=f"edge netto {net_edge:.3f} sotto soglia o confidence bassa")
            return base | {"stage": "PASS", "p": j.probability, "edge": net_edge}

        outcome = await self._execute(cand, quote, j, edge_info, cfg)
        self._set_cooldown(cand.market_id, cfg.judged_cooldown_s)
        return base | outcome

    async def _execute(self, cand: Any, quote: Any, j: JudgeOut, edge_info: dict[str, Any], cfg: Any) -> dict[str, Any]:
        instrument = self.registry.get(cand.epic)
        if instrument is None:
            return {"stage": "NO_INSTRUMENT"}
        from core.config import get_settings
        from core.pricing import worse_price
        from core.schemas import CostEstimate, ResidualAlpha

        limits = get_settings().risk
        direction = Direction.BUY if edge_info["side"] == "YES" else Direction.SELL
        entry_level = quote.offer if direction is Direction.BUY else quote.bid
        # target/stop in punti probabilita' sul quote YES; stop dimensionato per R:R >= 1.6
        limit_distance = max(0.02, abs(j.probability - entry_level))
        stop_distance = max(0.03, min(0.20, limit_distance / 1.6))
        if limit_distance / stop_distance < limits.min_reward_risk:
            return {"stage": "RR_TOO_LOW"}
        horizon_s = 7 * 86400
        if cand.expiry is not None:
            horizon_s = max(1800, min(int((cand.expiry - utcnow()).total_seconds()), 14 * 86400))
        horizon_s = min(horizon_s, limits.max_holding_time_s)
        # probabilita' che IL TRADE vinca (non p(YES)): per un NO e' 1 - p
        win_prob = j.probability if direction is Direction.BUY else 1.0 - j.probability
        gross_ret = limit_distance / max(entry_level, 1e-6)
        net_ret = float(edge_info["edge"]) / max(entry_level, 1e-6)
        residual = ResidualAlpha(
            epic=cand.epic,
            direction=direction,
            expected_move_pct=gross_ret,
            realized_move_pct=0.0,
            residual_move_pct=gross_ret,
            costs=CostEstimate(spread_pct=(quote.offer - quote.bid) / max(entry_level, 1e-6),
                               commission_pct=cfg.fee_bps / 10_000),
            net_alpha_pct=net_ret,
            passes=net_ret > 0,
        )
        proposal = TradeProposal(
            trade_id=f"LMTS{uuid.uuid4().hex[:16]}",
            event_id=f"lmts-{cand.market_id}",
            strategy_id=STRATEGY_ID,
            signal_type=SignalType.LIMITLESS_MISPRICING,
            instrument=instrument,
            epic=cand.epic,
            direction=direction,
            entry_type=EntryType.MARKET,
            quote=quote,
            max_entry=worse_price(entry_level, limits.max_slippage_pct * 0.9, direction.value),
            max_slippage_pct=limits.max_slippage_pct,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            stop_rationale="stop su punti probabilita': invalidazione della tesi model-vs-market",
            time_horizon_seconds=horizon_s,
            expected_return_pct=limit_distance / max(entry_level, 1e-6),
            expected_loss_pct=stop_distance / max(entry_level, 1e-6),
            probability=win_prob,
            confidence=j.confidence,
            residual=residual,
            reason_code=ReasonCode.LIMITLESS_MISPRICING,
            explanation=[
                f"{'YES' if direction is Direction.BUY else 'NO'} @ {entry_level:.3f}, p_model={j.probability:.3f}",
                f"edge netto {float(edge_info['edge']):.3f} dopo fee {cfg.fee_bps}bps e spread",
                j.rationale[:200],
            ],
            features={"net_edge": float(edge_info["edge"]), "yes_price": cand.yes_price, "volume": cand.volume},
        )
        decision = await self.engine.assess(proposal)
        if not decision.approved:
            log.info("limitless.risk_rejected", market=cand.market_id, reasons=decision.rejection_reasons[:3])
            await self._journal(cand, trade_id=proposal.trade_id, outcome="REJECTED_RISK", judge=j,
                                direction=edge_info["side"], entry=entry_level,
                                net_edge=float(edge_info["edge"]),
                                detail="; ".join(decision.rejection_reasons[:3]))
            return {"stage": "RISK_REJECTED", "reasons": decision.rejection_reasons[:3]}
        result = await self.engine.submit(proposal, decision, quote=quote)
        log.info("limitless.executed", market=cand.market_id, side=edge_info["side"], size=decision.size,
                 entry=entry_level, status=str(getattr(result, "status", "")))
        live_note = ""
        if self.live_gateway is not None and cand.slug:
            try:
                usdc_stake = decision.size * entry_level
                if getattr(cand, "trade_type", "amm") == "clob" and cand.tokens and hasattr(self.live_gateway, "place_fok"):
                    await self.live_gateway.place_fok(side=str(edge_info["side"]), tokens=cand.tokens,
                                                      market_slug=cand.slug, usdc_amount=usdc_stake)
                else:
                    await self.live_gateway.place_amm(side=str(edge_info["side"]),
                                                      market_slug=cand.slug, usdc_amount=usdc_stake)
                live_note = "LIVE ok"
            except Exception as exc:  # noqa: BLE001
                live_note = "LIVE fallito: " + str(exc)[:180]
                log.warning("limitless.live.failed", market=cand.market_id, error=str(exc)[:200])
                # il desk e' REALE: un mirror paper senza posizione on-chain e' una menzogna
                # contabile che gonfia open_risk e blocca i trade veri -> si chiude subito
                try:
                    from core.enums import ExitReason
                    await self.engine.close_position(proposal.trade_id, reason=ExitReason.THESIS_INVALIDATED,
                                                     by="live_failed", quote=quote)
                    log.info("limitless.mirror_closed", market=cand.market_id, trade_id=proposal.trade_id)
                except Exception as exc2:  # noqa: BLE001
                    log.warning("limitless.mirror_close_failed", market=cand.market_id, error=str(exc2)[:120])
        await self._journal(cand, trade_id=proposal.trade_id,
                            outcome="EXECUTED_LIVE" if live_note.startswith("LIVE ok") else "EXECUTED", judge=j,
                            direction=edge_info["side"], entry=entry_level, size=decision.size,
                            risk_eur=decision.risk_eur, stop=decision.stop_level, target=decision.limit_level,
                            net_edge=float(edge_info["edge"]), detail=(live_note + " | " + j.rationale)[:200])
        return {"stage": "EXECUTED", "executed": True, "side": edge_info["side"], "size": decision.size}
