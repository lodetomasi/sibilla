"""Contratti typed condivisi (sez. 17-21, 68; patch IG sez. 4-10, 12, 14-18, 24).

Tutto quello che attraversa i confini fra moduli - e in particolare tutto quello
che un LLM produce o consuma - passa da questi modelli Pydantic. Sez. 21: nessuna
esecuzione free-form, solo funzioni tipizzate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.clock import utcnow
from core.enums import (
    AlphaSource,
    AnalystDecision,
    AssetClass,
    Category,
    CriticVerdict,
    Direction,
    EntryType,
    EvidenceDirection,
    EvidenceType,
    Factor,
    FreshnessBucket,
    MacroIndicator,
    MarketStatus,
    PortfolioDecision,
    ReasonCode,
    RiskLevel,
    SignalType,
    SourceTier,
    TimeHorizon,
)
from core.pricing import PriceConvention, convention_for, implied_probability, is_price_acceptable


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- #
# Evidenza e informazione esterna
# --------------------------------------------------------------------------- #
class Evidence(StrictModel):
    """Hard rule 4 e sez. 68: ogni informazione ha fonte, timestamp, url, affidabilita."""

    evidence_id: str
    type: EvidenceType
    source: str
    source_tier: SourceTier = SourceTier.TIER_3
    url: str | None = None
    timestamp: datetime
    retrieved_at: datetime = Field(default_factory=utcnow)
    reliability: float = Field(ge=0.0, le=1.0, default=0.7)
    direction: EvidenceDirection = EvidenceDirection.SUPPORT
    impact: float = Field(ge=0.0, le=1.0, default=0.5)
    is_confirmed: bool = False
    independent_confirmations: int = 0
    summary: str = ""
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return (utcnow() - self.timestamp).total_seconds()

    @property
    def freshness(self) -> FreshnessBucket:
        return FreshnessBucket.from_seconds(self.age_seconds)


class NewsRecord(StrictModel):
    """News normalizzata da qualsiasi collector."""

    fingerprint: str
    title: str
    url: str
    source_name: str
    source_type: str = "media"
    tier: SourceTier = SourceTier.TIER_3
    reliability: float = 0.7
    summary: str | None = None
    body: str | None = None
    language: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)
    entities: list[str] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)
    cluster_id: str | None = None
    is_original: bool = True
    independent_confirmations: int = 0
    is_confirmed: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def effective_ts(self) -> datetime:
        return self.published_at or self.retrieved_at

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.effective_ts).total_seconds()


class MacroRelease(StrictModel):
    """Patch sez. 31.D - dato macro con actual/consensus/previous."""

    indicator: MacroIndicator
    name: str
    country: str = "US"
    release_time: datetime
    actual: float | None = None
    consensus: float | None = None
    previous: float | None = None
    unit: str = ""
    source: str = ""
    url: str | None = None
    retrieved_at: datetime = Field(default_factory=utcnow)

    @property
    def surprise(self) -> float | None:
        if self.actual is None or self.consensus is None:
            return None
        return self.actual - self.consensus

    @property
    def surprise_pct(self) -> float | None:
        if self.surprise is None or not self.consensus:
            return None
        return self.surprise / abs(self.consensus)


# --------------------------------------------------------------------------- #
# Eventi rilevati
# --------------------------------------------------------------------------- #
class DetectedEvent(StrictModel):
    """Output dell'event detector: il "fatto" da cui parte la pipeline."""

    event_id: str
    kind: str  # NEWS | MACRO_RELEASE | POLYMARKET_REPRICING | WALLET_CLUSTER | ANOMALY | COMPANY_EVENT
    title: str
    summary: str = ""
    category: Category = Category.OTHER
    detected_at: datetime = Field(default_factory=utcnow)
    occurred_at: datetime | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    surprise: float | None = None
    polymarket_probability_change: float | None = None
    polymarket_market_id: str | None = None
    macro: MacroRelease | None = None
    source_reliability: float = 0.0
    is_verified: bool = False
    verification_notes: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return (utcnow() - (self.occurred_at or self.detected_at)).total_seconds()

    @property
    def freshness(self) -> FreshnessBucket:
        return FreshnessBucket.from_seconds(self.age_seconds)


