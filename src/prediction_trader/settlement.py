import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from prediction_trader.adapters.polymarket import GammaClient
from prediction_trader.domain.models import Outcome
from prediction_trader.persistence import (
    MarketRepository,
    market_settlements,
    markets,
    replay_fill_settlements,
    replay_fills,
)


@dataclass(frozen=True, slots=True)
class MarketSettlement:
    market_id: str
    winning_outcome: Outcome
    resolved_at: datetime
    raw: Mapping[str, Any]


def _json_array(value: object) -> list[object] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, list) else None


def parse_gamma_settlement(payload: object) -> MarketSettlement | None:
    if not isinstance(payload, dict) or payload.get("closed") is not True:
        return None
    outcomes = _json_array(payload.get("outcomes"))
    prices = _json_array(payload.get("outcomePrices"))
    if outcomes is None or prices is None or len(outcomes) != 2 or len(prices) != 2:
        return None
    if any(not isinstance(outcome, str) for outcome in outcomes):
        return None
    labels = [str(outcome).strip().upper() for outcome in outcomes]
    if set(labels) not in ({"YES", "NO"}, {"UP", "DOWN"}):
        return None
    try:
        resolved_prices = [Decimal(str(price)) for price in prices]
    except (InvalidOperation, ValueError):
        return None
    if any(not price.is_finite() for price in resolved_prices):
        return None
    if set(resolved_prices) != {Decimal("0"), Decimal("1")}:
        return None
    winning_label = labels[resolved_prices.index(Decimal("1"))]
    winning_outcome = Outcome.YES if winning_label in {"YES", "UP"} else Outcome.NO
    market_id = str(payload.get("id", ""))
    if not market_id:
        return None
    raw_resolved_at = payload.get("closedTime") or payload.get("updatedAt")
    try:
        resolved_at = datetime.fromisoformat(str(raw_resolved_at).replace("Z", "+00:00"))
    except ValueError:
        resolved_at = datetime.now(UTC)
    return MarketSettlement(
        market_id,
        winning_outcome,
        resolved_at.astimezone(UTC),
        payload,
    )


@dataclass(frozen=True, slots=True)
class SettlementSyncResult:
    checked_markets: int
    resolved_markets: int
    newly_settled_fills: int


class SettlementSynchronizer:
    def __init__(self, repository: MarketRepository, client: GammaClient) -> None:
        self._repository = repository
        self._client = client

    def sync(self) -> SettlementSyncResult:
        now = datetime.now(UTC)
        with self._repository.engine.connect() as connection:
            market_ids: Sequence[str] = (
                connection.execute(
                    select(markets.c.id)
                    .outerjoin(
                        market_settlements,
                        market_settlements.c.market_id == markets.c.id,
                    )
                    .where(
                        market_settlements.c.market_id.is_(None),
                        markets.c.end_date <= now,
                        markets.c.end_date >= now - timedelta(days=7),
                    )
                    .order_by(markets.c.end_date.desc(), markets.c.id)
                    .limit(200)
                )
                .scalars()
                .all()
            )
        resolved: list[tuple[MarketSettlement, datetime]] = []
        for market_id in market_ids:
            settlement = parse_gamma_settlement(self._client.get_market(str(market_id)))
            if settlement is not None and settlement.market_id == str(market_id):
                resolved.append((settlement, datetime.now(UTC)))

        newly_settled = 0
        with self._repository.engine.begin() as connection:
            for settlement, observed_at in resolved:
                statement = insert(market_settlements).values(
                    market_id=settlement.market_id,
                    winning_outcome=settlement.winning_outcome.value,
                    resolved_at=settlement.resolved_at,
                    observed_at=observed_at,
                    source="polymarket_gamma",
                    raw=json.loads(json.dumps(settlement.raw)),
                )
                connection.execute(statement.on_conflict_do_nothing())
            # Also settle replay fills created after a market's outcome was saved.
            fills = connection.execute(
                select(
                    replay_fills,
                    market_settlements.c.winning_outcome,
                    market_settlements.c.resolved_at,
                )
                .join(
                    market_settlements, market_settlements.c.market_id == replay_fills.c.market_id
                )
                .outerjoin(
                    replay_fill_settlements,
                    replay_fill_settlements.c.fill_id == replay_fills.c.id,
                )
                .where(replay_fill_settlements.c.fill_id.is_(None))
                .order_by(replay_fills.c.id)
                .limit(5000)
            ).mappings()
            for fill in fills:
                winning_outcome = str(fill["winning_outcome"])
                won = str(fill["outcome"]) == winning_outcome
                shares = Decimal(fill["shares"])
                payout = shares if won else Decimal("0")
                realized_pnl = payout - Decimal(fill["notional_usd"])
                fill_statement = insert(replay_fill_settlements).values(
                    fill_id=fill["id"],
                    run_id=fill["run_id"],
                    market_id=fill["market_id"],
                    winning_outcome=winning_outcome,
                    won=won,
                    payout_usd=payout,
                    realized_pnl_usd=realized_pnl,
                    settled_at=fill["resolved_at"],
                )
                # psycopg versions may return rowcount=-1 for successful inserts.
                # RETURNING reflects actual inserted rows even under concurrent sync.
                inserted_id = connection.execute(
                    fill_statement.on_conflict_do_nothing().returning(
                        replay_fill_settlements.c.fill_id
                    )
                ).scalar_one_or_none()
                newly_settled += int(inserted_id is not None)
        return SettlementSyncResult(len(market_ids), len(resolved), newly_settled)
