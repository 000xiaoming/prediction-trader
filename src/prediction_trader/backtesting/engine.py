"""Deterministic replay with one point-in-time forecast per market window.

Quotes have no historical executable depth. Fills are explicitly a top-of-book
simulation; actual queue position, size, settlement disputes and capital allocation
are not modeled. Missing fee or settlement observation history is never invented.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from prediction_trader.profiles import MarketProfile
from prediction_trader.reference_analysis import ReferencePoint, analyze_reference

ZERO = Decimal("0")
ONE = Decimal("1")
Side = Literal["YES", "NO"]


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True, slots=True)
class BacktestParameters:
    start: datetime
    end: datetime
    as_of: datetime
    decision_delay_seconds: int = 60
    latency_ms: int = 250
    timeout_seconds: int = 5
    quote_max_age_seconds: int = 5
    reference_max_age_seconds: int = 5
    slippage_bps: Decimal = Decimal("5")
    min_edge: Decimal = Decimal("0.03")
    stake_usd: Decimal = Decimal("10")
    twap_window_seconds: int = 60
    max_markets: int = 5000

    def __post_init__(self) -> None:
        if not all(_aware(value) for value in (self.start, self.end, self.as_of)):
            raise ValueError("start, end and as_of must include a timezone")
        if not self.start < self.end <= self.as_of:
            raise ValueError("Require start < end <= as_of")
        if self.end - self.start > timedelta(days=7):
            raise ValueError("Replay range cannot exceed seven days")
        for name in (
            "decision_delay_seconds",
            "timeout_seconds",
            "quote_max_age_seconds",
            "reference_max_age_seconds",
            "max_markets",
        ):
            if not isinstance(getattr(self, name), int) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_markets > 5000:
            raise ValueError("max_markets cannot exceed 5000")
        if not isinstance(self.latency_ms, int) or not 0 <= self.latency_ms <= 60000:
            raise ValueError("latency_ms must be between 0 and 60000")
        if self.twap_window_seconds not in (30, 60):
            raise ValueError("TWAP window must be 30 or 60 seconds")
        for name, maximum, allow_zero in (
            ("stake_usd", Decimal("1000000"), False),
            ("min_edge", ONE, True),
            ("slippage_bps", Decimal("1000"), True),
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
            if value < ZERO or (not allow_zero and value == ZERO) or value > maximum:
                raise ValueError(f"{name} is outside supported bounds")

    def validate_profile(self, profile: MarketProfile) -> None:
        if self.decision_delay_seconds >= profile.duration_seconds:
            raise ValueError("Decision delay must be shorter than the market duration")

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, Decimal)
            else value
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True, slots=True)
class HistoricalReference:
    source: str
    symbol: str
    source_time: datetime
    observed_at: datetime
    price: Decimal


@dataclass(frozen=True, slots=True)
class HistoricalQuote:
    token_id: str
    outcome: str
    observed_at: datetime
    best_ask: Decimal | None
    source_time: datetime | None = None
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class HistoricalSettlement:
    winning_outcome: str
    resolved_at: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class HistoricalMarket:
    market_id: str
    slug: str
    end: datetime
    yes_token_id: str
    no_token_id: str
    raw: dict[str, Any]
    metadata_observed_at: datetime
    references: tuple[HistoricalReference, ...] = ()
    quotes: tuple[HistoricalQuote, ...] = ()
    settlement: HistoricalSettlement | None = None
    # Ingestion snapshots can provide historical metadata separately from current raw.
    fee_raw: dict[str, Any] | None = None
    fee_observed_at: datetime | None = None
    data_issues: tuple[str, ...] = ()


def explicit_fee_rate(raw: dict[str, Any]) -> Decimal | None:
    """Only supported, explicitly recorded per-market fee schedules are replayable."""
    if raw.get("feesEnabled") is False:
        return ZERO
    schedule = raw.get("feeSchedule")
    if not isinstance(schedule, dict) or "rate" not in schedule:
        return None
    # The supported formula is shares * rate * price * (1-price). Other powers
    # are not interchangeable with this schedule and must not silently be used.
    exponent = schedule.get("exponent", 1)
    if str(exponent) not in ("1", "1.0"):
        return None
    try:
        rate = Decimal(str(schedule["rate"]))
    except (InvalidOperation, ValueError):
        return None
    return rate if rate.is_finite() and ZERO <= rate <= ONE else None


def _quote_valid(
    quote: HistoricalQuote,
    market: HistoricalMarket,
    start: datetime,
    now: datetime,
    max_age_seconds: int,
) -> bool:
    tokens = {market.yes_token_id: "YES", market.no_token_id: "NO"}
    return (
        tokens.get(quote.token_id) == quote.outcome
        and quote.best_ask is not None
        and quote.best_ask.is_finite()
        and ZERO < quote.best_ask < ONE
        and start <= quote.observed_at <= now
        and (now - quote.observed_at).total_seconds() <= max_age_seconds
        and (
            quote.source_time is None
            or start <= quote.source_time <= now
            and (now - quote.source_time).total_seconds() <= max_age_seconds
        )
    )


def _cost_per_share(price: Decimal, fee_rate: Decimal) -> Decimal:
    return price + fee_rate * price * (ONE - price)


def _settlement_known(
    settlement: HistoricalSettlement | None,
    market: HistoricalMarket,
    as_of: datetime,
) -> bool:
    return (
        settlement is not None
        and settlement.winning_outcome in ("YES", "NO")
        and market.end <= settlement.resolved_at <= as_of
        and settlement.observed_at <= as_of
    )


def _market_decision(
    profile: MarketProfile,
    params: BacktestParameters,
    market: HistoricalMarket,
) -> dict[str, Any]:
    start = market.end - timedelta(seconds=profile.duration_seconds)
    decision_at = start + timedelta(seconds=params.decision_delay_seconds)
    result: dict[str, Any] = {
        "market_id": market.market_id,
        "slug": market.slug,
        "start": start.isoformat(),
        "end": market.end.isoformat(),
        "decision_at": decision_at.isoformat(),
        "status": "skipped",
        "issues": list(market.data_issues),
        "up_probability": None,
        "winning_outcome": None,
        "signal": None,
        "fill": None,
    }
    if _settlement_known(market.settlement, market, params.as_of):
        assert market.settlement is not None
        result["winning_outcome"] = market.settlement.winning_outcome
    if decision_at > params.as_of:
        result["issues"].append("decision_not_yet_observed")
        return result
    if market.data_issues:
        return result
    refs = [
        ReferencePoint(point.source_time, point.observed_at, point.price)
        for point in market.references
        if point.source == f"polymarket_rtds_chainlink_twap_{params.twap_window_seconds}"
        and point.symbol == profile.reference_symbol
    ]
    analysis = analyze_reference(
        refs,
        start=start,
        end=market.end,
        now=decision_at,
        max_age_seconds=params.reference_max_age_seconds,
        opening_tolerance_seconds=2,
    )
    result["reference_samples"] = analysis.sample_count
    result["issues"].extend(analysis.issues)
    if analysis.estimate is None:
        if not result["issues"]:
            result["issues"].append("probability_unavailable")
        return result
    # Current contract metadata cannot be backdated to a historical decision.
    raw = market.fee_raw if market.fee_raw is not None else market.raw
    metadata_at = market.fee_observed_at or market.metadata_observed_at
    if metadata_at <= decision_at:
        config = raw.get("cryptoMarketConfig")
        contract_window = config.get("twapLookbackSeconds") if isinstance(config, dict) else None
        if contract_window is not None and str(contract_window) != str(params.twap_window_seconds):
            result["issues"].append("contract_twap_window_mismatch")
            return result
    probability = analysis.estimate.up_probability
    result["up_probability"] = str(probability)
    result["status"] = "prediction_only"
    if metadata_at > decision_at:
        result["issues"].append("fee_not_known_at_decision")
        return result
    fee_rate = explicit_fee_rate(raw)
    if fee_rate is None:
        result["issues"].append("fee_schedule_missing_or_unsupported")
        return result
    result["fee_rate"] = str(fee_rate)
    quotes = sorted(market.quotes, key=lambda quote: (quote.observed_at, quote.sequence))
    latest: dict[str, HistoricalQuote] = {}
    for quote in quotes:
        if quote.observed_at > decision_at:
            continue
        if (quote.token_id, quote.outcome) not in (
            (market.yes_token_id, "YES"),
            (market.no_token_id, "NO"),
        ):
            continue
        # A newer empty/invalid book invalidates the previous ask. Skipping it
        # would resurrect an earlier quote which no longer exists at the signal.
        if _quote_valid(quote, market, start, decision_at, params.quote_max_age_seconds):
            latest[quote.outcome] = quote
        else:
            latest.pop(quote.outcome, None)
    candidates: list[tuple[Decimal, str, Decimal, Decimal]] = []
    multiplier = ONE + params.slippage_bps / Decimal("10000")
    for side, side_probability in (("YES", probability), ("NO", ONE - probability)):
        decision_quote = latest.get(side)
        if decision_quote is None or decision_quote.best_ask is None:
            continue
        limit = decision_quote.best_ask * multiplier
        edge = side_probability - _cost_per_share(limit, fee_rate)
        if limit < ONE and edge >= params.min_edge:
            candidates.append((edge, side, side_probability, limit))
    if not candidates:
        result["issues"].append("no_fee_adjusted_edge" if latest else "quotes_stale_or_missing")
        return result
    edge, side, side_probability, limit = max(candidates, key=lambda candidate: candidate[0])
    arrival = decision_at + timedelta(milliseconds=params.latency_ms)
    deadline = min(arrival + timedelta(seconds=params.timeout_seconds), market.end)
    result["signal"] = {
        "outcome": side,
        "probability": str(side_probability),
        "edge": str(edge),
        "limit_price": str(limit),
        "arrival_at": arrival.isoformat(),
        "deadline": deadline.isoformat(),
    }
    result["status"] = "unfilled" if params.as_of >= deadline else "pending_order"
    for quote in quotes:
        if quote.outcome != side or not (
            decision_at < quote.observed_at
            and arrival <= quote.observed_at <= deadline
            and quote.observed_at < market.end
            and quote.observed_at <= params.as_of
        ):
            continue
        if quote.source_time is not None and quote.source_time <= decision_at:
            continue
        if not _quote_valid(
            quote,
            market,
            start,
            quote.observed_at,
            params.quote_max_age_seconds,
        ):
            continue
        assert quote.best_ask is not None
        if quote.best_ask > limit:
            continue
        fill_price = min(quote.best_ask * multiplier, limit)
        per_share = _cost_per_share(fill_price, fee_rate)
        if side_probability - per_share < params.min_edge:
            continue
        shares = params.stake_usd / per_share
        fee = shares * fee_rate * fill_price * (ONE - fill_price)
        fill: dict[str, Any] = {
            "outcome": side,
            "fill_time": quote.observed_at.isoformat(),
            "price": str(fill_price),
            "shares": str(shares),
            "fee_usd": str(fee),
            "cost_usd": str(params.stake_usd),
            "realized_pnl_usd": None,
            "settled_at": None,
        }
        if result["winning_outcome"] is not None:
            assert market.settlement is not None
            payout = shares if side == result["winning_outcome"] else ZERO
            fill["realized_pnl_usd"] = str(payout - params.stake_usd)
            fill["settled_at"] = max(
                market.settlement.resolved_at,
                market.settlement.observed_at,
            ).isoformat()
        result["fill"] = fill
        result["status"] = "filled"
        break
    return result


def replay_profile(
    profile: MarketProfile,
    parameters: BacktestParameters,
    history: Iterable[HistoricalMarket],
) -> dict[str, Any]:
    """Replay supplied history deterministically; all output is JSON-serializable."""
    parameters.validate_profile(profile)
    decisions: list[dict[str, Any]] = []
    truncated = False
    seen: set[str] = set()
    for market in history:
        if market.market_id in seen or not profile.matches_market(market.slug, market.end):
            continue
        start = market.end - timedelta(seconds=profile.duration_seconds)
        if not parameters.start <= start < parameters.end:
            continue
        seen.add(market.market_id)
        if len(decisions) >= parameters.max_markets:
            truncated = True
            break
        decisions.append(_market_decision(profile, parameters, market))
    decisions.sort(key=lambda item: (item["decision_at"], item["market_id"]))
    forecasts = [item for item in decisions if item["up_probability"] is not None]
    resolved = [item for item in forecasts if item["winning_outcome"] is not None]
    fills = [item["fill"] for item in decisions if item["fill"] is not None]
    settled = [fill for fill in fills if fill["realized_pnl_usd"] is not None]
    brier = (
        sum(
            (Decimal(item["up_probability"]) - (ONE if item["winning_outcome"] == "YES" else ZERO))
            ** 2
            for item in resolved
        )
        / len(resolved)
        if resolved
        else None
    )
    bins: list[dict[str, Any]] = []
    for index in range(10):
        entries = [
            item for item in resolved if min(9, int(Decimal(item["up_probability"]) * 10)) == index
        ]
        bins.append(
            {
                "lower": index / 10,
                "upper": (index + 1) / 10,
                "count": len(entries),
                "mean_probability": str(
                    sum(
                        (Decimal(item["up_probability"]) for item in entries),
                        ZERO,
                    )
                    / len(entries)
                )
                if entries
                else None,
                "observed_up_frequency": str(
                    Decimal(sum(item["winning_outcome"] == "YES" for item in entries))
                    / len(entries)
                )
                if entries
                else None,
            }
        )
    equity = peak = drawdown = ZERO
    equity_curve: list[dict[str, str]] = []
    for fill in sorted(settled, key=lambda item: (item["settled_at"], item["fill_time"])):
        equity += Decimal(fill["realized_pnl_usd"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        equity_curve.append(
            {
                "time": fill["settled_at"],
                "realized_pnl_usd": str(equity),
                "drawdown_usd": str(peak - equity),
            }
        )
    issue_counts = Counter(issue for item in decisions for issue in item["issues"])
    return {
        "schema_version": 1,
        "mode": "profile_research_replay",
        "profile_id": profile.id,
        "profile": profile.as_dict(),
        "as_of": parameters.as_of.isoformat(),
        "parameters": parameters.as_dict(),
        "truncated": truncated,
        "metrics": {
            "market_count": len(decisions),
            "prediction_count": len(forecasts),
            "prediction_coverage": len(forecasts) / len(decisions) if decisions else 0,
            "resolved_prediction_count": len(resolved),
            "unresolved_prediction_count": len(forecasts) - len(resolved),
            "signal_count": sum(item["signal"] is not None for item in decisions),
            "fill_count": len(fills),
            "unfilled_count": sum(item["status"] == "unfilled" for item in decisions),
            "pending_order_count": sum(item["status"] == "pending_order" for item in decisions),
            "settled_fill_count": len(settled),
            "unresolved_fill_count": len(fills) - len(settled),
            "unresolved_cost_usd": str(
                sum(
                    (
                        Decimal(fill["cost_usd"])
                        for fill in fills
                        if fill["realized_pnl_usd"] is None
                    ),
                    ZERO,
                )
            ),
            "total_fees_usd": str(sum((Decimal(fill["fee_usd"]) for fill in fills), ZERO)),
            "realized_pnl_usd": str(equity),
            "max_realized_drawdown_usd": str(drawdown),
            "brier_score": str(brier) if brier is not None else None,
            "issues": dict(sorted(issue_counts.items())),
        },
        "calibration_bins": bins,
        "equity_curve": equity_curve,
        "decisions": decisions,
        "limitations": [
            "Research simulation; probability model is uncalibrated and no orders are submitted.",
            "Top-of-book fills assume the configured stake can fill; depth and queue are unknown.",
            "Fill requires a new eligible quote after signal latency, within the expiry window.",
            "Fee formula: shares * recorded rate * price * (1-price); no fee-rate fallback.",
            "Mutable fee metadata observed after a decision cannot authorize a simulated trade.",
            "Settlement counts only when resolved and observed by as_of; no estimated wins.",
            "Opening reference is a local approximation within start to start+2s.",
            "Realized drawdown excludes unresolved holdings and capital allocation is not modeled.",
            "Discovery history is incomplete; the retrospective cohort may have selection bias.",
        ],
    }
