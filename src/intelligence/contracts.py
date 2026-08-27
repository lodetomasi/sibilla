"""Output strutturati degli agenti del comitato (stack definitivo).

Tutti gli output sono validati con Pydantic (sez. 79: LLM structured-output tests).
Nessun campo permette all'LLM di scegliere size o leva (patch sez. 27).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.enums import AnalystDecision, Category, CriticVerdict, Direction, EntryType, RiskLevel


class LLMModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilterOutput(LLMModel):
    """DeepSeek V4 Flash - cheap relevance filter."""

    relevant: bool
    relevance: float = Field(ge=0.0, le=1.0)
    category: Category = Category.OTHER
    event_kind: str = "NEWS"  # NEWS | MACRO_RELEASE | COMPANY_EVENT | GEOPOLITICS | CRYPTO | OTHER
    is_market_moving: bool = False
    likely_assets: list[str] = Field(default_factory=list)
    one_line_summary: str = ""
    reason: str = ""


class AssetDirection(LLMModel):
    asset: str
    direction: Direction


class InvestigationOutput(LLMModel):
    """DeepSeek V4 Pro - evidence + first hypothesis."""

    verified: bool
    verification_notes: list[str] = Field(default_factory=list)
    catalyst: str
    what_changed_economically: str
    surprise_description: str = ""
    independent_sources: int = 0
    primary_source_tier: str = "TIER_3"
    first_hypothesis_assets: list[str] = Field(default_factory=list)
    first_hypothesis_directions: list[AssetDirection] = Field(default_factory=list)
    historical_analogues: list[str] = Field(default_factory=list)
    already_priced_assessment: str = ""
    red_flags: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    def direction_for(self, asset: str) -> Direction | None:
        key = asset.lower()
        for item in self.first_hypothesis_directions:
            if item.asset.lower() == key:
                return item.direction
        return None


class AnalystThesis(LLMModel):
    """Tesi indipendente (GLM 5.3 causal / Qwen indipendente / Grok contrarian)."""

    analyst: str = ""
    decision: AnalystDecision
    target_asset: str | None = None
    direction: Direction | None = None
    alternative_assets: list[str] = Field(default_factory=list)
    causal_chain: list[str] = Field(default_factory=list)
    expected_move_pct: float | None = Field(default=None, description="ampiezza attesa, frazione (0.006 = 0.6%)")
    time_horizon_seconds: int = 900
    estimated_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    already_priced_fraction: float = Field(ge=0.0, le=2.0, default=0.0)
    information_credibility: float = Field(ge=0.0, le=1.0, default=0.5)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradictory_evidence: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    second_order_effects: list[str] = Field(default_factory=list)
    reason_code: str = ""
    summary: str = ""

    @model_validator(mode="after")
    def _enter_requires_target(self) -> AnalystThesis:
        if self.decision == AnalystDecision.ENTER and (self.direction is None or not self.target_asset):
            raise ValueError("decision=ENTER richiede target_asset e direction")
        return self


class RedTeamOutput(LLMModel):
    """Kimi K3 - adversarial red team: il caso piu forte per rifiutare."""

    verdict: CriticVerdict
    risk_level: RiskLevel = RiskLevel.MEDIUM
    strongest_case_against: str
    blocking_reasons: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    already_priced_in: bool = False
    market_interpretation_differs: bool = False
    stale_or_duplicated_news: bool = False
    unreliable_source: bool = False
    hallucination_suspected: bool = False
    correlated_exposure_concern: bool = False
    critic_score: float = Field(ge=0.0, le=1.0, description="0 = trade indifendibile, 1 = nessuna obiezione seria")
    what_would_change_my_mind: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _block_requires_reason(self) -> RedTeamOutput:
        if self.verdict == CriticVerdict.BLOCK and not self.blocking_reasons:
            raise ValueError("verdict=BLOCK richiede almeno un blocking_reason")
        return self


class JudgeDecision(LLMModel):
    """GPT-5.6 Sol Pro - FINAL PORTFOLIO MANAGER.

    Ha autorita su tutta la discrezionalita: trade/no trade, asset, direzione,
    credibilita, interpretazione causale, priced-in, residual alpha, entry,
    max entry, stop/invalidation, target, orizzonte, rischio richiesto in EUR,
    uscita anticipata. NON sceglie size ne leva (patch sez. 27).
    """

    decision: str = Field(description="ENTER | ENTER_SMALL | WAIT | PASS")
    instrument: str | None = None
    epic: str | None = None
    direction: Direction | None = None
    entry_type: EntryType = EntryType.MARKET
    max_entry_slippage_pct: float = Field(ge=0.0, le=0.01, default=0.0005)
    stop_distance_pct: float | None = Field(default=None, ge=0.0, le=0.2, description="stop in frazione del prezzo (0.0025 = 0.25%)")
    target_distance_pct: float | None = Field(default=None, ge=0.0, le=0.5)
    time_horizon_seconds: int = Field(ge=30, le=86400, default=900)
    requested_risk_eur: float | None = Field(default=None, ge=0.0, description="rischio massimo in EUR che il PM chiede di allocare")
    expected_move_pct: float | None = Field(default=None, ge=0.0, le=0.5)
    already_priced_fraction: float = Field(ge=0.0, le=2.0, default=0.0)
    residual_alpha_pct: float | None = None
    information_credibility: float = Field(ge=0.0, le=1.0, default=0.5)
    estimated_probability: float = Field(ge=0.0, le=1.0, default=0.5)
    confidence: float = Field(ge=0.0, le=1.0)
    causal_interpretation: str = ""
    synthesis_of_committee: str = Field(default="", description="chi ha ragione e perche, senza votazione")
    invalidation_conditions: list[str] = Field(default_factory=list)
    early_exit_conditions: list[str] = Field(default_factory=list)
    explanation: list[str] = Field(default_factory=list, description="max 5 punti")
    reason_code: str = "MACRO_REPRICING"
    model_weights_used: list[str] = Field(default_factory=list, description="es. 'causal_analyst:0.4' - informativo")

    @model_validator(mode="after")
    def _enter_requires_fields(self) -> JudgeDecision:
        if self.decision in ("ENTER", "ENTER_SMALL"):
            missing = [
                name for name, value in (
                    ("instrument", self.instrument), ("direction", self.direction), ("stop_distance_pct", self.stop_distance_pct),
                    ("target_distance_pct", self.target_distance_pct), ("expected_move_pct", self.expected_move_pct),
                    ("requested_risk_eur", self.requested_risk_eur),
                ) if value is None
            ]
            if missing:
                raise ValueError(f"decision={self.decision} richiede: {missing}")
            if self.stop_distance_pct is not None and self.stop_distance_pct <= 0:
                raise ValueError("stop_distance_pct deve essere > 0: NO STOP = NO TRADE")
        self.explanation = self.explanation[:5]
        return self

    @property
    def enters(self) -> bool:
        return self.decision in ("ENTER", "ENTER_SMALL")


class ExitReview(LLMModel):
    """Revisione periodica posizione aperta (uscita anticipata, patch sez. 18)."""

    action: str = Field(description="HOLD | CLOSE | TIGHTEN_STOP | TAKE_PARTIAL")
    new_stop_distance_pct: float | None = None
    new_target_distance_pct: float | None = None
    reason: str = ""
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
