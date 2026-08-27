"""Enumerazioni condivise.

Riferimenti: requisiti.md sez. 5.3, 12, 13, 19, 31-33, 47, 58, 62, 72 e
requisiti_patch_ig.md sez. 4, 10, 11, 21, 23, 36.
"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - comodita nei log
        return self.value


class Category(StrEnum):
    """Sez. 5.3 - categorie di classificazione di eventi/news/mercati Polymarket."""

    CRYPTO = "crypto"
    POLITICS = "politics"
    SPORTS = "sports"
    MACRO = "macro"
    ECONOMICS = "economics"
    TECHNOLOGY = "technology"
    GEOPOLITICS = "geopolitics"
    COMPANIES = "companies"
    SCIENCE = "science"
    ENTERTAINMENT = "entertainment"
    WEATHER = "weather"
    OTHER = "other"


class SourceTier(StrEnum):
    """Sez. 12 - affidabilita delle fonti."""

    TIER_1 = "TIER_1"  # official source
    TIER_2 = "TIER_2"  # major news agency
    TIER_3 = "TIER_3"  # reputable media
    TIER_4 = "TIER_4"  # specialized journalist
    TIER_5 = "TIER_5"  # social / anonymous

    @property
    def reliability(self) -> float:
        return _TIER_RELIABILITY[self]


_TIER_RELIABILITY: dict[SourceTier, float] = {
    SourceTier.TIER_1: 0.97,
    SourceTier.TIER_2: 0.88,
    SourceTier.TIER_3: 0.72,
    SourceTier.TIER_4: 0.58,
    SourceTier.TIER_5: 0.30,
}


class FreshnessBucket(StrEnum):
    """Sez. 13 - la freshness e una feature primaria."""

    T_0_30S = "0-30s"
    T_30_120S = "30-120s"
    T_2_5M = "2-5m"
    T_5_30M = "5-30m"
    T_OVER_30M = ">30m"

    @classmethod
    def from_seconds(cls, seconds: float) -> FreshnessBucket:
        if seconds < 30:
            return cls.T_0_30S
        if seconds < 120:
            return cls.T_30_120S
        if seconds < 300:
            return cls.T_2_5M
        if seconds < 1800:
            return cls.T_5_30M
        return cls.T_OVER_30M

    @property
    def weight(self) -> float:
        """Peso decrescente: piu la notizia e vecchia, meno edge residuo."""
        return {
            FreshnessBucket.T_0_30S: 1.0,
            FreshnessBucket.T_30_120S: 0.8,
            FreshnessBucket.T_2_5M: 0.55,
            FreshnessBucket.T_5_30M: 0.25,
            FreshnessBucket.T_OVER_30M: 0.05,
        }[self]


# --------------------------------------------------------------------------- #
# Strumenti finanziari (patch IG sez. 4, 11, 21)
# --------------------------------------------------------------------------- #
class AssetClass(StrEnum):
    """Patch sez. 4 - Instrument Universe."""

    INDICES = "INDICES"
    FOREX = "FOREX"
    COMMODITIES = "COMMODITIES"
    EQUITY_CFD = "EQUITY_CFD"
    CRYPTO_CFD = "CRYPTO_CFD"
    RATES = "RATES"
    BONDS = "BONDS"
    OTHER = "OTHER"


class Direction(StrEnum):
    """Patch sez. 11: BUY = LONG, SELL = SHORT."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.BUY else -1

    @property
    def opposite(self) -> Direction:
        return Direction.SELL if self is Direction.BUY else Direction.BUY

    @classmethod
    def parse(cls, value: str) -> Direction:
        upper = str(value).upper()
        if upper in ("BUY", "LONG"):
            return cls.BUY
        if upper in ("SELL", "SHORT"):
            return cls.SELL
        raise ValueError(f"direction non valida: {value}")


class EntryType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class MarketStatus(StrEnum):
    """Patch sez. 21 - stati IG normalizzati."""

    TRADEABLE = "TRADEABLE"
    CLOSED = "CLOSED"
    SUSPENDED = "SUSPENDED"
    EDITS_ONLY = "EDITS_ONLY"
    OFFLINE = "OFFLINE"
    ON_AUCTION = "ON_AUCTION"
    ON_AUCTION_NO_EDITS = "ON_AUCTION_NO_EDITS"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, value: str | None) -> MarketStatus:
        if not value:
            return cls.UNKNOWN
        try:
            return cls(str(value).upper())
        except ValueError:
            return cls.UNKNOWN

    @property
    def tradeable(self) -> bool:
        return self is MarketStatus.TRADEABLE


class IGEnvironment(StrEnum):
    """Patch sez. 23 - DEMO e LIVE con credenziali separate."""

    DEMO = "DEMO"
    LIVE = "LIVE"