# --------------------------------------------------------------------------- #
# Strumenti e prezzi (patch sez. 4, 21)
# --------------------------------------------------------------------------- #
class Instrument(StrictModel):
    """Patch sez. 4 - voce dell'Instrument Registry. EPIC = identificatore IG."""

    epic: str
    name: str
    asset_class: AssetClass = AssetClass.OTHER
    currency: str = "EUR"
    market_status: MarketStatus = MarketStatus.UNKNOWN
    min_size: float = 0.1
    size_step: float = 0.1
    lot_size: float = 1.0
    contract_size: float = 1.0
    value_per_point: float = 1.0
    margin_factor: float = 5.0  # in percento (IG marginFactor con unit PERCENTAGE)
    spread: float | None = None
    trading_hours: dict[str, Any] = Field(default_factory=dict)
    controlled_risk_allowed: bool = False
    min_stop_distance: float | None = None
    min_stop_distance_unit: str = "POINTS"
    max_stop_distance: float | None = None
    scaling_factor: float = 1.0
    one_pip_means: str | None = None
    expiry: str = "-"
    streaming_available: bool = True
    aliases: list[str] = Field(default_factory=list)
    factors: dict[Factor, float] = Field(default_factory=dict)
    fallback_symbol: str | None = None  # simbolo dati pubblici usato quando IG non e collegato
    commission_pct: float = 0.0
    overnight_funding_pct_annual: float = 0.03
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return self.market_status.tradeable


class Quote(StrictModel):
    """Prezzo live di uno strumento. Iron rule: nessun prezzo inventato -> `source` obbligatoria."""

    epic: str
    bid: float
    offer: float
    ts: datetime = Field(default_factory=utcnow)
    market_status: MarketStatus = MarketStatus.UNKNOWN
    source: str  # ig-stream | ig-rest | yahoo | paper-replay
    high: float | None = None
    low: float | None = None
    change_pct: float | None = None
    delay_ms: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def mid(self) -> float:
        return (self.bid + self.offer) / 2.0

    @property
    def spread(self) -> float:
        return max(0.0, self.offer - self.bid)

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid if self.mid else 0.0

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.ts).total_seconds()

    def is_stale(self, max_age_s: float) -> bool:
        return self.age_seconds() > max_age_s

    def price_for(self, direction: Direction | str) -> float:
        return self.offer if Direction.parse(str(direction)) is Direction.BUY else self.bid

    def exit_price_for(self, direction: Direction | str) -> float:
        return self.bid if Direction.parse(str(direction)) is Direction.BUY else self.offer


class Candle(StrictModel):
    epic: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    source: str = "ig-rest"


class BookLevel(StrictModel):
    price: float
    size: float


class OrderBook(StrictModel):
    """Book Polymarket (probabilita) - usato solo come intelligence.

    asks = si compra (BUY), prezzo basso migliore; bids = si vende.
    """

    venue: str = "polymarket"
    market_id: str
    outcome: str = "YES"
    ts: datetime = Field(default_factory=utcnow)
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    status: str = "OPEN"
    price_convention: PriceConvention | None = None

    @property
    def convention(self) -> PriceConvention:
        return self.price_convention or convention_for(self.venue)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return self.best_bid if self.best_bid is not None else self.best_ask
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return abs(self.best_ask - self.best_bid)

    @property
    def implied_probability(self) -> float | None:
        mid = self.mid
        return implied_probability(mid, self.convention) if mid is not None else None

    def depth(self, levels: int = 3) -> float:
        return sum(level.size for level in self.bids[:levels]) + sum(
            level.size for level in self.asks[:levels]
        )

    def liquidity_at_or_better(self, price: float, side: str) -> float:
        levels = self.asks if side.upper() in ("BUY", "BACK") else self.bids
        return sum(
            level.size
            for level in levels
            if is_price_acceptable(level.price, price, self.convention, side)
        )

    def sort_levels(self) -> OrderBook:
        self.asks = sorted(self.asks, key=lambda level: level.price)
        self.bids = sorted(self.bids, key=lambda level: level.price, reverse=True)
        return self


class MarketSnapshot(StrictModel):
    """Stato di un mercato Polymarket in un istante (intelligence)."""

    venue: str = "polymarket"
    market_id: str
    question: str = ""
    outcome: str = "YES"
    category: Category = Category.OTHER
    ts: datetime = Field(default_factory=utcnow)
    price: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    spread: float | None = None
    mid: float | None = None
    liquidity: float | None = None
    volume: float | None = None
    status: str = "OPEN"
    suspended: bool = False
    book: OrderBook | None = None
    features: dict[str, float] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.ts).total_seconds()

    @property
    def implied_probability(self) -> float | None:
        if self.price is None:
            return None
        return implied_probability(self.price, convention_for(self.venue))


