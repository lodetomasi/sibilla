"""Configurazione centrale (sez. 25, 45, 52, 72; patch IG sez. 3, 13, 23).

I limiti di rischio vivono qui e sono immutabili a runtime dal punto di vista
degli agenti LLM: RiskLimits e un modello frozen e ogni modifica passa dal
canale umano tracciato nell'audit trail (sez. 53).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.enums import AutonomyLevel, ExecutionMode, IGEnvironment

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"


class RiskLimits(BaseModel):
    """Sez. 25/26 + patch sez. 13 - hard limits deterministici. Frozen: nessun LLM li modifica.

    Le frazioni sono relative all'equity del conto (patch sez. 12/13).
    """

    model_config = {"frozen": True}

    # equity di riferimento quando il broker non e raggiungibile (paper/shadow)
    bankroll: float = 500.0

    # rischio monetario
    max_risk_per_trade: float = 0.005      # 0.50% equity allo stop
    max_open_risk: float = 0.02            # 2.00% somma dei rischi allo stop delle posizioni aperte
    max_daily_loss: float = 0.02           # 2.00%
    max_weekly_drawdown: float = 0.05
    max_event_risk: float = 0.01           # rischio complessivo sullo stesso evento
    max_correlated_exposure: float = 0.015  # 1.50% rischio su posizioni correlate (stesso factor)

    # margine e leva (patch sez. 13/27/28)
    max_margin_usage: float = 0.20         # margine impegnato / equity
    min_free_margin: float = 0.70          # margine libero / equity dopo il trade
    # Nota CFD: con margin factor 5% e margin usage max 20% la leva effettiva e' gia
    # limitata a 4x; i cap nozionali sono espressi in multipli dell'equity.
    max_effective_leverage: float = 4.0    # esposizione nozionale totale / equity
    max_asset_exposure: float = 3.0        # nozionale singolo strumento / equity
    max_asset_class_exposure: float = 4.0  # nozionale per asset class / equity
    max_currency_exposure: float = 4.0     # nozionale per valuta / equity
    stress_scenarios_r: tuple[float, ...] = (1.0, 2.0)  # -1R, -2R
    stress_min_free_margin: float = 0.50   # free margin minimo dopo scenario -2R

    # stop / target / tempo (patch sez. 14-17)
    require_stop: bool = True
    min_reward_risk: float = 1.5
    max_holding_time_s: int = 4 * 3600
    default_holding_time_s: int = 900

    # execution
    kelly_fraction: float = 0.15
    min_net_alpha: float = 0.0005          # 5 bp: residual alpha netto minimo
    safety_margin_alpha: float = 0.0003    # margine di sicurezza sopra i costi
    max_slippage_pct: float = 0.0005       # 5 bp
    max_stake_abs: float = 5.0             # rischio EUR massimo per trade (live small)
    min_stake_abs: float = 0.5
    max_order_latency_ms: int = 5000
    max_data_staleness_s: float = 5.0          # feed broker (IG stream/REST)
    max_public_data_staleness_s: float = 150.0  # fallback pubblico a 1 minuto (solo PAPER/SHADOW)
    max_open_positions: int = 6
    live: bool = False                     # ordini REALI delegati (richiede deposito)
    onchain: bool = False                  # esecuzione AMM on-chain col wallet bot (permissionless)
    private_key: SecretStr | None = None   # chiave EOA bot (solo per onchain; mai in log)
    clob_api_key: SecretStr | None = None      # token HMAC dell'account EOA bot (CLOB)
    clob_api_secret: SecretStr | None = None
    maker: bool = False                    # maker set-completo sui book CLOB
    live_max_usdc_per_order: float = 2.0   # cap rigido per ordine reale
    max_positions_per_asset: int = 1
    max_rejected_orders_streak: int = 8
    max_trades_per_day: int = 20

    @field_validator("kelly_fraction")
    @classmethod
    def _kelly_never_full(cls, v: float) -> float:
        # Sez. 26: mai Full Kelly.
        if not 0 < v <= 0.25:
            raise ValueError("kelly_fraction deve essere in (0, 0.25]: mai Full Kelly")
        return v

    @field_validator(
        "max_risk_per_trade",
        "max_open_risk",
        "max_daily_loss",
        "max_weekly_drawdown",
        "max_event_risk",
        "max_correlated_exposure",
    )
    @classmethod
    def _fraction(cls, v: float) -> float:
        if not 0 < v <= 0.25:
            raise ValueError("le soglie di rischio sono frazioni di equity in (0, 0.25]")
        return v

    @field_validator("max_margin_usage", "min_free_margin", "stress_min_free_margin")
    @classmethod
    def _unit_fraction(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("frazione di margine in (0, 1]")
        return v

    @field_validator("max_effective_leverage", "max_asset_exposure", "max_asset_class_exposure", "max_currency_exposure")
    @classmethod
    def _leverage(cls, v: float) -> float:
        if not 0 < v <= 10:
            raise ValueError("i cap di leva/esposizione devono essere in (0, 10] multipli dell'equity")
        return v

    @field_validator("min_reward_risk")
    @classmethod
    def _rr(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("min_reward_risk deve essere >= 1.0")
        return v

    @model_validator(mode="after")
    def _coherent(self) -> RiskLimits:
        if self.max_risk_per_trade > self.max_open_risk:
            raise ValueError("max_risk_per_trade non puo superare max_open_risk")
        if self.max_margin_usage + self.min_free_margin > 1.0 + 1e-9:
            raise ValueError("max_margin_usage + min_free_margin non puo superare 1")
        return self


class PolymarketConfig(BaseModel):
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    data_url: str = "https://data-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws"
    # uscita di rete SOLO per Polymarket (read-only intelligence): es. 'socks5://127.0.0.1:9050'
    # per Tor, o l'endpoint SOCKS/HTTP di una VPN. Vuoto = diretto + bypass DoH.
    proxy: str | None = None
    rps: float = 8.0
    timeout_s: float = 20.0


class PolymarketTradingConfig(BaseModel):
    """Trading reale su Polymarket CLOB (venue prediction-market).

    La private key NON va mai in chat/log/git: arriva da env cifrata (enc:) o da
    secret manager; la firma EIP-712 avviene solo in locale via py-clob-client.
    """

    enabled: bool = False
    private_key: SecretStr | None = None      # chiave del wallet di firma (usa un wallet dedicato a basso saldo)
    funder_address: str | None = None         # indirizzo del proxy/funder Polymarket che detiene gli USDC
    signature_type: int = 1                   # 0=EOA, 1=email/magic (Polymarket proxy), 2=browser wallet
    chain_id: int = 137                       # Polygon mainnet
    api_key: SecretStr | None = None          # creds L2 CLOB (se gia derivate); altrimenti derivate a runtime
    api_secret: SecretStr | None = None
    api_passphrase: SecretStr | None = None
    max_stake_usdc: float = 5.0               # cap assoluto per ordine (live_small)
    default_order_type: str = "GTC"           # GTC limit; FOK/GTD supportati
    tick_size: float = 0.01
    neg_risk: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.private_key and self.funder_address)


class IGCredentials(BaseModel):
    """Credenziali di UN ambiente IG. DEMO e LIVE non condividono nulla (patch sez. 23)."""

    api_key: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    account_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.username and self.password)


class IGConfig(BaseModel):
    """Patch sez. 3/23 - IG REST + Streaming."""

    demo: IGCredentials = Field(default_factory=IGCredentials)
    live: IGCredentials = Field(default_factory=IGCredentials)
    demo_base_url: str = "https://demo-api.ig.com/gateway/deal"
    live_base_url: str = "https://api.ig.com/gateway/deal"
    rps: float = 2.0                 # IG: limiti stretti (circa 30-40 req/min non-trading)
    trading_rps: float = 1.5
    timeout_s: float = 15.0
    session_ttl_s: int = 6 * 3600
    price_allowance_guard: int = 500  # punti storici residui sotto cui smettere di scaricare
    default_currency: str = "EUR"
    streaming_enabled: bool = True
    confirm_poll_attempts: int = 8
    confirm_poll_interval_s: float = 0.5
    reconcile_interval_s: float = 30.0

    def credentials(self, env: IGEnvironment) -> IGCredentials:
        return self.demo if env is IGEnvironment.DEMO else self.live

    def base_url(self, env: IGEnvironment) -> str:
        return self.demo_base_url if env is IGEnvironment.DEMO else self.live_base_url

    def configured(self, env: IGEnvironment) -> bool:
        return self.credentials(env).configured


class LLMRole(BaseModel):
    """Un ruolo dello stack LLM: modello, scopo, parametri."""

    model: str
    role: str = ""
    reasoning_effort: str | None = None  # none | low | medium | high | max | pro
    max_output_tokens: int = 2000
    temperature: float = 0.0
    supports_json_schema: bool = True  # se False: schema forzato via tool call
    max_tool_turns: int = 0  # >0 = uso agentico con tool
    trigger: str = "always"  # always | qualified_opportunity_only | final_decision_only
    timeout_s: float = 150.0  # oltre: il ruolo viene considerato non disponibile (l'edge event-driven e' a minuti)


class LLMConfig(BaseModel):
    """Investment Committee AI (stack definitivo, 2026-08-27).

    LIVE WORLD (news, Polymarket, IG data)
      -> DEEPSEEK V4 FLASH   cheap relevance filter
      -> DEEPSEEK V4 PRO     evidence + first hypothesis (investigator)
      -> GLM 5.3 | QWEN 3.8 MAX | GROK 4.6   analisti INDIPENDENTI (non vedono gli altri)
      -> KIMI K3             adversarial red team (solo opportunita qualificate)
      -> GPT-5.6 SOL PRO     final portfolio manager (decisione finale, agentico con tool)
      -> HARD RISK KERNEL    solo vincoli matematici del conto
      -> IG

    Nessuna votazione: il PM riceve raw evidence, tutte le tesi, il red team,
    i calcoli quant, prezzi, esposizione, costi e la reliability storica per
    modello/categoria, e decide.
    """

    provider: str = "openrouter"
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    app_url: str = "https://github.com/local/automatic-trading-bot"
    app_title: str = "ATS Market Intelligence"

    high_volume_filter: LLMRole = Field(
        default_factory=lambda: LLMRole(
            model="deepseek/deepseek-v4-flash", role="cheap_relevance_filter",
            reasoning_effort="none", max_output_tokens=600, timeout_s=90,
        )
    )
    investigator: LLMRole = Field(
        default_factory=lambda: LLMRole(
            model="deepseek/deepseek-v4-pro-0813", role="evidence_and_first_hypothesis",
            reasoning_effort="medium", max_output_tokens=6000, max_tool_turns=8, timeout_s=120,
        )
    )
    causal_analyst: LLMRole = Field(
        default_factory=lambda: LLMRole(
            model="z-ai/glm-5.3", role="causal_macro_analyst",
            reasoning_effort="high", max_output_tokens=8000, max_tool_turns=4,
            supports_json_schema=False,
        )
    )
    independent_analyst: LLMRole = Field(
        default_factory=lambda: LLMRole(
            model="qwen/qwen3.8-max", role="independent_investment_analyst",
            reasoning_effort="high", max_output_tokens=6000, max_tool_turns=4,
        )
    )
    contrarian_agent: LLMRole = Field(
        default_factory=lambda: LLMRole(
            model="x-ai/grok-4.6", role="market_narrative_contrarian",
            reasoning_effort="high", max_output_tokens=6000, max_tool_turns=4,
        )
    )
    adversarial_red_team: LLMRole = Field(
        default_factory=lambda: LLMRole(
            model="moonshotai/kimi-k3", role="adversarial_red_team",
            reasoning_effort="medium", max_output_tokens=6000, max_tool_turns=4, timeout_s=180,
            trigger="qualified_opportunity_only",
        )
    )
    final_portfolio_manager: LLMRole = Field(
        default_factory=lambda: LLMRole(
            model="openai/gpt-5.6-sol-pro", role="final_autonomous_trade_decision",
            reasoning_effort="pro", max_output_tokens=10000, max_tool_turns=12, timeout_s=240,
            trigger="final_decision_only",
        )
    )

    # soglie di qualificazione della pipeline (funnel dei costi)
    filter_min_relevance: float = 0.6
    investigate_min_score: float = 0.5
    qualified_min_net_alpha: float = 0.0005  # sotto: niente red team / PM
    red_team_min_risk_eur: float = 1.0

    daily_budget_usd: float = 10.0
    # il filtro (DeepSeek Flash) costa ~$0.0002/chiamata: il vero limite di spesa e' il
    # budget giornaliero in $, non il conteggio chiamate. Cap orario alto solo come
    # backstop anti-loop; i modelli costosi partono solo su opportunita qualificate.
    max_calls_per_hour: int = 5000
    request_timeout_s: float = 180.0
    prompt_version: str = "v4-committee"
    max_structured_retries: int = 2

    # $/1M token (input, output) - prezzi OpenRouter verificati il 2026-08-27
    pricing: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {
            "deepseek/deepseek-v4-flash": (0.08, 0.16),
            "deepseek/deepseek-v4-pro-0813": (1.12, 3.37),
            "z-ai/glm-5.3": (1.40, 4.40),
            "qwen/qwen3.8-max": (2.00, 6.00),
            "x-ai/grok-4.6": (2.00, 6.00),
            "moonshotai/kimi-k3": (3.00, 15.00),
            "openai/gpt-5.6-sol-pro": (2.00, 10.00),
        }
    )

    @model_validator(mode="before")
    @classmethod
    def _merge_partial_roles(cls, data: Any) -> Any:
        """Un override flat tipo ATS_LLM_CAUSAL_ANALYST_MODEL deve cambiare SOLO il
        modello, conservando effort/tool-turns/trigger del ruolo."""
        if not isinstance(data, dict):
            return data
        merged = dict(data)
        for name, field in cls.model_fields.items():
            value = merged.get(name)
            if isinstance(value, dict) and field.default_factory is not None:
                base = field.default_factory()  # type: ignore[call-arg]
                if isinstance(base, LLMRole):
                    merged[name] = base.model_copy(update=value)
        return merged

    @property
    def api_key(self) -> SecretStr | None:
        return self.openrouter_api_key

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def price_for(self, model: str) -> tuple[float, float]:
        return self.pricing.get(model, (2.0, 10.0))

    def roles(self) -> dict[str, LLMRole]:
        return {
            "high_volume_filter": self.high_volume_filter,
            "investigator": self.investigator,
            "causal_analyst": self.causal_analyst,
            "independent_analyst": self.independent_analyst,
            "contrarian_agent": self.contrarian_agent,
            "adversarial_red_team": self.adversarial_red_team,
            "final_portfolio_manager": self.final_portfolio_manager,
        }

    @property
    def analyst_roles(self) -> tuple[str, ...]:
        """Analisti indipendenti eseguiti in parallelo, senza vedersi a vicenda."""
        return ("causal_analyst", "independent_analyst", "contrarian_agent")


class NewsConfig(BaseModel):
    newsapi_key: SecretStr | None = None
    user_agent: str = "ats-bot/0.1"
    fetch_timeout_s: float = 12.0
    max_items_per_feed: int = 60
    poll_interval_s: float = 45.0


class MacroConfig(BaseModel):
    """Patch sez. 31.D - calendario macro."""

    fred_api_key: SecretStr | None = None
    calendar_url: str | None = None
    poll_interval_s: float = 30.0


class AlertConfig(BaseModel):
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    slack_webhook_url: SecretStr | None = None
    email_to: str | None = None


class LimitlessConfig(BaseModel):
    """Venue Limitless (prediction market su Base, USDC): scan + decisione + esecuzione."""

    enabled: bool = False
    api_key: SecretStr | None = None       # HMAC lmts-api-key (solo endpoint autenticati)
    api_secret: SecretStr | None = None
    scan_interval_s: float = 90.0
    judge_interval_s: float = 120.0
    max_judged_per_cycle: int = 10         # giudizi profondi del comitato per ciclo (budget LLM, alzato con bankroll 160)
    max_pages: int = 20                    # 20 x 25 = fino a 500 mercati per scan
    min_price: float = 0.05                # fuori da qui il prezzo non e' informativo
    max_price: float = 0.95
    min_hours_to_expiry: float = 1.0
    min_edge: float = 0.05                 # punti probabilita' netti dopo fee+spread
    min_confidence: float = 0.55
    fee_bps: int = 300                     # taker fee worst case
    judged_cooldown_s: float = 10800.0     # non ri-giudicare lo stesso mercato per 3h
    max_open_positions: int = 6
    live: bool = False                     # ordini REALI delegati (richiede deposito)
    onchain: bool = False                  # esecuzione AMM on-chain col wallet bot (permissionless)
    private_key: SecretStr | None = None   # chiave EOA bot (solo per onchain; mai in log)
    clob_api_key: SecretStr | None = None      # token HMAC dell'account EOA bot (CLOB)
    clob_api_secret: SecretStr | None = None
    maker: bool = False                    # maker set-completo sui book CLOB
    live_max_usdc_per_order: float = 2.0   # cap rigido per ordine reale


class Settings(BaseSettings):
    """Settings globali; prefisso env ATS_, file .env."""

    model_config = SettingsConfigDict(
        env_prefix="ATS_",
        env_file=(PROJECT_ROOT / ".env"),
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: str = "dev"
    execution_mode: ExecutionMode = ExecutionMode.SHADOW
    autonomy_level: AutonomyLevel = AutonomyLevel.SIGNALS
    jurisdiction: str = "IT"
    log_level: str = "INFO"
    log_json: bool = False
    base_currency: str = "EUR"

    max_concurrent_events: int = 4
    api_enabled: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    database_url: str = f"sqlite+aiosqlite:///{DATA_DIR}/ats.db"
    redis_url: str | None = "redis://localhost:6379/0"
    secret_key: SecretStr | None = None

    polymarket: PolymarketConfig = Field(default_factory=PolymarketConfig)
    ig: IGConfig = Field(default_factory=IGConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    macro: MacroConfig = Field(default_factory=MacroConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    limitless: LimitlessConfig = Field(default_factory=LimitlessConfig)
    ig_enabled: bool = True

    # strumenti/venue vietati dalla giurisdizione (hard rule 9)
    blocked_epics: tuple[str, ...] = ()
    blocked_asset_classes: tuple[str, ...] = ()

    @property
    def dry_run(self) -> bool:
        return not self.execution_mode.sends_orders_to_broker

    @property
    def ig_environment(self) -> IGEnvironment:
        return self.execution_mode.ig_environment

    @model_validator(mode="after")
    def _live_requires_live_credentials(self) -> Settings:
        # Patch sez. 41: nessun passaggio automatico DEMO -> LIVE. In LIVE le
        # credenziali devono essere quelle LIVE e devono esistere.
        if self.execution_mode.uses_real_money and not self.ig.live.configured:
            raise ValueError(
                "execution_mode LIVE richiede credenziali IG LIVE (ATS_IG_LIVE_*); "
                "le credenziali DEMO non vengono mai riutilizzate"
            )
        return self


_FLAT_MAP: dict[str, tuple[str, ...]] = {
    # env var (senza prefisso) -> percorso nel modello
    "POLYMARKET_GAMMA_URL": ("polymarket", "gamma_url"),
    "POLYMARKET_CLOB_URL": ("polymarket", "clob_url"),
    "POLYMARKET_DATA_URL": ("polymarket", "data_url"),
    "POLYMARKET_WS_URL": ("polymarket", "ws_url"),
    "POLYMARKET_RPS": ("polymarket", "rps"),
    "POLYMARKET_PROXY": ("polymarket", "proxy"),
    "IG_DEMO_API_KEY": ("ig", "demo", "api_key"),
    "IG_DEMO_USERNAME": ("ig", "demo", "username"),
    "IG_DEMO_PASSWORD": ("ig", "demo", "password"),
    "IG_DEMO_ACCOUNT_ID": ("ig", "demo", "account_id"),
    "IG_LIVE_API_KEY": ("ig", "live", "api_key"),
    "IG_LIVE_USERNAME": ("ig", "live", "username"),
    "IG_LIVE_PASSWORD": ("ig", "live", "password"),
    "IG_LIVE_ACCOUNT_ID": ("ig", "live", "account_id"),
    "IG_DEMO_BASE_URL": ("ig", "demo_base_url"),
    "IG_LIVE_BASE_URL": ("ig", "live_base_url"),
    "IG_RPS": ("ig", "rps"),
    "IG_DEFAULT_CURRENCY": ("ig", "default_currency"),
    "IG_STREAMING_ENABLED": ("ig", "streaming_enabled"),
    "IG_ENABLED": ("ig_enabled",),
    "LIMITLESS_ENABLED": ("limitless", "enabled"),
    "LIMITLESS_API_KEY": ("limitless", "api_key"),
    "LIMITLESS_API_SECRET": ("limitless", "api_secret"),
    "LIMITLESS_LIVE": ("limitless", "live"),
    "LIMITLESS_LIVE_MAX_USDC": ("limitless", "live_max_usdc_per_order"),
    "LIMITLESS_ONCHAIN": ("limitless", "onchain"),
    "LIMITLESS_PRIVATE_KEY": ("limitless", "private_key"),
    "LIMITLESS_CLOB_API_KEY": ("limitless", "clob_api_key"),
    "LIMITLESS_CLOB_API_SECRET": ("limitless", "clob_api_secret"),
    "LIMITLESS_MAKER": ("limitless", "maker"),
    "OPENROUTER_API_KEY": ("llm", "openrouter_api_key"),
    "OPENROUTER_BASE_URL": ("llm", "openrouter_base_url"),
    "LLM_PROVIDER": ("llm", "provider"),
    "LLM_HIGH_VOLUME_FILTER_MODEL": ("llm", "high_volume_filter", "model"),
    "LLM_INVESTIGATOR_MODEL": ("llm", "investigator", "model"),
    "LLM_CAUSAL_ANALYST_MODEL": ("llm", "causal_analyst", "model"),
    "LLM_INDEPENDENT_ANALYST_MODEL": ("llm", "independent_analyst", "model"),
    "LLM_CONTRARIAN_AGENT_MODEL": ("llm", "contrarian_agent", "model"),
    "LLM_ADVERSARIAL_RED_TEAM_MODEL": ("llm", "adversarial_red_team", "model"),
    "LLM_FINAL_PORTFOLIO_MANAGER_MODEL": ("llm", "final_portfolio_manager", "model"),
    "LLM_DAILY_BUDGET_USD": ("llm", "daily_budget_usd"),
    "LLM_MAX_CALLS_PER_HOUR": ("llm", "max_calls_per_hour"),
    "NEWSAPI_KEY": ("news", "newsapi_key"),
    "NEWS_USER_AGENT": ("news", "user_agent"),
    "FRED_API_KEY": ("macro", "fred_api_key"),
    "MACRO_CALENDAR_URL": ("macro", "calendar_url"),
    "TELEGRAM_BOT_TOKEN": ("alerts", "telegram_bot_token"),
    "TELEGRAM_CHAT_ID": ("alerts", "telegram_chat_id"),
    "SLACK_WEBHOOK_URL": ("alerts", "slack_webhook_url"),
    "BANKROLL": ("risk", "bankroll"),
    "MAX_RISK_PER_TRADE": ("risk", "max_risk_per_trade"),
    "MAX_OPEN_RISK": ("risk", "max_open_risk"),
    "MAX_DAILY_LOSS": ("risk", "max_daily_loss"),
    "MAX_WEEKLY_DRAWDOWN": ("risk", "max_weekly_drawdown"),
    "MAX_EVENT_RISK": ("risk", "max_event_risk"),
    "MAX_CORRELATED_EXPOSURE": ("risk", "max_correlated_exposure"),
    "MAX_MARGIN_USAGE": ("risk", "max_margin_usage"),
    "MIN_FREE_MARGIN": ("risk", "min_free_margin"),
    "MAX_EFFECTIVE_LEVERAGE": ("risk", "max_effective_leverage"),
    "MAX_ASSET_EXPOSURE": ("risk", "max_asset_exposure"),
    "MAX_ASSET_CLASS_EXPOSURE": ("risk", "max_asset_class_exposure"),
    "MAX_CURRENCY_EXPOSURE": ("risk", "max_currency_exposure"),
    "MIN_REWARD_RISK": ("risk", "min_reward_risk"),
    "MAX_HOLDING_TIME_S": ("risk", "max_holding_time_s"),
    "KELLY_FRACTION": ("risk", "kelly_fraction"),
    "MIN_NET_ALPHA": ("risk", "min_net_alpha"),
    "MAX_SLIPPAGE_PCT": ("risk", "max_slippage_pct"),
    "MAX_DATA_STALENESS_S": ("risk", "max_data_staleness_s"),
    "MAX_PUBLIC_DATA_STALENESS_S": ("risk", "max_public_data_staleness_s"),
    "STRESS_MIN_FREE_MARGIN": ("risk", "stress_min_free_margin"),
    "MAX_STAKE_ABS": ("risk", "max_stake_abs"),
    "REQUIRE_STOP": ("risk", "require_stop"),
}


def _collect_flat_overrides(env: dict[str, str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for raw_key, path in _FLAT_MAP.items():
        value = env.get(f"ATS_{raw_key}")
        if value in (None, ""):
            continue
        cursor: dict[str, object] = out
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})  # type: ignore[assignment]
        cursor[path[-1]] = value
    return out


def _deep_merge(base: dict[str, object], extra: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def load_settings(**overrides: object) -> Settings:
    """Costruisce le Settings unendo .env, ambiente e override espliciti.

    Le variabili flat tipo ATS_IG_DEMO_API_KEY vengono mappate sui sotto-modelli:
    tenere il .env piatto e piu comodo per chi incolla le API una alla volta.
    """
    from dotenv import dotenv_values

    env: dict[str, str] = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        env.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    env.update({k: v for k, v in os.environ.items() if k.startswith("ATS_")})

    merged = _deep_merge(_collect_flat_overrides(env), overrides)
    return Settings(**merged)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = load_settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return settings


def reset_settings_cache() -> None:
    get_settings.cache_clear()