class TimeHorizon(StrEnum):
    SECONDS = "SECONDS"
    MINUTES = "MINUTES"
    INTRADAY = "INTRADAY"
    OVERNIGHT = "OVERNIGHT"
    MULTI_DAY = "MULTI_DAY"

    @classmethod
    def from_seconds(cls, seconds: float) -> TimeHorizon:
        if seconds <= 120:
            return cls.SECONDS
        if seconds <= 1800:
            return cls.MINUTES
        if seconds <= 8 * 3600:
            return cls.INTRADAY
        if seconds <= 24 * 3600:
            return cls.OVERNIGHT
        return cls.MULTI_DAY


class Factor(StrEnum):
    """Patch sez. 29 - factor exposure."""

    US_EQUITY = "US_EQUITY"
    EU_EQUITY = "EU_EQUITY"
    ASIA_EQUITY = "ASIA_EQUITY"
    USD = "USD"
    EUR = "EUR"
    JPY = "JPY"
    GBP = "GBP"
    RATES = "RATES"
    OIL = "OIL"
    GOLD = "GOLD"
    CRYPTO = "CRYPTO"
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    VOLATILITY = "VOLATILITY"


# --------------------------------------------------------------------------- #
# Agenti LLM
# --------------------------------------------------------------------------- #
class AnalystDecision(StrEnum):
    """Sez. 17 - output dell'LLM Analyst."""

    ENTER = "ENTER"
    WAIT = "WAIT"
    PASS = "PASS"


class CriticVerdict(StrEnum):
    """Sez. 18."""

    PASS = "PASS"
    BLOCK = "BLOCK"
    REVIEW = "REVIEW"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class PortfolioDecision(StrEnum):
    """Sez. 19 - decisioni consentite al Portfolio Manager."""

    ENTER = "ENTER"
    ENTER_SMALL = "ENTER_SMALL"
    WAIT = "WAIT"
    CANCEL = "CANCEL"
    EXIT = "EXIT"
    REDUCE = "REDUCE"
    PASS = "PASS"


class ModelTier(StrEnum):
    """Sez. 39 - ensemble: A fast, B reasoning, C critic."""

    SCREEN = "SCREEN"
    FAST = "FAST"
    REASONING = "REASONING"
    CRITIC = "CRITIC"
    PORTFOLIO = "PORTFOLIO"


# --------------------------------------------------------------------------- #
# Stato operativo
# --------------------------------------------------------------------------- #
class ExecutionMode(StrEnum):
    """Sez. 31-33 - progressione verso il capitale reale.

    SHADOW: decide ma non invia; PAPER: simula fill su prezzi live IG;
    DEMO: invia a IG demo; LIVE_SMALL/LIVE: capitale reale.
    """

    SHADOW = "SHADOW"
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE_SMALL = "LIVE_SMALL"
    LIVE = "LIVE"

    @property
    def sends_orders_to_broker(self) -> bool:
        return self in (ExecutionMode.DEMO, ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE)

    @property
    def uses_real_money(self) -> bool:
        return self in (ExecutionMode.LIVE_SMALL, ExecutionMode.LIVE)

    @property
    def ig_environment(self) -> IGEnvironment:
        return IGEnvironment.LIVE if self.uses_real_money else IGEnvironment.DEMO


class AutonomyLevel(int, Enum):
    """Sez. 72."""

    ANALYTICS_ONLY = 0
    SIGNALS = 1
    SUGGESTIONS = 2
    AUTO_WITH_CONFIRMATION = 3
    FULLY_AUTONOMOUS = 4