# --------------------------------------------------------------------------- #
# Wallet
# --------------------------------------------------------------------------- #
class WalletMetrics(StrictModel):
    """Sez. 5.2 - tutte le metriche richieste per wallet."""

    address: str
    category: str = "ALL"
    window_start: datetime | None = None
    window_end: datetime | None = None
    n_trades: int = 0
    n_markets: int = 0
    total_volume: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    roi: float = 0.0
    win_rate: float = 0.0
    avg_entry_price: float = 0.0
    avg_exit_price: float = 0.0
    avg_winning_trade: float = 0.0
    avg_losing_trade: float = 0.0
    payoff_ratio: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_like: float = 0.0
    sortino_like: float = 0.0
    avg_holding_time_s: float = 0.0
    category_distribution: dict[str, float] = Field(default_factory=dict)
    exposure_concentration: float = 0.0
    trade_frequency_per_day: float = 0.0
    avg_trade_size: float = 0.0
    median_trade_size: float = 0.0
    clv_edge: float = 0.0
    post_entry_drift: float = 0.0
    information_advantage: float = 0.0


class WalletScoreCard(StrictModel):
    address: str
    category: str = "ALL"
    as_of: datetime
    score: float = 0.0
    persistence_score: float = 0.0
    ranking_stability: float = 0.0
    sample_size: int = 0
    metrics: WalletMetrics | None = None
    components: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Impatto causale, reazione di mercato, residual alpha (patch sez. 5-9)
# --------------------------------------------------------------------------- #
class AffectedAsset(StrictModel):
    """Patch sez. 5 - una voce di affected_assets."""

    asset: str
    epic: str | None = None
    direction: Direction
    expected_impact: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    expected_move_pct: float | None = None  # movimento atteso in frazione (0.009 = +0.9%)
    time_horizon_seconds: int = 900
    rationale: str = ""


class CausalImpact(StrictModel):
    """Patch sez. 6 - output del Causal Impact Analyst."""

    assets: list[str]
    expected_direction: dict[str, Direction]
    expected_magnitude: dict[str, float]  # frazione, es. 0.006
    time_horizon: dict[str, int]  # secondi
    causal_chain: list[str]
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> CausalImpact:
        for asset in self.assets:
            if asset not in self.expected_direction:
                raise ValueError(f"expected_direction mancante per {asset}")
        return self


class ImpactMap(StrictModel):
    """Patch sez. 5 - output dell'Event -> Asset Mapper."""

    event: str
    event_id: str
    affected_assets: list[AffectedAsset]
    causal_chain: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    model: str = ""


class MarketReaction(StrictModel):
    """Patch sez. 7 - quanto del segnale e gia nel prezzo."""

    epic: str
    event_ts: datetime
    price_before_event: float | None = None
    price_at_event: float | None = None
    price_5s_after: float | None = None
    price_30s_after: float | None = None
    price_1m_after: float | None = None
    price_5m_after: float | None = None
    current_price: float | None = None
    realized_move: float = 0.0  # frazione, con segno
    expected_move: float = 0.0  # frazione, con segno (direzione attesa)
    residual_move: float = 0.0  # expected - realized, nel verso del trade
    volatility_pct: float | None = None
    cross_asset_confirmation: float | None = None  # -1..1
    data_source: str = ""

    @property
    def fraction_already_priced(self) -> float:
        if not self.expected_move:
            return 1.0
        return max(0.0, min(2.0, self.realized_move / self.expected_move))


class CostEstimate(StrictModel):
    """Patch sez. 19 - cost model."""

    spread_pct: float = 0.0
    commission_pct: float = 0.0
    slippage_pct: float = 0.0
    financing_pct: float = 0.0
    market_impact_pct: float = 0.0

    @property
    def total_pct(self) -> float:
        return (
            self.spread_pct
            + self.commission_pct
            + self.slippage_pct
            + self.financing_pct
            + self.market_impact_pct
        )


class ResidualAlpha(StrictModel):
    """Patch sez. 7/19: NET ALPHA = residual expected return - costi."""

    epic: str
    direction: Direction
    expected_move_pct: float
    realized_move_pct: float
    residual_move_pct: float
    costs: CostEstimate
    safety_margin_pct: float = 0.0
    net_alpha_pct: float
    passes: bool
    reaction: MarketReaction | None = None


