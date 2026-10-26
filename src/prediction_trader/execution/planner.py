from datetime import datetime
from decimal import Decimal

from prediction_trader.domain.models import (
    ExecutionPlan,
    ExecutionStyle,
    MarketSnapshot,
    RiskDecision,
    TargetPosition,
)


class ExecutionPlanner:
    def plan(
        self,
        target: TargetPosition,
        risk: RiskDecision,
        snapshot: MarketSnapshot,
        now: datetime,
    ) -> ExecutionPlan:
        if not risk.approved or now > target.valid_until:
            return ExecutionPlan(
                target.market_id,
                target.outcome,
                ExecutionStyle.SKIP,
                None,
                Decimal("0"),
                target.valid_until,
                risk.reason,
            )

        ask = snapshot.yes_ask if target.outcome.value == "YES" else snapshot.no_ask
        bid = snapshot.yes_bid if target.outcome.value == "YES" else snapshot.no_bid
        spread = ask - bid
        should_make = snapshot.seconds_to_expiry > 60 and spread > Decimal("0.01")
        style = ExecutionStyle.MAKER if should_make else ExecutionStyle.TAKER
        price = bid if style is ExecutionStyle.MAKER else min(ask, target.max_acceptable_price)
        return ExecutionPlan(
            target.market_id,
            target.outcome,
            style,
            price,
            risk.approved_notional_usd,
            target.valid_until,
            f"style={style.value};spread={spread}",
        )
