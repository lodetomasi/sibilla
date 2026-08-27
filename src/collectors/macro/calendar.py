"""Macro release collector (patch sez. 31.D).

Fonti reali:
  - BLS RSS/press (CPI, NFP...) e Fed press: rilevazione dell'uscita dal news feed (Tier 1);
  - FRED API (opzionale, con chiave) per actual/previous di serie chiave;
  - calendario programmato: file JSON `data/macro_calendar.json` (indicatore, ora,
    consensus) mantenuto dall'operatore, piu URL opzionale ATS_MACRO_CALENDAR_URL.
Il consensus non e' disponibile gratuitamente in modo affidabile: se assente il
sistema calcola la sorpresa vs `previous` e lo dichiara nel record.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from collectors.base import BaseCollector, CollectionMode
from core.bus import emit
from core.clock import utcnow
from core.config import DATA_DIR, get_settings
from core.db import session_scope
from core.enums import EventType, MacroIndicator
from core.logging import get_logger
from core.repository import Repository
from core.schemas import MacroRelease

log = get_logger("collectors.macro")

CALENDAR_FILE = DATA_DIR / "macro_calendar.json"

FRED_SERIES: dict[MacroIndicator, str] = {
    MacroIndicator.CPI: "CPIAUCSL",
    MacroIndicator.CORE_CPI: "CPILFESL",
    MacroIndicator.PCE: "PCEPILFE",
    MacroIndicator.NFP: "PAYEMS",
    MacroIndicator.UNEMPLOYMENT: "UNRATE",
    MacroIndicator.GDP: "A191RL1Q225SBEA",
    MacroIndicator.RETAIL_SALES: "RSAFS",
}

# pattern sui titoli Tier-1 per riconoscere una release e il valore
_HEADLINE_PATTERNS: tuple[tuple[MacroIndicator, re.Pattern[str]], ...] = (
    (MacroIndicator.CPI, re.compile(r"consumer price index.*?(?:rose|increased|fell|declined|unchanged)\s*([\-\d\.]+)?\s*percent", re.I)),
    (MacroIndicator.NFP, re.compile(r"payroll employment.*?(rose|increased|fell|declined|changed little)\s*(?:by\s*)?([\d,]+)?", re.I)),
    (MacroIndicator.UNEMPLOYMENT, re.compile(r"unemployment rate.*?(?:at|to|was|held at|unchanged at)\s*([\d\.]+)\s*percent", re.I)),
    (MacroIndicator.PPI, re.compile(r"producer price index.*?(?:rose|increased|fell|declined)\s*([\-\d\.]+)?\s*percent", re.I)) if hasattr(MacroIndicator, "PPI") else (MacroIndicator.OTHER, re.compile(r"producer price index", re.I)),
    (MacroIndicator.FOMC, re.compile(r"federal open market committee|fomc statement|federal funds rate", re.I)),
    (MacroIndicator.ECB, re.compile(r"monetary policy decisions|ecb.*?interest rate", re.I)),
    (MacroIndicator.GDP, re.compile(r"gross domestic product.*?(?:increased|rose|decreased|fell).*?([\d\.]+)\s*percent", re.I)),
)


class MacroCalendarCollector(BaseCollector):
    name = "macro_calendar"

    def __init__(self, *, http: httpx.AsyncClient | None = None):
        super().__init__()
        settings = get_settings()
        self.config = settings.macro
        self._http = http or httpx.AsyncClient(timeout=15.0, headers={"User-Agent": settings.news.user_agent})
        self._own_http = http is None

    async def collect(self, mode: CollectionMode = CollectionMode.INCREMENTAL, **kwargs: Any) -> int:
        count = 0
        count += await self._load_scheduled()
        if self.config.fred_api_key:
            count += await self._refresh_from_fred()
        count += await self._emit_due_releases()
        self.stats.watermark = utcnow()
        return count

    # ------------------------------------------------------------- calendar
    async def _load_scheduled(self) -> int:
        entries: list[dict[str, Any]] = []
        if CALENDAR_FILE.exists():
            try:
                entries.extend(json.loads(CALENDAR_FILE.read_text()))
            except json.JSONDecodeError as exc:
                self.log.warning("macro.calendar.invalid_json", error=str(exc)[:120])
        if self.config.calendar_url:
            try:
                response = await self._http.get(self.config.calendar_url)
                if response.status_code < 400:
                    payload = response.json()
                    entries.extend(payload if isinstance(payload, list) else payload.get("events", []))
            except Exception as exc:  # noqa: BLE001
                self.log.warning("macro.calendar.url_failed", error=str(exc)[:120])
        if not entries:
            return 0
        stored = 0
        async with session_scope() as session:
            repo = Repository(session)
            for entry in entries:
                release = parse_calendar_entry(entry)
                if release is None:
                    continue
                await repo.upsert_macro_release(
                    indicator=release.indicator.value, name=release.name, country=release.country, release_time=release.release_time,
                    actual=release.actual, consensus=release.consensus, previous=release.previous, unit=release.unit,
                    source=release.source or "calendar", url=release.url,
                )
                stored += 1
        return stored

    async def _refresh_from_fred(self) -> int:
        assert self.config.fred_api_key
        key = self.config.fred_api_key.get_secret_value()
        updated = 0
        async with session_scope() as session:
            repo = Repository(session)
            for indicator, series in FRED_SERIES.items():
                try:
                    response = await self._http.get(
                        "https://api.stlouisfed.org/fred/series/observations",
                        params={"series_id": series, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 3},
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("macro.fred.failed", series=series, error=str(exc)[:100])
                    continue
                if response.status_code >= 400:
                    continue
                observations = [o for o in response.json().get("observations", []) if o.get("value") not in (".", None)]
                if len(observations) < 2:
                    continue
                latest, previous = observations[0], observations[1]
                actual, prev = _to_float(latest["value"]), _to_float(previous["value"])
                if indicator in (MacroIndicator.CPI, MacroIndicator.CORE_CPI, MacroIndicator.PCE, MacroIndicator.RETAIL_SALES) and actual and prev:
                    # indice -> variazione % mensile
                    actual, prev = (actual / prev - 1) * 100, None
                for row in await repo.upcoming_macro_releases(hours=24 * 45):
                    if row.indicator == indicator.value and row.actual is None and row.release_time <= utcnow():
                        row.actual = actual
                        row.previous = row.previous if row.previous is not None else prev
                        row.source = f"FRED:{series}"
                        updated += 1
        return updated

    async def _emit_due_releases(self) -> int:
        emitted = 0
        async with session_scope() as session:
            repo = Repository(session)
            for row in await repo.unprocessed_macro_releases():
                if row.release_time > utcnow():
                    continue
                release = MacroRelease(
                    indicator=MacroIndicator(row.indicator), name=row.name, country=row.country, release_time=row.release_time,
                    actual=row.actual, consensus=row.consensus, previous=row.previous, unit=row.unit or "", source=row.source or "", url=row.url,
                )
                await emit(EventType.MACRO_RELEASE, release.model_dump(mode="json"), source=self.name)
                row.processed = True
                emitted += 1
        return emitted

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()


def parse_calendar_entry(entry: dict[str, Any]) -> MacroRelease | None:
    try:
        indicator = MacroIndicator(str(entry.get("indicator", "OTHER")).upper())
    except ValueError:
        indicator = MacroIndicator.OTHER
    ts = entry.get("release_time") or entry.get("time")
    if not ts:
        return None
    try:
        release_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if release_time.tzinfo is None:
        release_time = release_time.replace(tzinfo=UTC)
    return MacroRelease(
        indicator=indicator, name=str(entry.get("name") or indicator.value), country=str(entry.get("country") or "US"),
        release_time=release_time, actual=_to_float(entry.get("actual")), consensus=_to_float(entry.get("consensus")),
        previous=_to_float(entry.get("previous")), unit=str(entry.get("unit") or ""), source=str(entry.get("source") or "calendar"),
        url=entry.get("url"),
    )


def release_from_headline(title: str, *, published_at: datetime | None, source: str, url: str | None) -> MacroRelease | None:
    """Riconosce una release macro da un titolo Tier-1 (es. BLS) ed estrae il valore se presente."""
    for indicator, pattern in _HEADLINE_PATTERNS:
        match = pattern.search(title)
        if not match:
            continue
        actual = None
        for group in match.groups() or ():
            value = _to_float(str(group).replace(",", "")) if group else None
            if value is not None:
                actual = value
                break
        if actual is not None and re.search(r"\b(fell|declined|decreased)\b", title, re.I) and actual > 0:
            actual = -actual
        return MacroRelease(indicator=indicator, name=title[:200], release_time=published_at or utcnow(), actual=actual, source=source, url=url)
    return None


def upcoming_window(rows: list[Any], *, minutes: int = 30) -> list[Any]:
    now = utcnow()
    return [r for r in rows if now <= r.release_time <= now + timedelta(minutes=minutes)]


def _to_float(value: Any) -> float | None:
    if value in (None, "", "."):
        return None
    try:
        return float(str(value).replace("%", "").replace(",", ""))
    except ValueError:
        return None