class StrategyStatus(StrEnum):
    """Sez. 62."""

    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE_SMALL = "LIVE_SMALL"
    LIVE = "LIVE"
    DISABLED = "DISABLED"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PositionStatus(StrEnum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    OPEN = "OPEN"
    REDUCED = "REDUCED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class ExitReason(StrEnum):
    STOP_HIT = "STOP_HIT"
    TARGET_HIT = "TARGET_HIT"
    TIME_STOP = "TIME_STOP"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    KILL_SWITCH = "KILL_SWITCH"
    MANUAL = "MANUAL"
    RISK_REDUCTION = "RISK_REDUCTION"
    MARKET_CLOSE = "MARKET_CLOSE"


class EventType(StrEnum):
    """Sez. 47 - eventi sul bus."""

    PRICE_CHANGED = "PRICE_CHANGED"
    NEWS_DETECTED = "NEWS_DETECTED"
    WALLET_TRADE = "WALLET_TRADE"
    MACRO_RELEASE = "MACRO_RELEASE"
    POLYMARKET_REPRICING = "POLYMARKET_REPRICING"
    ANOMALY_DETECTED = "ANOMALY_DETECTED"
    EVENT_DETECTED = "EVENT_DETECTED"
    SIGNAL_CREATED = "SIGNAL_CREATED"
    TRADE_PROPOSED = "TRADE_PROPOSED"
    TRADE_APPROVED = "TRADE_APPROVED"
    TRADE_REJECTED = "TRADE_REJECTED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_UPDATED = "POSITION_UPDATED"
    POSITION_CLOSED = "POSITION_CLOSED"
    THESIS_INVALIDATED = "THESIS_INVALIDATED"
    MARKET_RESOLVED = "MARKET_RESOLVED"
    KILL_SWITCH_TRIGGERED = "KILL_SWITCH_TRIGGERED"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    ALERT = "ALERT"


class AlphaSource(StrEnum):
    """Patch sez. 36 - alpha attribution aggiornata."""

    NEWS = "NEWS_ALPHA"
    POLYMARKET = "POLYMARKET_ALPHA"
    WALLET = "WALLET_ALPHA"
    MACRO = "MACRO_ALPHA"
    CROSS_ASSET = "CROSS_ASSET_ALPHA"
    LLM = "LLM_ALPHA"
    TIMING = "TIMING_ALPHA"
    EXECUTION = "EXECUTION_ALPHA"


class SignalType(StrEnum):
    """Patch sez. 31 - strategie MVP."""

    BREAKING_NEWS_REPRICING = "STRATEGY_A_BREAKING_NEWS"
    POLYMARKET_ASSET_SIGNAL = "STRATEGY_B_POLYMARKET_SIGNAL"
    CROSS_ASSET_LAG = "STRATEGY_C_CROSS_ASSET_LAG"
    MACRO_RELEASE = "STRATEGY_D_MACRO_RELEASE"
    COMPANY_EVENT = "STRATEGY_E_COMPANY_EVENT"
    LIMITLESS_MISPRICING = "STRATEGY_F_LIMITLESS_MISPRICING"


class EvidenceType(StrEnum):
    """Sez. 68."""

    NEWS = "NEWS"
    OFFICIAL = "OFFICIAL"
    MACRO_DATA = "MACRO_DATA"
    POLYMARKET = "POLYMARKET"
    WALLET = "WALLET"
    QUANT = "QUANT"
    MARKET = "MARKET"
    CROSS_ASSET = "CROSS_ASSET"
    MICROSTRUCTURE = "MICROSTRUCTURE"


class EvidenceDirection(StrEnum):
    SUPPORT = "SUPPORT"
    CONTRADICT = "CONTRADICT"
    NEUTRAL = "NEUTRAL"


class ReasonCode(StrEnum):
    """Motivazioni registrate (hard rule 5: ogni ordine ha una motivazione)."""

    NEWS_NOT_FULLY_PRICED = "NEWS_NOT_FULLY_PRICED"
    MACRO_REPRICING = "MACRO_REPRICING"
    POLYMARKET_REPRICING = "POLYMARKET_REPRICING"
    CROSS_ASSET_LAG = "CROSS_ASSET_LAG"
    COMPANY_EVENT = "COMPANY_EVENT"
    WALLET_CONSENSUS = "WALLET_CONSENSUS"
    ANOMALY_INVESTIGATED = "ANOMALY_INVESTIGATED"
    LIMITLESS_MISPRICING = "LIMITLESS_MISPRICING"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    EXIT_TARGET_REACHED = "EXIT_TARGET_REACHED"
    EXIT_STOP = "EXIT_STOP"
    EXIT_TIME_STOP = "EXIT_TIME_STOP"
    EXIT_INVALIDATED = "EXIT_INVALIDATED"


class KillSwitchReason(StrEnum):
    """Sez. 27."""

    API_UNAVAILABLE = "API_UNAVAILABLE"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    MARKET_SUSPENDED = "MARKET_SUSPENDED"
    CORRUPTED_DATA = "CORRUPTED_DATA"
    EXCESSIVE_LATENCY = "EXCESSIVE_LATENCY"
    EXCESSIVE_DRAWDOWN = "EXCESSIVE_DRAWDOWN"
    ABNORMAL_LLM_BEHAVIOR = "ABNORMAL_LLM_BEHAVIOR"
    REPEATED_REJECTED_ORDERS = "REPEATED_REJECTED_ORDERS"
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"
    BALANCE_MISMATCH = "BALANCE_MISMATCH"
    MARGIN_BREACH = "MARGIN_BREACH"
    MANUAL = "MANUAL"


class SystemState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    KILLED = "KILLED"


class MacroIndicator(StrEnum):
    """Patch sez. 31.D."""

    CPI = "CPI"
    CORE_CPI = "CORE_CPI"
    PCE = "PCE"
    NFP = "NFP"
    UNEMPLOYMENT = "UNEMPLOYMENT"
    GDP = "GDP"
    PMI = "PMI"
    RETAIL_SALES = "RETAIL_SALES"
    FOMC = "FOMC"
    ECB = "ECB"
    BOE = "BOE"
    BOJ = "BOJ"
    OTHER = "OTHER"