class CrossAssetCheck(StrictModel):
    """Patch sez. 8."""

    expected: dict[str, Direction]
    observed: dict[str, float]  # epic -> realized move (frazione)
    confirmations: int = 0
    contradictions: int = 0
    score: float = 0.0  # -1..1
    interpretation: str = ""


class AssetCandidate(StrictModel):
    """Patch sez. 9 - candidato alla selezione del veicolo."""

    instrument: Instrument
    quote: Quote
    direction: Direction
    residual: ResidualAlpha
    expected_return_pct: float
    cost_pct: float
    risk_pct: float  # distanza stop in frazione
    score: float
    rationale: str = ""


# --------------------------------------------------------------------------- #
# Output degli agenti LLM
# --------------------------------------------------------------------------- #
class AnalystOutput(StrictModel):
    """Sez. 17 adattato a IG: decisione, direzione BUY/SELL, expected move, invalidazione."""

    decision: AnalystDecision
    direction: Direction | None = None
    target_asset: str | None = None
    estimated_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    expected_move_pct: float | None = None
    time_horizon_seconds: int = 900
    main_catalyst: str = ""
    supporting_evidence: list[str] = Field(default_factory=list)
    contradictory_evidence: list[str] = Field(default_factory=list)
    information_freshness_seconds: float = 0.0
    invalidation_conditions: list[str] = Field(default_factory=list)
    requested_risk_eur: float | None = None  # patch sez. 27: l'LLM chiede rischio, mai leva/size
    reason_code: str = ""
    notes: str = ""

    @model_validator(mode="after")
    def _direction_required_on_enter(self) -> AnalystOutput:
        if self.decision == AnalystDecision.ENTER and self.direction is None:
            raise ValueError("decision=ENTER richiede una direction")
        return self


class CriticOutput(StrictModel):
    """Sez. 18 + patch sez. 8."""

    verdict: CriticVerdict
    risk_level: RiskLevel = RiskLevel.MEDIUM
    blocking_reasons: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    hallucination_suspected: bool = False
    market_interpretation_differs: bool = False
    already_priced_in: bool = False
    critic_score: float = Field(ge=0.0, le=1.0, default=0.5)
    notes: str = ""

    @model_validator(mode="after")
    def _block_requires_reason(self) -> CriticOutput:
        if self.verdict == CriticVerdict.BLOCK and not self.blocking_reasons:
            raise ValueError("verdict=BLOCK richiede almeno un blocking_reason")
        return self


class PortfolioOutput(StrictModel):
    """Sez. 19 - il PM propone, non dimensiona (sez. 70, patch sez. 27/30)."""

    decision: PortfolioDecision
    risk_fraction_of_max: float = Field(ge=0.0, le=1.0, default=1.0)
    rationale: str = ""
    exit_criteria: dict[str, Any] = Field(default_factory=dict)
    concerns: list[str] = Field(default_factory=list)

    @field_validator("risk_fraction_of_max")
    @classmethod
    def _no_upsizing(cls, v: float) -> float:
        # L'LLM puo solo ridurre la frazione del rischio massimo, mai aumentarla.
        return min(max(v, 0.0), 1.0)


# --------------------------------------------------------------------------- #
# Segnale, proposta di trade, rischio, esecuzione
# --------------------------------------------------------------------------- #
class SignalPayload(StrictModel):
    """Segnale prodotto dalle strategie, prima degli LLM."""

    signal_type: SignalType
    strategy_id: str
    ts: datetime = Field(default_factory=utcnow)
    event: DetectedEvent
    candidate_assets: list[AffectedAsset] = Field(default_factory=list)
    score: float = 0.0
    components: dict[str, float] = Field(default_factory=dict)
    features: dict[str, float] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    reason_code: ReasonCode | None = None
    notes: str = ""


