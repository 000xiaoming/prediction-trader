from datetime import datetime
from decimal import Decimal

from prediction_trader.domain.models import RiskDecision, TargetPosition


class RiskEngine:
    def __init__(
        self,
        max_market_exposure_usd: Decimal,
        max_total_exposure_usd: Decimal,
        max_daily_loss_usd: Decimal,
    ) -> None:
        self._max_market = max_market_exposure_usd
        self._max_total = max_total_exposure_usd
        self._max_daily_loss = max_daily_loss_usd

    def assess(
        self,
        target: TargetPosition,
        *,
        now: datetime,
        market_exposure_usd: Decimal,
        total_exposure_usd: Decimal,
        daily_pnl_usd: Decimal,
        reconciled: bool,
    ) -> RiskDecision:
        if not reconciled:
            return RiskDecision(False, Decimal("0"), "account_not_reconciled")
        if now > target.valid_until:
            return RiskDecision(False, Decimal("0"), "target_expired")
        if daily_pnl_usd <= -self._max_daily_loss:
            return RiskDecision(False, Decimal("0"), "daily_loss_limit")

        market_capacity = max(Decimal("0"), self._max_market - market_exposure_usd)
        total_capacity = max(Decimal("0"), self._max_total - total_exposure_usd)
        approved = min(target.target_notional_usd, market_capacity, total_capacity)
        if approved <= 0:
            return RiskDecision(False, Decimal("0"), "exposure_limit")
        reason = "approved" if approved == target.target_notional_usd else "clamped"
        return RiskDecision(True, approved, reason)
