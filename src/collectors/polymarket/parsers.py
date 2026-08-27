"""Normalizzazione payload Polymarket -> schemi interni.

Le API Polymarket restituiscono spesso JSON-in-stringa (outcomes, outcomePrices,
clobTokenIds) e nomi di campo variabili fra endpoint: qui c'e' l'unico punto in
cui questa irregolarita viene assorbita.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from core.clock import utcnow
from core.enums import Category
from core.pricing import PriceConvention
from core.schemas import BookLevel, MarketSnapshot, OrderBook
from market.categorization import classify

VENUE = "polymarket"


def parse_json_field(value: Any) -> Any:
    """`outcomes` e simili arrivano come stringa JSON."""
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return [part.strip() for part in text.split(",") if part.strip()]
    return value


def parse_ts(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # millisecondi
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return parse_ts(int(text))
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _f(value: Any, default: float | None = None) -> float | None:
    if value in (None, "", "null"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def extract_tags(raw: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for key in ("tags", "categories", "eventTags"):
        value = parse_json_field(raw.get(key))
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    tags.append(item)
                elif isinstance(item, dict):
                    label = item.get("slug") or item.get("label") or item.get("name")
                    if label:
                        tags.append(str(label))
    for key in ("category", "subcategory", "groupItemTitle"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            tags.append(value)
    event = raw.get("events")
    if isinstance(event, list):
        for item in event:
            if isinstance(item, dict):
                tags.extend(extract_tags(item))
    return tags


def parse_market(raw: dict[str, Any]) -> dict[str, Any]:
    """Metadati mercato normalizzati per la tabella `markets` (sez. 4.1)."""
    outcomes = parse_json_field(raw.get("outcomes")) or ["Yes", "No"]
    prices = parse_json_field(raw.get("outcomePrices")) or []
    token_ids = parse_json_field(raw.get("clobTokenIds")) or []
    question = raw.get("question") or raw.get("title") or raw.get("groupItemTitle") or ""
    tags = extract_tags(raw)
    category = classify(f"{question} {raw.get('description', '')}", tags)

    outcome_objects: list[dict[str, Any]] = []
    for index, name in enumerate(outcomes if isinstance(outcomes, list) else []):
        outcome_objects.append(
            {
                "name": str(name),
                "token_id": str(token_ids[index]) if index < len(token_ids) else None,
                "price": _f(prices[index]) if index < len(prices) else None,
            }
        )

    external_id = str(
        raw.get("conditionId") or raw.get("condition_id") or raw.get("id") or raw.get("slug") or ""
    )
    status = "CLOSED" if raw.get("closed") else ("OPEN" if raw.get("active", True) else "INACTIVE")
    if raw.get("archived"):
        status = "ARCHIVED"

    return {
        "venue": VENUE,
        "external_id": external_id,
        "slug": raw.get("slug"),
        "question": question,
        "outcomes": outcome_objects,
        "category": category.value,
        "status": status,
        "tradable": False,  # sez. 4.1: Polymarket e' intelligence source
        "liquidity": _f(raw.get("liquidityNum") or raw.get("liquidity"), 0.0),
        "volume": _f(raw.get("volumeNum") or raw.get("volume"), 0.0),
        "created_date": parse_ts(raw.get("createdAt") or raw.get("startDate")),
        "resolution_date": parse_ts(raw.get("endDate") or raw.get("resolutionDate")),
        "resolution_source": raw.get("resolutionSource") or raw.get("umaResolutionSource"),
        "resolved_outcome": _resolved_outcome(raw, outcome_objects),
        "settlement_rules": {
            "description": (raw.get("description") or "")[:4000],
            "resolution_source": raw.get("resolutionSource"),
            "uma_status": raw.get("umaResolutionStatus"),
            "end_date": (parse_ts(raw.get("endDate")) or "") and parse_ts(raw.get("endDate")).isoformat(),  # type: ignore[union-attr]
            "negRisk": raw.get("negRisk"),
        },
        "raw": {
            "id": raw.get("id"),
            "conditionId": raw.get("conditionId"),
            "questionID": raw.get("questionID"),
            "clobTokenIds": token_ids,
            "tags": tags,
            "spread": _f(raw.get("spread")),
            "bestBid": _f(raw.get("bestBid")),
            "bestAsk": _f(raw.get("bestAsk")),
            "lastTradePrice": _f(raw.get("lastTradePrice")),
            "volume24hr": _f(raw.get("volume24hr")),
            "oneDayPriceChange": _f(raw.get("oneDayPriceChange")),
            "acceptingOrders": raw.get("acceptingOrders"),
            "closed": raw.get("closed"),
            "active": raw.get("active"),
            "eventSlug": _event_slug(raw),
            "eventTitle": _event_title(raw),
        },
    }


def _resolved_outcome(raw: dict[str, Any], outcomes: list[dict[str, Any]]) -> str | None:
    if not raw.get("closed"):
        return None
    explicit = raw.get("resolvedOutcome") or raw.get("winningOutcome")
    if explicit:
        return str(explicit)
    # a mercato chiuso il prezzo dell'esito vincente e' ~1
    for outcome in outcomes:
        price = outcome.get("price")
        if price is not None and price >= 0.99:
            return str(outcome["name"])
    return None


def _event_slug(raw: dict[str, Any]) -> str | None:
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return events[0].get("slug")
    return raw.get("eventSlug") or raw.get("slug")


def _event_title(raw: dict[str, Any]) -> str | None:
    events = raw.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return events[0].get("title")
    return raw.get("eventTitle") or raw.get("question")


def market_snapshot(raw: dict[str, Any], *, outcome_index: int = 0) -> MarketSnapshot:
    """Snapshot prezzo/liquidita a partire dai metadati Gamma."""
    parsed = parse_market(raw)
    outcomes = parsed["outcomes"]
    outcome = outcomes[outcome_index] if outcome_index < len(outcomes) else {"name": "Yes"}
    best_bid = _f(raw.get("bestBid"))
    best_ask = _f(raw.get("bestAsk"))
    price = outcome.get("price")
    if price is None:
        price = _f(raw.get("lastTradePrice"))
    spread = _f(raw.get("spread"))
    if spread is None and best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
    mid = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2
    return MarketSnapshot(
        venue=VENUE,
        market_id=parsed["external_id"],
        question=parsed["question"],
        outcome=str(outcome.get("name", "Yes")),
        category=Category(parsed["category"]),
        ts=utcnow(),
        price=price,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        mid=mid,
        liquidity=parsed["liquidity"],
        volume=parsed["volume"],
        status=parsed["status"],
        suspended=not bool(raw.get("acceptingOrders", True)),
        raw={"token_id": outcome.get("token_id")},
    )


def parse_book(raw: dict[str, Any], *, market_id: str | None = None, outcome: str = "YES") -> OrderBook:
    """Book CLOB -> OrderBook (bids desc, asks asc)."""
    bids = [
        BookLevel(price=float(level["price"]), size=float(level["size"]))
        for level in raw.get("bids", []) or []
        if _f(level.get("price")) is not None and _f(level.get("size")) is not None
    ]
    asks = [
        BookLevel(price=float(level["price"]), size=float(level["size"]))
        for level in raw.get("asks", []) or []
        if _f(level.get("price")) is not None and _f(level.get("size")) is not None
    ]
    bids.sort(key=lambda level: level.price, reverse=True)
    asks.sort(key=lambda level: level.price)
    return OrderBook(
        venue=VENUE,
        market_id=str(market_id or raw.get("market") or raw.get("asset_id") or ""),
        outcome=outcome,
        ts=parse_ts(raw.get("timestamp")) or utcnow(),
        bids=bids,
        asks=asks,
        status="OPEN",
        price_convention=PriceConvention.PROBABILITY,
    )


def parse_wallet_trade(raw: dict[str, Any], *, address: str | None = None) -> dict[str, Any]:
    """Trade wallet -> riga `wallet_trades`."""
    wallet = str(
        address
        or raw.get("proxyWallet")
        or raw.get("user")
        or raw.get("maker")
        or raw.get("wallet")
        or ""
    ).lower()
    price = _f(raw.get("price"), 0.0) or 0.0
    size = _f(raw.get("size") or raw.get("shares"), 0.0) or 0.0
    usd = _f(raw.get("usdcSize") or raw.get("usdSize"))
    if usd is None:
        usd = price * size
    question = raw.get("title") or raw.get("question") or raw.get("market", {}).get("question") if isinstance(raw.get("market"), dict) else raw.get("title")
    side = str(raw.get("side") or raw.get("type") or "BUY").upper()
    if side in ("TRADE", ""):
        side = "BUY"
    external_id = str(
        raw.get("transactionHash")
        or raw.get("id")
        or raw.get("tradeId")
        or f"{wallet}:{raw.get('asset') or raw.get('asset_id')}:{raw.get('timestamp')}"
    )
    tags = extract_tags(raw)
    category = classify(str(question or ""), tags)
    return {
        "wallet_address": wallet,
        "venue": VENUE,
        "external_id": external_id,
        "market_external_id": str(raw.get("conditionId") or raw.get("market") or "") or None,
        "condition_id": str(raw.get("conditionId") or "") or None,
        "asset_id": str(raw.get("asset") or raw.get("asset_id") or "") or None,
        "market_question": question,
        "category": category.value,
        "outcome": str(raw.get("outcome") or raw.get("outcomeIndex") or "") or None,
        "side": side,
        "price": price,
        "size": size,
        "usd_size": usd,
        "ts": parse_ts(raw.get("timestamp") or raw.get("matchTime") or raw.get("createdAt")) or utcnow(),
        "raw": {
            k: raw.get(k)
            for k in ("slug", "eventSlug", "outcomeIndex", "transactionHash", "type", "bio")
            if k in raw
        },
    }


def parse_wallet_position(raw: dict[str, Any], *, address: str | None = None) -> dict[str, Any]:
    wallet = str(address or raw.get("proxyWallet") or raw.get("user") or "").lower()
    return {
        "wallet_address": wallet,
        "asset_id": str(raw.get("asset") or raw.get("asset_id") or raw.get("tokenId") or ""),
        "condition_id": str(raw.get("conditionId") or "") or None,
        "market_question": raw.get("title") or raw.get("question"),
        "outcome": str(raw.get("outcome") or "") or None,
        "size": _f(raw.get("size"), 0.0) or 0.0,
        "avg_price": _f(raw.get("avgPrice")),
        "current_price": _f(raw.get("curPrice") or raw.get("currentPrice")),
        "unrealized_pnl": _f(raw.get("cashPnl") or raw.get("unrealizedPnl")),
        "realized_pnl": _f(raw.get("realizedPnl")),
        "raw": {
            k: raw.get(k)
            for k in ("initialValue", "currentValue", "percentPnl", "redeemable", "slug")
            if k in raw
        },
    }


def parse_price_history(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for point in points or []:
        ts = parse_ts(point.get("t") or point.get("timestamp"))
        price = _f(point.get("p") or point.get("price"))
        if ts is None or price is None:
            continue
        out.append({"ts": ts, "price": price})
    out.sort(key=lambda item: item["ts"])
    return out


def parse_holder(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "address": str(raw.get("proxyWallet") or raw.get("user") or raw.get("wallet") or "").lower(),
        "label": raw.get("name") or raw.get("pseudonym"),
        "size": _f(raw.get("amount") or raw.get("size"), 0.0) or 0.0,
        "outcome": raw.get("outcomeIndex"),
    }


def parse_leaderboard_entry(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "address": str(raw.get("proxyWallet") or raw.get("wallet") or raw.get("user") or "").lower(),
        "label": raw.get("name") or raw.get("pseudonym"),
        "pnl": _f(raw.get("pnl") or raw.get("profit") or raw.get("amount"), 0.0) or 0.0,
        "volume": _f(raw.get("volume") or raw.get("vol"), 0.0) or 0.0,
    }
