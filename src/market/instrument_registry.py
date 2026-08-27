"""Instrument Registry (patch sez. 4): universo strumenti IG con EPIC come chiave.

- seed statico dei mercati principali (indici, FX, commodity, crypto, rates) con
  fattori di rischio (patch sez. 29) e simbolo pubblico di fallback;
- sync da IG: `get_market_details` popola min size, margin factor, spread, orari;
- `resolve()` trova l'EPIC da nome/alias (patch sez. 40: "trovare automaticamente un EPIC").
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from rapidfuzz import fuzz, process

from core.clock import utcnow
from core.db import session_scope
from core.enums import AssetClass, Factor, MarketStatus
from core.logging import get_logger
from core.repository import Repository
from core.schemas import Instrument

log = get_logger("market.registry")


@dataclass(frozen=True)
class SeedInstrument:
    epic: str
    name: str
    asset_class: AssetClass
    currency: str
    fallback_symbol: str | None
    aliases: tuple[str, ...]
    factors: dict[Factor, float]
    typical_spread: float
    value_per_point: float = 1.0
    margin_factor: float = 5.0
    min_size: float = 0.1
    size_step: float = 0.1
    commission_pct: float = 0.0
    search_terms: tuple[str, ...] = ()
    fallback_scale: float = 1.0  # moltiplicatore prezzo pubblico -> punti IG (es. EUR/USD 1.1659 -> 11659)


# EPIC IG CFD piu comuni (verificati a sync time: se l'epic non esiste sul conto,
# il resolver cerca per nome e sostituisce).
SEED_UNIVERSE: tuple[SeedInstrument, ...] = (
    SeedInstrument("IX.D.NASDAQ.IFE.IP", "US Tech 100", AssetClass.INDICES, "USD", "^NDX",
                   ("nasdaq", "nasdaq 100", "us tech", "ndx", "nq", "us tech 100 cash"),
                   {Factor.US_EQUITY: 1.0, Factor.RISK_ON: 0.9, Factor.RATES: -0.6}, 1.0, 1.0, 5.0, 0.1, 0.1, 0.0,
                   ("US Tech 100",)),
    SeedInstrument("IX.D.SPTRD.IFE.IP", "US 500", AssetClass.INDICES, "USD", "^GSPC",
                   ("s&p", "s&p 500", "spx", "sp500", "us 500", "es"),
                   {Factor.US_EQUITY: 1.0, Factor.RISK_ON: 0.8, Factor.RATES: -0.4}, 0.4, 1.0, 5.0, 0.1, 0.1, 0.0,
                   ("US 500",)),
    SeedInstrument("IX.D.DOW.IFE.IP", "Wall Street", AssetClass.INDICES, "USD", "^DJI",
                   ("dow", "dow jones", "djia", "wall street", "us 30"),
                   {Factor.US_EQUITY: 0.9, Factor.RISK_ON: 0.7, Factor.RATES: -0.3}, 2.4, 1.0, 5.0, 0.1, 0.1, 0.0,
                   ("Wall Street",)),
    SeedInstrument("IX.D.RUSSELL.IFE.IP", "US Russell 2000", AssetClass.INDICES, "USD", "^RUT",
                   ("russell", "russell 2000", "rty", "small caps"),
                   {Factor.US_EQUITY: 1.0, Factor.RISK_ON: 1.0, Factor.RATES: -0.7}, 0.3, 1.0, 5.0, 0.1, 0.1, 0.0,
                   ("US Russell 2000",)),
    SeedInstrument("IX.D.DAX.IFMM.IP", "Germany 40", AssetClass.INDICES, "EUR", "^GDAXI",
                   ("dax", "dax 40", "germany 40", "germany"),
                   {Factor.EU_EQUITY: 1.0, Factor.RISK_ON: 0.8, Factor.EUR: 0.2}, 1.2, 1.0, 5.0, 0.1, 0.1, 0.0,
                   ("Germany 40",)),
    SeedInstrument("IX.D.STXE.IFMM.IP", "EU Stocks 50", AssetClass.INDICES, "EUR", "^STOXX50E",
                   ("euro stoxx", "eurostoxx", "stoxx 50", "eu stocks 50", "sx5e"),
                   {Factor.EU_EQUITY: 1.0, Factor.RISK_ON: 0.8}, 1.5, 1.0, 5.0, 0.1, 0.1, 0.0,
                   ("EU Stocks 50",)),
    SeedInstrument("IX.D.FTSE.IFMM.IP", "FTSE 100", AssetClass.INDICES, "GBP", "^FTSE",
                   ("ftse", "ftse 100", "uk 100", "footsie"),
                   {Factor.EU_EQUITY: 0.8, Factor.RISK_ON: 0.6, Factor.GBP: -0.3, Factor.OIL: 0.2}, 1.0, 1.0, 5.0, 0.1, 0.1, 0.0,
                   ("FTSE 100",)),
    SeedInstrument("IX.D.NIKKEI.IFMM.IP", "Japan 225", AssetClass.INDICES, "JPY", "^N225",
                   ("nikkei", "nikkei 225", "japan 225", "nky"),
                   {Factor.ASIA_EQUITY: 1.0, Factor.RISK_ON: 0.7, Factor.JPY: -0.5}, 7.0, 1.0, 5.0, 0.1, 0.1, 0.0,
                   ("Japan 225",)),
    SeedInstrument("IX.D.VIX.IFMM.IP", "Volatility Index", AssetClass.INDICES, "USD", "^VIX",
                   ("vix", "volatility", "volatility index", "fear index"),
                   {Factor.VOLATILITY: 1.0, Factor.RISK_OFF: 1.0, Factor.US_EQUITY: -0.8}, 5.0, 1.0, 20.0, 0.1, 0.1, 0.0,
                   ("Volatility Index",), fallback_scale=100),
    SeedInstrument("CS.D.EURUSD.CFD.IP", "EUR/USD", AssetClass.FOREX, "USD", "EURUSD=X",
                   ("eurusd", "eur/usd", "euro dollar", "fiber"),
                   {Factor.EUR: 1.0, Factor.USD: -1.0}, 0.6, 1.0, 3.33, 0.1, 0.1, 0.0,
                   ("EUR/USD",), fallback_scale=10000),
    SeedInstrument("CS.D.GBPUSD.CFD.IP", "GBP/USD", AssetClass.FOREX, "USD", "GBPUSD=X",
                   ("gbpusd", "gbp/usd", "cable", "sterling"),
                   {Factor.GBP: 1.0, Factor.USD: -1.0, Factor.RISK_ON: 0.3}, 0.9, 1.0, 3.33, 0.1, 0.1, 0.0,
                   ("GBP/USD",), fallback_scale=10000),
    SeedInstrument("CS.D.USDJPY.CFD.IP", "USD/JPY", AssetClass.FOREX, "JPY", "JPY=X",
                   ("usdjpy", "usd/jpy", "dollar yen", "yen"),
                   {Factor.USD: 1.0, Factor.JPY: -1.0, Factor.RATES: 0.6, Factor.RISK_ON: 0.4}, 0.7, 1.0, 3.33, 0.1, 0.1, 0.0,
                   ("USD/JPY",), fallback_scale=100),
    SeedInstrument("CS.D.USDCHF.CFD.IP", "USD/CHF", AssetClass.FOREX, "CHF", "CHF=X",
                   ("usdchf", "usd/chf", "swissie"),
                   {Factor.USD: 1.0, Factor.RISK_ON: 0.3}, 1.5, 1.0, 3.33, 0.1, 0.1, 0.0,
                   ("USD/CHF",), fallback_scale=10000),
    SeedInstrument("CS.D.AUDUSD.CFD.IP", "AUD/USD", AssetClass.FOREX, "USD", "AUDUSD=X",
                   ("audusd", "aud/usd", "aussie"),
                   {Factor.USD: -1.0, Factor.RISK_ON: 0.6, Factor.ASIA_EQUITY: 0.3}, 0.6, 1.0, 3.33, 0.1, 0.1, 0.0,
                   ("AUD/USD",), fallback_scale=10000),
    SeedInstrument("CS.D.CFDGOLD.CFDGC.IP", "Spot Gold", AssetClass.COMMODITIES, "USD", "GC=F",
                   ("gold", "xauusd", "xau", "spot gold", "oro"),
                   {Factor.GOLD: 1.0, Factor.USD: -0.5, Factor.RATES: -0.5, Factor.RISK_OFF: 0.4}, 0.3, 1.0, 5.0, 0.1, 0.1, 0.0,
                   ("Spot Gold",)),
    SeedInstrument("CS.D.CFDSILVER.CFDSI.IP", "Spot Silver", AssetClass.COMMODITIES, "USD", "SI=F",
                   ("silver", "xagusd", "xag", "argento"),
                   {Factor.GOLD: 0.8, Factor.USD: -0.5, Factor.RISK_ON: 0.2}, 2.0, 1.0, 10.0, 0.1, 0.1, 0.0,
                   ("Spot Silver",), fallback_scale=100),
    SeedInstrument("CC.D.CL.UNC.IP", "Oil - US Crude", AssetClass.COMMODITIES, "USD", "CL=F",
                   ("oil", "crude", "wti", "us crude", "petrolio"),
                   {Factor.OIL: 1.0, Factor.RISK_ON: 0.3}, 2.8, 1.0, 10.0, 0.1, 0.1, 0.0,
                   ("Oil - US Crude",), fallback_scale=100),
    SeedInstrument("CC.D.LCO.UNC.IP", "Oil - Brent Crude", AssetClass.COMMODITIES, "USD", "BZ=F",
                   ("brent", "brent crude"),
                   {Factor.OIL: 1.0, Factor.RISK_ON: 0.3}, 2.8, 1.0, 10.0, 0.1, 0.1, 0.0,
                   ("Oil - Brent Crude",), fallback_scale=100),
    SeedInstrument("CC.D.NG.UNC.IP", "Natural Gas", AssetClass.COMMODITIES, "USD", "NG=F",
                   ("natgas", "natural gas", "gas naturale", "henry hub"),
                   {Factor.OIL: 0.3}, 3.0, 1.0, 10.0, 0.1, 0.1, 0.0,
                   ("Natural Gas",), fallback_scale=1000),
    SeedInstrument("CS.D.BITCOIN.CFD.IP", "Bitcoin", AssetClass.CRYPTO_CFD, "USD", "BTC-USD",
                   ("bitcoin", "btc", "btcusd", "xbt"),
                   {Factor.CRYPTO: 1.0, Factor.RISK_ON: 0.6, Factor.USD: -0.3}, 36.0, 1.0, 50.0, 0.01, 0.01, 0.0,
                   ("Bitcoin",)),
    SeedInstrument("CS.D.ETHUSD.CFD.IP", "Ether", AssetClass.CRYPTO_CFD, "USD", "ETH-USD",
                   ("ethereum", "eth", "ethusd", "ether"),
                   {Factor.CRYPTO: 1.0, Factor.RISK_ON: 0.6}, 2.0, 1.0, 50.0, 0.1, 0.1, 0.0,
                   ("Ether",)),
    SeedInstrument("IR.D.10YEAR100.FWM2.IP", "US Treasury Note 10Y", AssetClass.BONDS, "USD", "ZN=F",
                   ("treasury", "10y", "t-note", "us 10 year", "bonds", "tnote"),
                   {Factor.RATES: -1.0, Factor.RISK_OFF: 0.6, Factor.USD: 0.2}, 3.0, 1.0, 20.0, 0.1, 0.1, 0.0,
                   ("US Treasury Note 10Y", "US T-Note 10Y", "US Ultra Treasury Bond"), fallback_scale=100),
    SeedInstrument("IR.D.FGBL.FWM2.IP", "German Bund", AssetClass.BONDS, "EUR", None,
                   ("bund", "german bund", "bobl"),
                   {Factor.RATES: -1.0, Factor.RISK_OFF: 0.5, Factor.EUR: 0.2}, 2.0, 1.0, 20.0, 0.1, 0.1, 0.0,
                   ("German Bund", "Bund"), fallback_scale=100),
    SeedInstrument("CS.D.DOLLARINDEX.CFD.IP", "US Dollar Basket", AssetClass.FOREX, "USD", "DX-Y.NYB",
                   ("dxy", "dollar index", "usd index", "dollar basket", "us dollar basket"),
                   {Factor.USD: 1.0, Factor.RISK_OFF: 0.3}, 3.0, 1.0, 3.33, 0.1, 0.1, 0.0,
                   ("US Dollar Basket", "Dollar Basket"), fallback_scale=100),
)

# Mappa del fattore -> strumenti che lo esprimono (per cross-asset confirmation).
FACTOR_PROXIES: dict[Factor, tuple[str, ...]] = {
    Factor.US_EQUITY: ("IX.D.NASDAQ.IFE.IP", "IX.D.SPTRD.IFE.IP", "IX.D.DOW.IFE.IP"),
    Factor.EU_EQUITY: ("IX.D.DAX.IFMM.IP", "IX.D.STXE.IFMM.IP", "IX.D.FTSE.IFMM.IP"),
    Factor.USD: ("CS.D.DOLLARINDEX.CFD.IP", "CS.D.EURUSD.CFD.IP"),
    Factor.RATES: ("IR.D.10YEAR100.FWM2.IP",),
    Factor.GOLD: ("CS.D.CFDGOLD.CFDGC.IP",),
    Factor.OIL: ("CC.D.CL.UNC.IP", "CC.D.LCO.UNC.IP"),
    Factor.CRYPTO: ("CS.D.BITCOIN.CFD.IP", "CS.D.ETHUSD.CFD.IP"),
    Factor.VOLATILITY: ("IX.D.VIX.IFMM.IP",),
}


class InstrumentRegistry:
    """Registry in memoria con persistenza su DB e sync opzionale da IG."""

    def __init__(self, instruments: list[Instrument] | None = None):
        self._by_epic: dict[str, Instrument] = {}
        self._alias_index: dict[str, str] = {}
        for instrument in instruments or seed_instruments():
            self.add(instrument)

    # ---------------------------------------------------------------- basics
    def add(self, instrument: Instrument) -> None:
        self._by_epic[instrument.epic] = instrument
        self._alias_index[instrument.name.lower()] = instrument.epic
        self._alias_index[instrument.epic.lower()] = instrument.epic
        for alias in instrument.aliases:
            self._alias_index[alias.lower()] = instrument.epic

    def get(self, epic: str) -> Instrument | None:
        return self._by_epic.get(epic)

    def all(self) -> list[Instrument]:
        return list(self._by_epic.values())

    def by_asset_class(self, asset_class: AssetClass) -> list[Instrument]:
        return [i for i in self._by_epic.values() if i.asset_class == asset_class]

    def tradeable(self) -> list[Instrument]:
        return [i for i in self._by_epic.values() if i.tradeable]

    def names(self) -> list[str]:
        return [i.name for i in self._by_epic.values()]

    def resolve(self, query: str, *, min_score: float = 70.0) -> Instrument | None:
        """Nome/alias/EPIC -> Instrument, con fuzzy matching sugli alias."""
        if not query:
            return None
        key = query.strip().lower()
        if key in self._alias_index:
            return self._by_epic[self._alias_index[key]]
        candidates = list(self._alias_index.keys())
        best = process.extractOne(key, candidates, scorer=fuzz.WRatio)
        if best and best[1] >= min_score:
            return self._by_epic[self._alias_index[best[0]]]
        return None

    def resolve_many(self, queries: list[str]) -> dict[str, Instrument | None]:
        return {q: self.resolve(q) for q in queries}

    def proxies_for(self, factor: Factor) -> list[Instrument]:
        return [self._by_epic[e] for e in FACTOR_PROXIES.get(factor, ()) if e in self._by_epic]

    def factor_vector(self, epic: str) -> dict[Factor, float]:
        instrument = self.get(epic)
        return dict(instrument.factors) if instrument else {}

    def related(self, epic: str, *, min_overlap: float = 0.3) -> list[tuple[Instrument, float]]:
        """Strumenti correlati via prodotto scalare dei fattori (patch sez. 29)."""
        base = self.factor_vector(epic)
        out: list[tuple[Instrument, float]] = []
        for other in self._by_epic.values():
            if other.epic == epic:
                continue
            score = correlation_proxy(base, other.factors)
            if abs(score) >= min_overlap:
                out.append((other, score))
        out.sort(key=lambda pair: -abs(pair[1]))
        return out

    def update_status(self, epic: str, status: MarketStatus, **fields: Any) -> None:
        instrument = self._by_epic.get(epic)
        if instrument is None:
            return
        updated = instrument.model_copy(update={"market_status": status, **fields})
        self._by_epic[epic] = updated

    # --------------------------------------------------------------- persist
    async def load_from_db(self) -> int:
        seed_scale = {seed.epic: seed.fallback_scale for seed in SEED_UNIVERSE}
        async with session_scope() as session:
            rows = await Repository(session).list_instruments()
            for row in rows:
                raw = dict(row.raw or {})
                raw.setdefault("fallback_scale", seed_scale.get(row.epic, 1.0))
                self.add(
                    Instrument(
                        epic=row.epic,
                        name=row.name,
                        asset_class=AssetClass(row.asset_class),
                        currency=row.currency,
                        market_status=MarketStatus.parse(row.market_status),
                        min_size=row.min_size,
                        size_step=row.size_step,
                        lot_size=row.lot_size,
                        contract_size=row.contract_size,
                        value_per_point=row.value_per_point,
                        margin_factor=row.margin_factor,
                        spread=row.spread,
                        trading_hours=row.trading_hours or {},
                        controlled_risk_allowed=row.controlled_risk_allowed,
                        min_stop_distance=row.min_stop_distance,
                        max_stop_distance=row.max_stop_distance,
                        scaling_factor=row.scaling_factor,
                        expiry=row.expiry,
                        streaming_available=row.streaming_available,
                        aliases=list(row.aliases or []),
                        factors={Factor(k): float(v) for k, v in (row.factors or {}).items()},
                        fallback_symbol=row.fallback_symbol,
                        commission_pct=row.commission_pct,
                        overnight_funding_pct_annual=row.overnight_funding_pct_annual,
                        raw=raw,
                    )
                )
            return len(rows)

    async def save_to_db(self) -> int:
        async with session_scope() as session:
            repo = Repository(session)
            for instrument in self._by_epic.values():
                await repo.upsert_instrument(
                    instrument.epic,
                    name=instrument.name,
                    asset_class=instrument.asset_class.value,
                    currency=instrument.currency,
                    market_status=instrument.market_status.value,
                    min_size=instrument.min_size,
                    size_step=instrument.size_step,
                    lot_size=instrument.lot_size,
                    contract_size=instrument.contract_size,
                    value_per_point=instrument.value_per_point,
                    margin_factor=instrument.margin_factor,
                    spread=instrument.spread,
                    trading_hours=instrument.trading_hours,
                    controlled_risk_allowed=instrument.controlled_risk_allowed,
                    min_stop_distance=instrument.min_stop_distance,
                    max_stop_distance=instrument.max_stop_distance,
                    scaling_factor=instrument.scaling_factor,
                    expiry=instrument.expiry,
                    streaming_available=instrument.streaming_available,
                    aliases=instrument.aliases,
                    factors={k.value: v for k, v in instrument.factors.items()},
                    fallback_symbol=instrument.fallback_symbol,
                    commission_pct=instrument.commission_pct,
                    overnight_funding_pct_annual=instrument.overnight_funding_pct_annual,
                    last_synced_at=utcnow(),
                    raw=instrument.raw,
                )
        return len(self._by_epic)

    # -------------------------------------------------------------- IG sync
    async def sync_from_ig(
        self, client: Any, *, skip_recent_hours: float = 12.0, pace_s: float = 1.5, db_synced: dict[str, datetime] | None = None
    ) -> dict[str, Any]:
        """Aggiorna i dettagli da IG rispettando l'allowance del conto demo.

        - salta gli epic gia sincronizzati da meno di `skip_recent_hours` (persistiti a DB);
        - spaziatura `pace_s` fra le chiamate;
        - su 403 exceeded-allowance interrompe con grazia e tiene i valori a DB/seed.
        """
        report: dict[str, Any] = {"updated": [], "replaced": [], "missing": [], "skipped": [], "allowance_hit": False}
        recent = db_synced or {}
        cutoff = utcnow() - timedelta(hours=skip_recent_hours)
        for index, seed in enumerate(SEED_UNIVERSE):
            current_epic = seed.epic
            if current_epic in recent and recent[current_epic] and recent[current_epic] >= cutoff:
                report["skipped"].append(current_epic)
                continue
            if index:
                await asyncio.sleep(pace_s)
            instrument = self._by_epic.get(seed.epic)
            details = None
            try:
                details = await client.get_market_details(seed.epic)
            except Exception as exc:  # noqa: BLE001
                if _allowance_error(exc):
                    report["allowance_hit"] = True
                    log.warning("registry.allowance_exhausted", synced=len(report["updated"]))
                    break
                log.info("registry.epic_missing", epic=seed.epic, error=str(exc)[:100])
            if details is None and not report["allowance_hit"]:
                await asyncio.sleep(pace_s)
                try:
                    replacement = await self._search_replacement(client, seed)
                except Exception as exc:  # noqa: BLE001
                    if _allowance_error(exc):
                        report["allowance_hit"] = True
                        break
                    replacement = None
                if replacement is None:
                    report["missing"].append(seed.epic)
                    continue
                new_epic = str(replacement["epic"])
                await asyncio.sleep(pace_s)
                try:
                    details = await client.get_market_details(new_epic)
                except Exception as exc:  # noqa: BLE001
                    if _allowance_error(exc):
                        report["allowance_hit"] = True
                        break
                    report["missing"].append(seed.epic)
                    log.warning("registry.replacement_details_failed", epic=new_epic, error=str(exc)[:100])
                    continue
                self._by_epic.pop(seed.epic, None)
                instrument = (instrument or seed_to_instrument(seed)).model_copy(update={"epic": new_epic})
                report["replaced"].append({"from": seed.epic, "to": new_epic})
            if details is None:
                continue
            if instrument is None:
                instrument = seed_to_instrument(seed)
            updated = apply_ig_details(instrument, details)
            self.add(updated)
            report["updated"].append(updated.epic)
        await self.save_to_db()
        return report

    def epics(self) -> list[str]:
        return list(self._by_epic.keys())

    async def _search_replacement(self, client: Any, seed: SeedInstrument) -> dict[str, Any] | None:
        for term in seed.search_terms or (seed.name,):
            try:
                markets = await client.search_markets(term)
            except Exception:  # noqa: BLE001
                continue
            preferred = [
                m for m in markets
                if str(m.get("expiry", "-")) in ("-", "DFB")
                and _asset_type_matches(seed.asset_class, str(m.get("instrumentType", "")))
            ]
            pool = preferred or markets
            if not pool:
                continue
            best = process.extractOne(
                seed.name.lower(), [str(m.get("instrumentName", "")).lower() for m in pool], scorer=fuzz.WRatio
            )
            if best and best[1] >= 75:
                return pool[best[2]]
        return None


def _allowance_error(exc: Exception) -> bool:
    return "exceeded-api-key-allowance" in str(exc) or "exceeded-account-allowance" in str(exc)


def _asset_type_matches(asset_class: AssetClass, ig_type: str) -> bool:
    ig_type = ig_type.upper()
    mapping = {
        AssetClass.INDICES: ("INDICES",),
        AssetClass.FOREX: ("CURRENCIES",),
        AssetClass.COMMODITIES: ("COMMODITIES",),
        AssetClass.CRYPTO_CFD: ("CURRENCIES", "CRYPTOCURRENCIES", "COMMODITIES"),
        AssetClass.EQUITY_CFD: ("SHARES",),
        AssetClass.BONDS: ("RATES", "BONDS"),
        AssetClass.RATES: ("RATES",),
    }
    return not ig_type or ig_type in mapping.get(asset_class, (ig_type,))


def apply_ig_details(instrument: Instrument, details: dict[str, Any]) -> Instrument:
    """Applica GET /markets/{epic} v3 (instrument, dealingRules, snapshot)."""
    info = details.get("instrument") or {}
    rules = details.get("dealingRules") or {}
    snapshot = details.get("snapshot") or {}

    margin_factor = _num(info.get("marginFactor"), instrument.margin_factor)
    if str(info.get("marginFactorUnit", "PERCENTAGE")).upper() != "PERCENTAGE" and margin_factor:
        margin_factor = margin_factor * 100  # margin espresso come frazione
    bands = info.get("marginDepositBands") or []
    if bands and isinstance(bands, list):
        first = bands[0]
        margin_factor = _num(first.get("margin"), margin_factor)

    value_per_point = instrument.value_per_point
    vop = info.get("valueOfOnePip")
    if vop not in (None, ""):
        try:
            value_per_point = float(str(vop).replace(",", ""))
        except ValueError:
            pass
    contract_size = _num(info.get("contractSize"), instrument.contract_size)
    lot_size = _num(info.get("lotSize"), instrument.lot_size)

    currency = instrument.currency
    currencies = info.get("currencies") or []
    if currencies:
        default = next((c for c in currencies if c.get("isDefault")), currencies[0])
        currency = str(default.get("code") or currency)

    bid = _num(snapshot.get("bid"))
    offer = _num(snapshot.get("offer"))
    spread = offer - bid if bid is not None and offer is not None else instrument.spread
    min_stop = rules.get("minNormalStopOrLimitDistance") or {}
    max_stop = rules.get("maxStopOrLimitDistance") or {}
    min_deal = rules.get("minDealSize") or {}
    step = rules.get("minStepDistance") or {}

    return instrument.model_copy(
        update={
            "name": str(info.get("name") or instrument.name),
            "currency": currency,
            "market_status": MarketStatus.parse(snapshot.get("marketStatus")),
            "min_size": _num(min_deal.get("value"), instrument.min_size) or instrument.min_size,
            "size_step": _num(min_deal.get("value"), instrument.size_step) or instrument.size_step,
            "lot_size": lot_size or 1.0,
            "contract_size": contract_size or 1.0,
            "value_per_point": value_per_point or 1.0,
            "margin_factor": margin_factor or instrument.margin_factor,
            "spread": spread,
            "trading_hours": info.get("openingHours") or instrument.trading_hours,
            "controlled_risk_allowed": bool(info.get("controlledRiskAllowed", instrument.controlled_risk_allowed)),
            "min_stop_distance": _num(min_stop.get("value"), instrument.min_stop_distance),
            "min_stop_distance_unit": str(min_stop.get("unit") or "POINTS"),
            "max_stop_distance": _num(max_stop.get("value"), instrument.max_stop_distance),
            "scaling_factor": _num(snapshot.get("scalingFactor"), instrument.scaling_factor) or 1.0,
            "one_pip_means": info.get("onePipMeans"),
            "expiry": str(info.get("expiry") or instrument.expiry),
            "streaming_available": bool(info.get("streamingPricesAvailable", True)),
            "raw": {
                "instrument_type": info.get("type"),
                "market_id": info.get("marketId"),
                "unit": info.get("unit"),
                "min_step_distance": step,
                "market_order_preference": rules.get("marketOrderPreference"),
                "trailing_stops_preference": rules.get("trailingStopsPreference"),
                "controlled_risk_extra_spread": snapshot.get("controlledRiskExtraSpread"),
                "decimal_places_factor": snapshot.get("decimalPlacesFactor"),
                "update_time": snapshot.get("updateTimeUTC") or snapshot.get("updateTime"),
            },
        }
    )


def seed_to_instrument(seed: SeedInstrument) -> Instrument:
    return Instrument(
        epic=seed.epic,
        name=seed.name,
        asset_class=seed.asset_class,
        currency=seed.currency,
        min_size=seed.min_size,
        size_step=seed.size_step,
        value_per_point=seed.value_per_point,
        margin_factor=seed.margin_factor,
        spread=seed.typical_spread,
        aliases=list(seed.aliases),
        factors=dict(seed.factors),
        fallback_symbol=seed.fallback_symbol,
        commission_pct=seed.commission_pct,
        raw={"fallback_scale": seed.fallback_scale},
    )


def seed_instruments() -> list[Instrument]:
    return [seed_to_instrument(seed) for seed in SEED_UNIVERSE]


def correlation_proxy(a: dict[Factor, float], b: dict[Factor, float]) -> float:
    """Coseno fra vettori di fattori: stima strutturale della correlazione."""
    if not a or not b:
        return 0.0
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in set(a) | set(b))
    norm_a = sum(v * v for v in a.values()) ** 0.5
    norm_b = sum(v * v for v in b.values()) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def _num(value: Any, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_registry: InstrumentRegistry | None = None


def get_registry() -> InstrumentRegistry:
    global _registry
    if _registry is None:
        _registry = InstrumentRegistry()
    return _registry


def set_registry(registry: InstrumentRegistry) -> None:
    global _registry
    _registry = registry
