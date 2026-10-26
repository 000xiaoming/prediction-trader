from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class Outcome(StrEnum):
    YES = "YES"
    NO = "NO"


class StrategyGroup(StrEnum):
    CROSS_VENUE = "cross_venue"
    MICROSTRUCTURE = "microstructure"
    WALLET_FLOW = "wallet_flow"
    CONTRACT_STRUCTURE = "contract_structure"


class ExecutionStyle(StrEnum):
    MAKER = "maker"
    TAKER = "taker"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    market_id: str
    event_time: datetime
    receive_time: datetime
    yes_bid: Decimal
    yes_ask: Decimal
    no_bid: Decimal
    no_ask: Decimal
    seconds_to_expiry: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StrategySignal:
    strategy_name: str
    strategy_group: StrategyGroup
    market_id: str
    yes_probability: Decimal
    confidence: Decimal
    observed_at: datetime
    valid_until: datetime
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FusedSignal:
    market_id: str
    yes_probability: Decimal
    confidence: Decimal
    computed_at: datetime
    source_count: int


@dataclass(frozen=True, slots=True)
class TargetPosition:
    market_id: str
    outcome: Outcome
    target_notional_usd: Decimal
    max_acceptable_price: Decimal
    valid_until: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    approved_notional_usd: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    market_id: str
    outcome: Outcome
    style: ExecutionStyle
    limit_price: Decimal | None
    notional_usd: Decimal
    valid_until: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class OrderIntent:
    plan: ExecutionPlan
    client_order_id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

