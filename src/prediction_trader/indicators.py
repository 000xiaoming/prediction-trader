import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ProbabilityEstimate:
    up_probability: Decimal
    distance_usd: Decimal
    momentum_15s: Decimal
    volatility_60s: Decimal
    expected_remaining_move_usd: Decimal


def estimate_remaining_probability(
    prices: list[tuple[datetime, Decimal]],
    *,
    start_price: Decimal,
    remaining_seconds: int,
) -> ProbabilityEstimate | None:
    if len(prices) < 3 or start_price <= 0:
        return None
    ordered = sorted(prices, key=lambda point: point[0])
    current_time, current = ordered[-1]
    if current <= 0:
        return None
    returns: list[float] = []
    for (_, previous), (_, latest) in zip(ordered, ordered[1:], strict=False):
        if previous > 0 and latest > 0:
            returns.append(math.log(float(latest / previous)))
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    sigma_tick = math.sqrt(max(variance, 1e-18))
    elapsed = max((ordered[-1][0] - ordered[0][0]).total_seconds(), 1.0)
    seconds_per_tick = elapsed / max(len(returns), 1)
    sigma_second = sigma_tick / math.sqrt(seconds_per_tick)
    cutoff = current_time.timestamp() - 15
    momentum_base = next(
        (price for time, price in ordered if time.timestamp() >= cutoff), ordered[0][1]
    )
    momentum = current / momentum_base - 1 if momentum_base > 0 else Decimal("0")
    remaining = max(remaining_seconds, 1)
    denominator = sigma_second * math.sqrt(remaining)
    distance_log = math.log(float(current / start_price))
    # Momentum is displayed separately. Until calibrated, it must not be extrapolated to expiry.
    z_score = distance_log / max(denominator, 1e-12)
    probability = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
    expected_move = float(current) * denominator
    return ProbabilityEstimate(
        up_probability=Decimal(str(min(0.99, max(0.01, probability)))),
        distance_usd=current - start_price,
        momentum_15s=momentum,
        volatility_60s=Decimal(str(sigma_second * math.sqrt(60))),
        expected_remaining_move_usd=Decimal(str(expected_move)),
    )


@dataclass(frozen=True, slots=True)
class TradeCalculation:
    shares: Decimal
    buy_fee_usd: Decimal
    total_cost_usd: Decimal
    sell_fee_usd: Decimal
    sell_proceeds_usd: Decimal
    exit_pnl_usd: Decimal
    win_pnl_usd: Decimal
    lose_pnl_usd: Decimal
    break_even_probability: Decimal
    break_even_sell_price: Decimal | None
    target_sell_price: Decimal | None
    expected_value_usd: Decimal | None


def taker_fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    return shares * fee_rate * price * (Decimal("1") - price)


def _required_sell_price(
    required_proceeds_per_share: Decimal, fee_rate: Decimal
) -> Decimal | None:
    if required_proceeds_per_share > 1:
        return None
    low, high = Decimal("0"), Decimal("1")
    for _ in range(80):
        middle = (low + high) / 2
        net = middle - fee_rate * middle * (Decimal("1") - middle)
        if net < required_proceeds_per_share:
            low = middle
        else:
            high = middle
    return high


def calculate_manual_trade(
    *,
    stake_usd: Decimal,
    buy_price: Decimal,
    sell_price: Decimal,
    fee_rate: Decimal,
    target_profit_usd: Decimal = Decimal("0"),
    model_probability: Decimal | None = None,
) -> TradeCalculation:
    if stake_usd <= 0 or not 0 < buy_price < 1 or not 0 <= sell_price <= 1:
        raise ValueError("Stake and prices are outside the valid range")
    cost_per_share = buy_price + fee_rate * buy_price * (Decimal("1") - buy_price)
    shares = stake_usd / cost_per_share
    buy_fee = taker_fee(shares, buy_price, fee_rate)
    sell_fee = taker_fee(shares, sell_price, fee_rate)
    proceeds = shares * sell_price - sell_fee
    break_even_probability = stake_usd / shares
    expected_value = None
    if model_probability is not None:
        expected_value = shares * model_probability - stake_usd
    return TradeCalculation(
        shares=shares,
        buy_fee_usd=buy_fee,
        total_cost_usd=shares * buy_price + buy_fee,
        sell_fee_usd=sell_fee,
        sell_proceeds_usd=proceeds,
        exit_pnl_usd=proceeds - stake_usd,
        win_pnl_usd=shares - stake_usd,
        lose_pnl_usd=-stake_usd,
        break_even_probability=break_even_probability,
        break_even_sell_price=_required_sell_price(stake_usd / shares, fee_rate),
        target_sell_price=_required_sell_price(
            (stake_usd + target_profit_usd) / shares, fee_rate
        ),
        expected_value_usd=expected_value,
    )
