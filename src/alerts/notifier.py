"""Alert (sez. 51): trade executed/rejected, opportunita, daily loss, API failure, kill switch, P&L.

Canali: log + DB sempre; Telegram/Slack se configurati.
"""
from __future__ import annotations

from typing import Any

import httpx

from core.bus import BusEvent, EventBus
from core.config import AlertConfig, get_settings
from core.db import session_scope
from core.enums import EventType
from core.logging import get_logger
from core.repository import Repository

log = get_logger("alerts")

SEVERITY = {
    EventType.KILL_SWITCH_TRIGGERED: "CRITICAL",
    EventType.ORDER_REJECTED: "WARNING",
    EventType.POSITION_OPENED: "INFO",
    EventType.POSITION_CLOSED: "INFO",
    EventType.TRADE_APPROVED: "INFO",
    EventType.TRADE_REJECTED: "INFO",
    EventType.THESIS_INVALIDATED: "WARNING",
    EventType.ALERT: "WARNING",
}


class Notifier:
    def __init__(self, config: AlertConfig | None = None, *, http: httpx.AsyncClient | None = None):
        self.config = config or get_settings().alerts
        self._http = http or httpx.AsyncClient(timeout=10.0)

    def channels(self) -> list[str]:
        channels = ["log", "db"]
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            channels.append("telegram")
        if self.config.slack_webhook_url:
            channels.append("slack")
        return channels

    async def notify(self, kind: str, title: str, message: str = "", *, severity: str = "INFO", details: dict[str, Any] | None = None) -> None:
        log.info("alert", kind=kind, severity=severity, title=title)
        delivered = False
        try:
            if "telegram" in self.channels():
                delivered |= await self._telegram(f"[{severity}] {title}\n{message}")
            if "slack" in self.channels():
                delivered |= await self._slack(f"*[{severity}] {title}*\n{message}")
        except Exception as exc:  # noqa: BLE001
            log.warning("alert.delivery_failed", error=str(exc)[:120])
        try:
            async with session_scope() as session:
                await Repository(session).add_alert(kind=kind, severity=severity, title=title, message=message, channels=self.channels(), delivered=delivered, details=details or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("alert.persist_failed", error=str(exc)[:120])

    async def _telegram(self, text: str) -> bool:
        assert self.config.telegram_bot_token and self.config.telegram_chat_id
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token.get_secret_value()}/sendMessage"
        response = await self._http.post(url, json={"chat_id": self.config.telegram_chat_id, "text": text[:4000]})
        return response.status_code < 400

    async def _slack(self, text: str) -> bool:
        assert self.config.slack_webhook_url
        response = await self._http.post(self.config.slack_webhook_url.get_secret_value(), json={"text": text[:4000]})
        return response.status_code < 400

    def attach(self, bus: EventBus) -> None:
        for event_type in SEVERITY:
            bus.subscribe(event_type, self._on_event)

    async def _on_event(self, event: BusEvent) -> None:
        severity = SEVERITY.get(event.type, "INFO")
        payload = event.payload
        title = {
            EventType.KILL_SWITCH_TRIGGERED: f"KILL SWITCH: {payload.get('reason')}",
            EventType.ORDER_REJECTED: f"Ordine rifiutato {payload.get('trade_id', '')}",
            EventType.POSITION_OPENED: f"Trade eseguito {payload.get('direction')} {payload.get('epic')} size {payload.get('size')}",
            EventType.POSITION_CLOSED: f"Posizione chiusa {payload.get('epic')} P&L {payload.get('pnl')} ({payload.get('reason')})",
            EventType.TRADE_APPROVED: f"Trade approvato {payload.get('epic')}",
            EventType.TRADE_REJECTED: f"Trade rifiutato dal Risk Engine {payload.get('epic', '')}",
            EventType.THESIS_INVALIDATED: f"Tesi invalidata {payload.get('epic')}: {payload.get('reason')}",
            EventType.ALERT: str(payload.get("title", "alert")),
        }.get(event.type, event.type.value)
        await self.notify(event.type.value, title, str(payload)[:1500], severity=severity, details=payload)

    async def aclose(self) -> None:
        await self._http.aclose()