class TradeProposal(StrictModel):
    """Patch sez. 10 - Nuovo Trade Object.

    Attraversa Analyst -> Critic -> PM -> Risk -> Execution. Size e leva NON
    sono scelti dagli LLM: il Risk Engine li calcola dal rischio (sez. 12/27).
    """

    trade_id: str
    event_id: str
    strategy_id: str
    signal_type: SignalType
    instrument: Instrument
    epic: str
    direction: Direction
    entry_type: EntryType = EntryType.MARKET
    quote: Quote
    max_entry: float  # prezzo peggiore accettabile
    max_slippage_pct: float = 0.0005
    stop_distance: float  # punti, obbligatorio (patch sez. 14)
    limit_distance: float | None = None
    stop_rationale: str = ""
    time_horizon_seconds: int = 900
    expected_return_pct: float
    expected_loss_pct: float
    probability: float = Field(ge=0.0, le=1.0, default=0.5)
    calibrated_probability: float | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    requested_risk_eur: float | None = None
    reason_code: ReasonCode
    residual: ResidualAlpha | None = None
    cross_asset: CrossAssetCheck | None = None
    invalidation_conditions: list[str] = Field(default_factory=list)
    analyst: AnalystOutput | None = None
    critic: CriticOutput | None = None
    portfolio: PortfolioOutput | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    explanation: list[str] = Field(default_factory=list)
    latencies: dict[str, float] = Field(default_factory=dict)
    features: dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("explanation")
    @classmethod
    def _max_five_points(cls, v: list[str]) -> list[str]:
        # Sez. 50: spiegabile in massimo 5 punti.
        return v[:5]

    @field_validator("stop_distance")
    @classmethod
    def _stop_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("stop_distance deve essere > 0: NO STOP = NO TRADE")
        return v

    @property
    def reward_risk_ratio(self) -> float | None:
        if self.limit_distance is None or self.stop_distance <= 0:
            return None
        return self.limit_distance / self.stop_distance

    @property
    def horizon(self) -> TimeHorizon:
        return TimeHorizon.from_seconds(self.time_horizon_seconds)


class RiskCheck(StrictModel):
    name: str
    passed: bool
    detail: str = ""
    value: float | None = None
    limit: float | None = None


class MarginStress(StrictModel):
    """Patch sez. 28."""

    equity: float
    margin_used_before: float
    margin_required: float
    margin_used_after: float
    free_margin_after: float
    free_margin_ratio_after: float
    margin_usage_after: float
    scenarios: dict[str, float] = Field(default_factory=dict)  # "-1R" -> free margin ratio
    passes: bool


class RiskDecision(StrictModel):
    """Esito deterministico del Risk Engine (sez. 25, patch sez. 12-14, 27-28)."""

    approved: bool
    size: float = 0.0
    risk_eur: float = 0.0
    stop_distance: float = 0.0
    stop_level: float | None = None
    limit_distance: float | None = None
    limit_level: float | None = None
    max_entry: float = 0.0
    notional: float = 0.0
    margin_required: float = 0.0
    effective_leverage: float = 0.0
    checks: list[RiskCheck] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    capped_by: str | None = None
    stress: MarginStress | None = None
    computed_at: datetime = Field(default_factory=utcnow)

    @property
    def failed_checks(self) -> list[RiskCheck]:
        return [c for c in self.checks if not c.passed]


