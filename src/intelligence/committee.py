"""Investment Committee AI (stack definitivo).

  filter (DeepSeek V4 Flash) -> investigate (DeepSeek V4 Pro)
  -> analisti INDIPENDENTI in parallelo: GLM 5.3 causal | Qwen3.8 Max | Grok 4.6 contrarian
  -> red team (Kimi K3, solo opportunita qualificate)
  -> judge (GPT-5.6 Sol Pro, agentico con tool, decisione finale)

Ogni analista riceve le stesse evidenze ma NON le conclusioni degli altri.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from core.clock import utcnow
from core.enums import Direction
from core.logging import get_logger
from core.schemas import DetectedEvent
from intelligence.contracts import (
    AnalystThesis,
    ExitReview,
    FilterOutput,
    InvestigationOutput,
    JudgeDecision,
    RedTeamOutput,
)
from intelligence.llm import LLMClient, LLMResult, ToolSpec
from intelligence.prompts import (
    CAUSAL_ANALYST_SYSTEM,
    CONTRARIAN_SYSTEM,
    EXIT_REVIEW_SYSTEM,
    FILTER_SYSTEM,
    INDEPENDENT_ANALYST_SYSTEM,
    INVESTIGATOR_SYSTEM,
    JUDGE_SYSTEM,
    RED_TEAM_SYSTEM,
)

log = get_logger("intelligence.committee")

ANALYST_PROMPTS = {
    "causal_analyst": CAUSAL_ANALYST_SYSTEM,
    "independent_analyst": INDEPENDENT_ANALYST_SYSTEM,
    "contrarian_agent": CONTRARIAN_SYSTEM,
}


def _j(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=1)


def event_brief(event: DetectedEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id, "kind": event.kind, "title": event.title, "summary": event.summary, "category": event.category.value,
        "occurred_at": (event.occurred_at or event.detected_at).isoformat(), "age_seconds": round(event.age_seconds), "freshness_bucket": event.freshness.value,
        "source_reliability": event.source_reliability, "is_verified": event.is_verified, "surprise": event.surprise,
        "polymarket_probability_change": event.polymarket_probability_change, "macro": event.macro.model_dump(mode="json") if event.macro else None,
        "entities": event.entities, "evidence": [
            {"source": e.source, "tier": e.source_tier.value, "reliability": e.reliability, "url": e.url, "timestamp": e.timestamp.isoformat(), "confirmed": e.is_confirmed, "independent_confirmations": e.independent_confirmations, "summary": e.summary}
            for e in event.evidence],
    }


class Committee:
    def __init__(self, llm: LLMClient, *, tools: list[ToolSpec] | None = None, signal_id: int | None = None):
        self.llm = llm
        self.tools = tools or []
        self.signal_id = signal_id
        self.costs: dict[str, float] = {}
        self.results: dict[str, LLMResult] = {}

    def _track(self, name: str, result: LLMResult) -> LLMResult:
        self.costs[name] = self.costs.get(name, 0.0) + result.cost_usd
        self.results[name] = result
        return result

    @property
    def total_cost(self) -> float:
        return sum(self.costs.values())

    # ------------------------------------------------------------ 1. filter
    async def filter(self, event: DetectedEvent, *, universe: list[str]) -> FilterOutput:
        messages = [
            {"role": "system", "content": FILTER_SYSTEM},
            {"role": "user", "content": f"UNIVERSO TRADABILE: {universe}\n\nELEMENTO GREZZO:\n{_j(event_brief(event))}\n\nRispondi con FilterOutput."},
        ]
        result = self._track("high_volume_filter", await self.llm.complete("high_volume_filter", messages, schema=FilterOutput, signal_id=self.signal_id, context={"event_id": event.event_id}))
        return result.parsed  # type: ignore[return-value]

    # ------------------------------------------------------- 2. investigate
    async def investigate(self, event: DetectedEvent, *, filter_output: FilterOutput, market_context: dict[str, Any]) -> InvestigationOutput:
        messages = [
            {"role": "system", "content": INVESTIGATOR_SYSTEM},
            {"role": "user", "content": (
                f"EVENTO:\n{_j(event_brief(event))}\n\nFILTRO: {_j(filter_output.model_dump())}\n\nCONTESTO DI MERCATO (prezzi live, movimenti dall'evento, calendario):\n{_j(market_context)}\n\n"
                "Verifica con i tool (fonte ufficiale, news indipendenti, storico Polymarket, dati macro, prezzi/cross-asset) e produci InvestigationOutput."
            )},
        ]
        result = self._track("investigator", await self.llm.complete("investigator", messages, schema=InvestigationOutput, tools=self.tools, signal_id=self.signal_id, context={"event_id": event.event_id}))
        return result.parsed  # type: ignore[return-value]

    # ------------------------------------------------------- 3. analysts
    async def analysts(self, event: DetectedEvent, *, investigation: InvestigationOutput, quant: dict[str, Any], market_context: dict[str, Any], roles: tuple[str, ...]) -> dict[str, AnalystThesis | None]:
        shared = (
            f"EVENTO:\n{_j(event_brief(event))}\n\nINVESTIGAZIONE (evidenze verificate, prima ipotesi):\n{_j(investigation.model_dump())}\n\n"
            f"CALCOLI QUANT (reazione gia avvenuta, expected move, residual alpha, costi, volatilita, cross-asset):\n{_j(quant)}\n\n"
            f"CONTESTO DI MERCATO:\n{_j(market_context)}\n\nProduci la tua tesi INDIPENDENTE come AnalystThesis (campo analyst = il tuo ruolo)."
        )

        async def run(role: str) -> AnalystThesis | None:
            messages = [{"role": "system", "content": ANALYST_PROMPTS[role]}, {"role": "user", "content": shared}]
            try:
                result = self._track(role, await self.llm.complete(role, messages, schema=AnalystThesis, tools=self.tools, signal_id=self.signal_id, context={"event_id": event.event_id}))
                thesis: AnalystThesis = result.parsed  # type: ignore[assignment]
                thesis.analyst = role
                return thesis
            except Exception as exc:  # noqa: BLE001 - un analista che fallisce non blocca il comitato
                log.warning("committee.analyst_failed", role=role, error=str(exc)[:200])
                return None

        outputs = await asyncio.gather(*(run(role) for role in roles))
        return dict(zip(roles, outputs, strict=False))

    # ------------------------------------------------------- 4. red team
    async def red_team(self, event: DetectedEvent, *, investigation: InvestigationOutput, theses: dict[str, AnalystThesis | None], quant: dict[str, Any], portfolio: dict[str, Any]) -> RedTeamOutput:
        messages = [
            {"role": "system", "content": RED_TEAM_SYSTEM},
            {"role": "user", "content": (
                f"EVENTO:\n{_j(event_brief(event))}\n\nINVESTIGAZIONE:\n{_j(investigation.model_dump())}\n\n"
                f"TESI DEGLI ANALISTI:\n{_j({k: (v.model_dump() if v else None) for k, v in theses.items()})}\n\nQUANT:\n{_j(quant)}\n\nPORTAFOGLIO:\n{_j(portfolio)}\n\n"
                "Trova il caso PIU FORTE per rifiutare questo trade. Produci RedTeamOutput."
            )},
        ]
        try:
            result = self._track("adversarial_red_team", await self.llm.complete("adversarial_red_team", messages, schema=RedTeamOutput, tools=self.tools, signal_id=self.signal_id, context={"event_id": event.event_id}))
        except Exception as exc:  # noqa: BLE001 - il judge deve sapere che il red team manca
            log.warning("committee.red_team_unavailable", error=str(exc)[:200])
            return RedTeamOutput(verdict="REVIEW", risk_level="HIGH", strongest_case_against=f"RED TEAM NON DISPONIBILE ({str(exc)[:120]}): nessuna verifica avversariale e' stata fatta. Il Judge deve trattarlo come un rischio aggiuntivo e preferire PASS/WAIT in caso di dubbio.", concerns=["red team timeout/errore"], critic_score=0.3)
        return result.parsed  # type: ignore[return-value]

    # ------------------------------------------------------- 5. judge
    async def judge(self, event: DetectedEvent, *, investigation: InvestigationOutput, theses: dict[str, AnalystThesis | None], red_team: RedTeamOutput | None, quant: dict[str, Any], portfolio: dict[str, Any], prices: dict[str, Any], reliability: dict[str, Any], hard_limits: dict[str, Any]) -> JudgeDecision:
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": (
                f"EVENTO E RAW EVIDENCE:\n{_j(event_brief(event))}\n\nINVESTIGAZIONE:\n{_j(investigation.model_dump())}\n\n"
                f"TESI INDIPENDENTI:\n{_j({k: (v.model_dump() if v else None) for k, v in theses.items()})}\n\n"
                f"RED TEAM:\n{_j(red_team.model_dump() if red_team else 'non eseguito (opportunita non qualificata)')}\n\n"
                f"QUANT:\n{_j(quant)}\n\nPREZZI LIVE:\n{_j(prices)}\n\nPORTAFOGLIO ED ESPOSIZIONE:\n{_j(portfolio)}\n\n"
                f"RELIABILITY STORICA PER MODELLO (categoria {event.category.value}):\n{_j(reliability)}\n\nLIMITI HARD (non negoziabili, verificati dopo di te):\n{_j(hard_limits)}\n\n"
                "Prendi la decisione finale come JudgeDecision. Usa i tool se ti servono verifiche ulteriori."
            )},
        ]
        result = self._track("final_portfolio_manager", await self.llm.complete("final_portfolio_manager", messages, schema=JudgeDecision, tools=self.tools, signal_id=self.signal_id, context={"event_id": event.event_id}))
        return result.parsed  # type: ignore[return-value]

    # ------------------------------------------------------ exit review
    async def exit_review(self, position: dict[str, Any], *, new_evidence: dict[str, Any], market: dict[str, Any]) -> ExitReview:
        messages = [
            {"role": "system", "content": EXIT_REVIEW_SYSTEM},
            {"role": "user", "content": f"POSIZIONE:\n{_j(position)}\n\nNUOVE EVIDENZE:\n{_j(new_evidence)}\n\nMERCATO:\n{_j(market)}\n\nProduci ExitReview."},
        ]
        result = self._track("final_portfolio_manager", await self.llm.complete("final_portfolio_manager", messages, schema=ExitReview, tools=self.tools, signal_id=self.signal_id, context={"trade_id": position.get("trade_id")}))
        return result.parsed  # type: ignore[return-value]

    def transcript(self) -> dict[str, Any]:
        return {name: {"model": r.model, "cost_usd": round(r.cost_usd, 5), "latency_ms": round(r.latency_ms), "tools_used": r.tools_used, "turns": r.turns, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens} for name, r in self.results.items()}


def disagreement(theses: dict[str, AnalystThesis | None]) -> float:
    """0 = tutti d'accordo, 1 = massimo disaccordo (direzione/decisione)."""
    valid = [t for t in theses.values() if t is not None]
    if len(valid) < 2:
        return 0.0
    enters = [t for t in valid if t.decision.value == "ENTER"]
    if not enters:
        return 0.0
    directions = {t.direction for t in enters if t.direction}
    frac_enter = len(enters) / len(valid)
    dir_conflict = 1.0 if len(directions) > 1 else 0.0
    return round(max(dir_conflict, 1 - abs(2 * frac_enter - 1)), 3)


def consensus_direction(theses: dict[str, AnalystThesis | None]) -> tuple[Direction | None, str | None]:
    counts: dict[tuple[str, Direction], float] = {}
    for thesis in theses.values():
        if thesis and thesis.decision.value == "ENTER" and thesis.direction and thesis.target_asset:
            key = (thesis.target_asset.lower(), thesis.direction)
            counts[key] = counts.get(key, 0.0) + thesis.confidence
    if not counts:
        return None, None
    (asset, direction), _ = max(counts.items(), key=lambda kv: kv[1])
    return direction, asset


def now_iso() -> str:
    return utcnow().isoformat()


def ts_or_now(value: datetime | None) -> datetime:
    return value or utcnow()
