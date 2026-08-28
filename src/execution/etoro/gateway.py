"""Gateway eToro: ordini/posizioni/balances tipizzati sugli schemi esistenti.

epic = f"ETORO:{instrumentId}" — stesso pattern del precedente Limitless
(epic = f"LMTS:{slug}") per restare compatibile con Instrument/RiskEngine.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from core.bus import emit as default_emit
from core.enums import Direction, EventType
from core.schemas import AccountState, BrokerPosition, OrderResult

EmitFn = Callable[..., Awaitable[None]]


def etoro_epic(instrument_id: int) -> str:
    return f"ETORO:{instrument_id}"


def instrument_id_from_epic(epic: str) -> int:
    return int(epic.split(":", 1)[1])


class EtoroGateway:
    def __init__(self, *, client: Any, emit: EmitFn = default_emit):
        self.client = client
        self._emit = emit

    async def open_market_order(
        self,
        *,
        instrument_id: int,
        direction: Direction,
        units: float,
        stop_loss: float,
        take_profit: float,
        leverage: int,
    ) -> OrderResult:
        payload = {
            "action": "open",
            "transaction": "buy" if direction is Direction.BUY else "sell",
            "instrumentId": instrument_id,
            "orderType": "mkt",
            "leverage": leverage,
            "units": units,
            "stopLoss": stop_loss,
            "takeProfit": take_profit,
        }
        await self._emit(EventType.ORDER_SUBMITTED, {"epic": etoro_epic(instrument_id), "payload": payload}, source="etoro.gateway")
        raw = await self.client.post_order(payload)
        status = raw.get("status", "UNKNOWN")
        result = OrderResult(
            client_order_id=str(raw.get("orderId", "")),
            deal_id=raw.get("positionId"),
            status=status,
            filled_size=float(raw.get("units", 0.0)) if status == "EXECUTED" else 0.0,
            fill_price=raw.get("executionPrice"),
            requested_size=units,
            error=raw.get("reason"),
            raw=raw,
        )
        event_type = EventType.ORDER_CONFIRMED if status == "EXECUTED" else EventType.ORDER_REJECTED
        await self._emit(event_type, {"epic": etoro_epic(instrument_id), "result": raw}, source="etoro.gateway")
        return result

    async def close_position(self, *, position_id: str, instrument_id: int, units: float) -> dict[str, Any]:
        demo = not self.client.settings.execution_mode.uses_real_money
        base = "/api/v1/trading/execution/demo/market-close-orders/positions" if demo else "/api/v1/trading/execution/market-close-orders/positions"
        raw = await self.client.post(
            f"{base}/{position_id}",
            json={"InstrumentId": instrument_id, "UnitsToDeduct": units},
        )
        await self._emit(EventType.POSITION_CLOSED, {"positionId": position_id, "result": raw}, source="etoro.gateway")
        return raw

    def _pnl_path(self) -> str:
        demo = not self.client.settings.execution_mode.uses_real_money
        return "/api/v1/trading/info/demo/pnl" if demo else "/api/v1/trading/info/real/pnl"

    async def positions(self) -> list[BrokerPosition]:
        raw = await self.client.get(self._pnl_path())
        out: list[BrokerPosition] = []
        for p in raw.get("clientPortfolio", {}).get("positions", []):
            out.append(
                BrokerPosition(
                    deal_id=str(p["positionId"]),
                    epic=etoro_epic(int(p["instrumentId"])),
                    direction=Direction.BUY if p.get("isBuy", True) else Direction.SELL,
                    size=float(p["units"]),
                    level=float(p["openRate"]),
                    stop_level=p.get("stopLossRate"),
                    limit_level=p.get("takeProfitRate"),
                    currency="USD",
                    raw=p,
                )
            )
        return out

    async def balances(self) -> AccountState:
        # /api/v1/balances* copre solo i conti REALI finanziati: sul conto demo/practice
        # (credito virtuale, es. 100.000 USD di default eToro) il saldo vive dentro
        # clientPortfolio dello stesso endpoint gia' usato da positions() (verificato
        # 28/8 in produzione: /api/v1/balances tornava balances=[] anche con 100k demo).
        raw = await self.client.get(self._pnl_path())
        portfolio = raw.get("clientPortfolio", {})
        credit = float(portfolio.get("credit", 0.0))
        unrealized = float(portfolio.get("unrealizedPnL", 0.0))
        return AccountState(
            account_id="etoro",
            currency="USD",
            balance=credit,
            equity=credit + unrealized,
            available=credit,
            source="etoro-rest",
        )
