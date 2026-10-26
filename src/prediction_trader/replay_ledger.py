import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from prediction_trader.domain.models import StrategySignal
from prediction_trader.execution_replay import ExecutionReplayResult, SimulatedFill
from prediction_trader.persistence import (
    MarketRepository,
    replay_equity_points,
    replay_fills,
    replay_runs,
    replay_signals,
)


@dataclass(frozen=True, slots=True)
class EquityPoint:
    point_time: datetime
    equity_usd: Decimal
    drawdown_usd: Decimal


def build_marked_equity_curve(fills: tuple[SimulatedFill, ...]) -> tuple[EquityPoint, ...]:
    equity = Decimal("0")
    peak = Decimal("0")
    points: list[EquityPoint] = []
    for fill in sorted(fills, key=lambda item: item.fill_time):
        equity += fill.unrealized_pnl_usd or Decimal("0")
        peak = max(peak, equity)
        points.append(EquityPoint(fill.fill_time, equity, peak - equity))
    return tuple(points)


class ReplayLedger:
    def __init__(self, repository: MarketRepository) -> None:
        self._repository = repository

    def record(
        self,
        *,
        strategy_name: str,
        frames: int,
        signals: list[StrategySignal],
        replay: ExecutionReplayResult,
        parameters: dict[str, object],
    ) -> tuple[str, Decimal]:
        run_id = str(uuid4())
        now = datetime.now(UTC)
        curve = build_marked_equity_curve(replay.filled)
        max_drawdown = max((point.drawdown_usd for point in curve), default=Decimal("0"))
        filled_keys = {(fill.market_id, fill.signal_time) for fill in replay.filled}

        with self._repository.engine.begin() as connection:
            connection.execute(
                replay_runs.insert().values(
                    id=run_id,
                    strategy_name=strategy_name,
                    mode="event_execution_mark_to_market",
                    started_at=now,
                    completed_at=now,
                    frames=frames,
                    signal_count=replay.signals,
                    fill_count=len(replay.filled),
                    unfilled_count=replay.unfilled,
                    marked_pnl_usd=replay.marked_pnl_usd,
                    max_drawdown_usd=max_drawdown,
                    parameters=json.loads(json.dumps(parameters)),
                )
            )
            if signals:
                connection.execute(
                    replay_signals.insert(),
                    [
                        {
                            "run_id": run_id,
                            "strategy_name": signal.strategy_name,
                            "strategy_group": signal.strategy_group.value,
                            "market_id": signal.market_id,
                            "yes_probability": signal.yes_probability,
                            "confidence": signal.confidence,
                            "observed_at": signal.observed_at,
                            "valid_until": signal.valid_until,
                            "filled": (signal.market_id, signal.observed_at) in filled_keys,
                            "evidence": json.loads(json.dumps(signal.evidence, default=str)),
                        }
                        for signal in signals
                    ],
                )
            if replay.filled:
                connection.execute(
                    replay_fills.insert(),
                    [
                        {
                            "run_id": run_id,
                            "market_id": fill.market_id,
                            "outcome": fill.outcome.value,
                            "signal_time": fill.signal_time,
                            "fill_time": fill.fill_time,
                            "fill_price": fill.fill_price,
                            "mark_price": fill.mark_price,
                            "notional_usd": fill.notional_usd,
                            "shares": fill.shares,
                            "unrealized_pnl_usd": fill.unrealized_pnl_usd,
                        }
                        for fill in replay.filled
                    ],
                )
                connection.execute(
                    replay_equity_points.insert(),
                    [
                        {
                            "run_id": run_id,
                            "point_time": point.point_time,
                            "equity_usd": point.equity_usd,
                            "drawdown_usd": point.drawdown_usd,
                        }
                        for point in curve
                    ],
                )
        return run_id, max_drawdown
