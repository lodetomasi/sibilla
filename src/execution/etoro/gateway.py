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
        # "sellShort" per aprire uno short, non "sell": verificato via doc ufficiale
        # create-an-order 28/8 - "sell"/"buyToCover" sono attualmente RIFIUTATI
        # dall'API eToro (riservati alla chiusura, non ancora abilitata su questo
        # endpoint - la chiusura reale passa da close_position/market-close-orders).
        payload = {
            "action": "open",
            "transaction": "buy" if direction is Direction.BUY else "sellShort",
            "instrumentId": instrument_id,
            "orderType": "mkt",
            # esplicito, non lasciato al default non documentato dell'API: tutto il
            # risk model (margin_factor, leva) in risk/etoro_adapter.py assuma CFD,
            # non acquisto azioni reali senza leva.
            "settlementType": "cfd",
            "leverage": leverage,
            "units": units,
            # "Rate", non "Loss"/"Profit" nudi: verificato via doc ufficiale eToro
            # (create-an-order) 28/8 - i nomi vecchi venivano ignorati in silenzio
            # dall'API, aprendo posizioni SENZA stop/target.
            "stopLossRate": stop_loss,
            "takeProfitRate": take_profit,
        }
        await self._emit(EventType.ORDER_SUBMITTED, {"epic": etoro_epic(instrument_id), "payload": payload}, source="etoro.gateway")
        raw = await self.client.post_order(payload)
        # La risposta sincrona di create-an-order (verificato via doc ufficiale) e'
        # SOLO {token, orderId, referenceId}: nessun campo status/positionId/
        # executionPrice/reason. "CONFIRMED" qui significa "il broker ha accettato
        # la richiesta", NON "eseguita" - il fill vero si scopre al prossimo poll
        # di positions() (stesso pattern del vecchio codice era una finzione: quei
        # campi non sono mai esistiti nella risposta reale).
        order_id = raw.get("orderId")
        accepted = order_id is not None
        result = OrderResult(
            client_order_id=str(order_id) if accepted else "",
            deal_id=None,
            status="CONFIRMED" if accepted else "REJECTED",
            filled_size=0.0,
            fill_price=None,
            requested_size=units,
            error=None if accepted else str(raw)[:200],
            raw=raw,
        )
        event_type = EventType.ORDER_CONFIRMED if accepted else EventType.ORDER_REJECTED
        await self._emit(event_type, {"epic": etoro_epic(instrument_id), "result": raw}, source="etoro.gateway")
        return result

    async def close_position(self, *, position_id: str, instrument_id: int, units: float) -> dict[str, Any]:
        demo = not self.client.settings.execution_mode.uses_real_money
        base = "/api/v1/trading/execution/demo/market-close-orders/positions" if demo else "/api/v1/trading/execution/market-close-orders/positions"
        # "InstrumentID" (ID maiuscolo), non "InstrumentId": verificato via doc
        # ufficiale close-demo-position-by-units 28/8, stesso pattern di casing
        # incoerente gia' visto in rates.py. UnitsToDeduct va OMESSO, non passato
        # esplicitamente alla size intera: eToro a volte rifiuta con errorCode 776
        # ("Calculated remaining live position amount in dollars: 0 < 1") quando lo
        # si specifica per una chiusura totale - verificato in produzione 28/8, la
        # stessa identica richiesta senza UnitsToDeduct chiude correttamente
        # (statusID Filled). L'unico chiamante (time_stop_close_all) chiude sempre
        # l'intera posizione, mai parzialmente.
        raw = await self.client.post(f"{base}/{position_id}", json={"InstrumentID": instrument_id})
        await self._emit(EventType.POSITION_CLOSED, {"positionId": position_id, "result": raw}, source="etoro.gateway")
        return raw

    def _pnl_path(self) -> str:
        demo = not self.client.settings.execution_mode.uses_real_money
        return "/api/v1/trading/info/demo/pnl" if demo else "/api/v1/trading/info/real/pnl"

    async def positions(self) -> list[BrokerPosition]:
        raw = await self.client.get(self._pnl_path())
        out: list[BrokerPosition] = []
        for p in raw.get("clientPortfolio", {}).get("positions", []):
            # "positionID"/"instrumentID" (ID maiuscolo): verificato sulla prima
            # posizione reale mai aperta (28/8) - stesso pattern di casing
            # incoerente gia' visto in rates.py e close_position.
            out.append(
                BrokerPosition(
                    deal_id=str(p["positionID"]),
                    epic=etoro_epic(int(p["instrumentID"])),
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
