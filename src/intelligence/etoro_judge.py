"""Judge LLM per il motore eToro: SOLO gate catalizzatore anti pump&dump.

L'LLM non decide mai size/stop/leva (quelli restano nel Risk Engine): qui
risponde solo "esiste un catalizzatore verificabile per questo movimento?".
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from core.logging import get_logger
from intelligence.llm import LLMClient
from strategies.etoro_momentum import MomentumCandidate

log = get_logger("intelligence.etoro_judge")


class CatalystVerdict(BaseModel):
    has_catalyst: bool
    direction: Literal["BUY", "NONE"] = "NONE"
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    rationale: str = ""


async def judge_catalyst(candidate: MomentumCandidate, *, news_brief: str, llm: LLMClient) -> CatalystVerdict:
    prompt = (
        "You are the catalyst gate of a penny-stock trading desk. A screener flagged this "
        f"stock for a {candidate.gap_pct:.1%} price gap on {candidate.relative_volume:.1f}x "
        "relative volume. Your ONLY job: does a verifiable, concrete news catalyst explain "
        "this move (earnings, FDA/regulatory approval, contract, M&A, guidance change)? "
        "If the move looks like pure speculation, social-media hype, or has no identifiable "
        "cause, answer has_catalyst=false — this is the anti pump-and-dump gate. You do NOT "
        "decide position size, stop loss, or leverage.\n\n"
        f"Recent news for this instrument:\n{news_brief or '(none found)'}"
    )
    try:
        result = await llm.complete("etoro_catalyst_judge", [{"role": "user", "content": prompt}], schema=CatalystVerdict)
    except Exception as exc:  # noqa: BLE001 - qualsiasi errore LLM = niente trade
        log.warning("etoro.judge_failed", instrument_id=candidate.instrument_id, error=str(exc)[:160])
        return CatalystVerdict(has_catalyst=False, rationale=f"llm_error: {exc}"[:200])
    if not result.parsed:
        return CatalystVerdict(has_catalyst=False, rationale="unparsed_response")
    return result.parsed
