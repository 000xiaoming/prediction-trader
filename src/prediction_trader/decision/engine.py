from datetime import datetime, timedelta
from decimal import Decimal

from prediction_trader.domain.models import FusedSignal, MarketSnapshot, Outcome, TargetPosition


class DecisionEngine:
    def __init__(self, min_net_edge: Decimal, default_notional_usd: Decimal) -> None:
        self._min_net_edge = min_net_edge
        self._default_notional = default_notional_usd

    def decide(
        self,
        snapshot: MarketSnapshot,
        fused: FusedSignal,
        explicit_cost_buffer: Decimal,
        now: datetime,
    ) -> TargetPosition | None:
        yes_edge = fused.yes_probability - snapshot.yes_ask - explicit_cost_buffer
        no_probability = Decimal("1") - fused.yes_probability
        no_edge = no_probability - snapshot.no_ask - explicit_cost_buffer

        if max(yes_edge, no_edge) < self._min_net_edge:
            return None
        outcome = Outcome.YES if yes_edge >= no_edge else Outcome.NO
        price = snapshot.yes_ask if outcome is Outcome.YES else snapshot.no_ask
        edge = yes_edge if outcome is Outcome.YES else no_edge
        return TargetPosition(
            market_id=snapshot.market_id,
            outcome=outcome,
            target_notional_usd=self._default_notional,
            max_acceptable_price=price,
            valid_until=min(now + timedelta(seconds=2), fused.computed_at + timedelta(seconds=3)),
            reason=f"net_edge={edge}",
        )