class OrderRequest(StrictModel):
    """Unico modo di chiedere un ordine (sez. 21/24, patch sez. 10/14/26)."""

    client_order_id: str
    trade_id: str
    epic: str
    direction: Direction
    size: float
    entry_type: EntryType = EntryType.MARKET
    level: float | None = None
    max_entry: float
    reference_price: float
    stop_distance: float
    limit_distance: float | None = None
    stop_level: float | None = None
    limit_level: float | None = None
    guaranteed_stop: bool = False
    currency_code: str = "EUR"
    expiry: str = "-"
    force_open: bool = True
    time_in_force: str = "FILL_OR_KILL"
    time_horizon_seconds: int = 900
    invalidation_conditions: list[str] = Field(default_factory=list)
    reason_code: ReasonCode
    reason: str = ""
    strategy_id: str | None = None
    event_id: str | None = None
    risk_eur: float = 0.0

    @field_validator("size")
    @classmethod
    def _positive_size(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("size deve essere positiva")
        return v

    @field_validator("stop_distance")
    @classmethod
    def _stop_required(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("stop_distance deve essere > 0")
        return v


class DealConfirmation(StrictModel):
    """Patch sez. 24 - conferma IG (GET /confirms/{dealReference})."""

    deal_reference: str
    deal_id: str | None = None
    deal_status: str  # ACCEPTED | REJECTED
    status: str | None = None  # OPEN | CLOSED | AMENDED | PARTIALLY_CLOSED | DELETED
    reason: str | None = None
    epic: str | None = None
    direction: Direction | None = None
    size: float | None = None
    level: float | None = None
    stop_level: float | None = None
    limit_level: float | None = None
    profit: float | None = None
    profit_currency: str | None = None
    affected_deals: list[dict[str, Any]] = Field(default_factory=list)
    date: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.deal_status.upper() == "ACCEPTED"


class OrderResult(StrictModel):
    client_order_id: str
    deal_reference: str | None = None
    deal_id: str | None = None
    status: str
    filled_size: float = 0.0
    fill_price: float | None = None
    requested_size: float = 0.0
    slippage_pct: float | None = None
    commission: float = 0.0
    error: str | None = None
    confirmation: DealConfirmation | None = None
    timings: dict[str, float] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class BrokerPosition(StrictModel):
    """Posizione come la vede il broker (per la riconciliazione, patch sez. 24)."""

    deal_id: str
    epic: str
    direction: Direction
    size: float
    level: float
    stop_level: float | None = None
    limit_level: float | None = None
    currency: str = "EUR"
    created_at: datetime | None = None
    deal_reference: str | None = None
    bid: float | None = None
    offer: float | None = None
    market_status: MarketStatus = MarketStatus.UNKNOWN
    contract_size: float | None = None
    controlled_risk: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class AccountState(StrictModel):
    """Patch sez. 3/28 - stato conto (get_account / get_balance / get_margin)."""

    account_id: str
    currency: str = "EUR"
    balance: float
    deposit: float = 0.0
    profit_loss: float = 0.0
    available: float = 0.0
    margin_used: float = 0.0
    equity: float
    ts: datetime = Field(default_factory=utcnow)
    source: str = "ig-rest"

    @property
    def free_margin(self) -> float:
        return max(0.0, self.equity - self.margin_used)

    @property
    def margin_usage(self) -> float:
        return self.margin_used / self.equity if self.equity > 0 else 1.0

    @property
    def free_margin_ratio(self) -> float:
        return self.free_margin / self.equity if self.equity > 0 else 0.0


class LatencyRecord(StrictModel):
    """Sez. 30."""

    signal_ts: datetime | None = None
    decision_ts: datetime | None = None
    risk_approval_ts: datetime | None = None
    order_submission_ts: datetime | None = None
    exchange_ack_ts: datetime | None = None
    fill_ts: datetime | None = None

    def _delta_ms(self, a: datetime | None, b: datetime | None) -> float | None:
        if a is None or b is None:
            return None
        return (b - a).total_seconds() * 1000

    @property
    def analysis_latency_ms(self) -> float | None:
        return self._delta_ms(self.signal_ts, self.decision_ts)

    @property
    def risk_latency_ms(self) -> float | None:
        return self._delta_ms(self.decision_ts, self.risk_approval_ts)

    @property
    def execution_latency_ms(self) -> float | None:
        return self._delta_ms(self.order_submission_ts, self.exchange_ack_ts)

    @property
    def fill_latency_ms(self) -> float | None:
        return self._delta_ms(self.order_submission_ts, self.fill_ts)

    @property
    def total_latency_ms(self) -> float | None:
        return self._delta_ms(self.signal_ts, self.fill_ts or self.exchange_ack_ts)

    def as_dict(self) -> dict[str, float | None]:
        return {
            "analysis_latency_ms": self.analysis_latency_ms,
            "risk_latency_ms": self.risk_latency_ms,
            "execution_latency_ms": self.execution_latency_ms,
            "fill_latency_ms": self.fill_latency_ms,
            "total_latency_ms": self.total_latency_ms,
        }


class PostSignalAlpha(StrictModel):
    """Patch sez. 35 - return dopo 5s, 30s, 1m, 5m, 15m, 1h vs entry."""

    trade_id: str
    epic: str
    direction: Direction
    entry_price: float
    returns: dict[str, float | None] = Field(default_factory=dict)  # "5s" -> frazione con segno

    @property
    def best_horizon(self) -> str | None:
        valid = {k: v for k, v in self.returns.items() if v is not None}
        return max(valid, key=valid.get) if valid else None  # type: ignore[arg-type]


class AttributionBreakdown(StrictModel):
    """Patch sez. 36."""

    total_pnl: float = 0.0
    by_source: dict[AlphaSource, float] = Field(default_factory=dict)
    method: str = "signal_weight"
    sample_size: int = 0
