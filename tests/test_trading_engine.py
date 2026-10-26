import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from prediction_trader.trading.catalog import INSTRUMENTS, event_instruments, get_instrument
from prediction_trader.trading.engine import PaperAccount
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import (
    InstrumentKind,
    InstrumentSpec,
    PaperOrder,
    Quote,
    TradingError,
)

D = Decimal
NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


def spec(kind: InstrumentKind = InstrumentKind.SPOT) -> InstrumentSpec:
    return replace(
        get_instrument(f"binance_btc_{kind.value}"),
        quantity_step=D("0.01"),
        minimum_quantity=D("0.01"),
        minimum_notional=D("1"),
        maximum_quantity=D("100"),
    )


def quote(
    instrument: InstrumentSpec,
    *,
    price: str = "100",
    portfolio: str = "alpha",
    at: datetime = NOW,
) -> Quote:
    return Quote(
        instrument.instrument_id,
        portfolio,
        at,
        D(price),
        D(price),
        D("100"),
        D("100"),
        mark_price=D(price) if instrument.kind == InstrumentKind.PERPETUAL else None,
    )


def order(
    instrument: InstrumentSpec,
    side: str = "buy",
    quantity: str = "1",
    key: str = "first",
    *,
    fee: str = "10",
    leverage: str = "1",
    portfolio: str = "alpha",
    reduce: bool = False,
) -> PaperOrder:
    return PaperOrder(
        key,
        instrument.instrument_id,
        portfolio,
        side,
        D(quantity),
        D(fee),
        D(leverage),
        reduce,
    )


def gamma() -> dict[str, object]:
    return {
        "id": "765432",
        "conditionId": "0x" + "ab" * 32,
        "question": "Will the example event happen?",
        "outcomes": '["No", "Yes"]',
        "clobTokenIds": '["222222", "111111"]',
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
        "endDate": (NOW + timedelta(days=1)).isoformat(),
        "negRisk": False,
    }


def resolved() -> dict[str, object]:
    return {
        **gamma(),
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomePrices": '["0", "1"]',
        "closedTime": (NOW + timedelta(days=1)).isoformat(),
    }


def test_catalog_is_frozen_and_allowlisted() -> None:
    assert len(INSTRUMENTS) == 4
    with pytest.raises(FrozenInstanceError):
        get_instrument("binance_btc_spot").symbol = "DOGEUSDT"  # type: ignore[misc]
    with pytest.raises(TradingError, match="Unknown instrument"):
        get_instrument("binance_btc_spot;curl")
    with pytest.raises(TradingError):
        replace(spec(), quantity_step=D("NaN"))
    with pytest.raises(TradingError):
        replace(spec(), maximum_leverage=D("2"))


def test_spot_partial_sale_cash_equity_and_fee_reconciliation() -> None:
    instrument = spec()
    account = PaperAccount("alpha", D("1000"))
    account.execute(instrument, order(instrument, quantity="2"), quote(instrument), now=NOW)
    fill = account.execute(
        instrument,
        order(instrument, "sell", "1", "sell"),
        quote(instrument, price="120"),
        now=NOW,
    )
    assert fill.realized_pnl == D("20")
    assert account.cash == D("919.68")
    assert account.positions[instrument.instrument_id].quantity == D("1")
    summary = account.summary()
    assert D(summary["equity"]) == D("1039.68")
    assert D(summary["unrealized_pnl"]) == D("20")
    assert D(summary["realized_pnl"]) == D("20")
    assert D(summary["fees"]) == D("0.32")


def test_spot_weighted_average_and_flat_close() -> None:
    instrument = spec()
    account = PaperAccount("alpha", D("1000"))
    account.execute(instrument, order(instrument, fee="0"), quote(instrument), now=NOW)
    account.execute(
        instrument, order(instrument, key="add", fee="0"), quote(instrument, price="120"), now=NOW
    )
    assert account.positions[instrument.instrument_id].average_price == D("110")
    fill = account.execute(
        instrument,
        order(instrument, "sell", "2", "exit", fee="0"),
        quote(instrument, price="130"),
        now=NOW,
    )
    assert fill.realized_pnl == D("40")
    assert account.cash == D("1040")
    assert account.positions[instrument.instrument_id].average_price == 0


@pytest.mark.parametrize(
    "bad_order,error",
    [
        ({"side": "sell"}, "naked short"),
        ({"quantity": D("10")}, "cash or margin"),
        ({"quantity": D("0.015")}, "precision"),
        ({"quantity": D("0.001")}, "precision"),
        ({"quantity": D("101")}, "maximum quantity"),
        ({"leverage": D("2")}, "leverage limit"),
        ({"reduce_only": True}, "reduce_only"),
    ],
)
def test_spot_rejections_leave_account_unchanged(bad_order: dict, error: str) -> None:
    instrument = spec()
    account = PaperAccount("alpha", D("1000"))
    before = account.to_state()
    with pytest.raises(TradingError, match=error):
        account.execute(
            instrument, replace(order(instrument), **bad_order), quote(instrument), now=NOW
        )
    assert account.to_state() == before


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-1"), None, 0.1])
def test_explicit_finite_fees_required(value: object) -> None:
    with pytest.raises(TradingError, match="fee_bps"):
        replace(order(spec()), fee_bps=value)


@pytest.mark.parametrize(
    "change,error",
    [
        ({"observed_at": NOW - timedelta(seconds=11)}, "stale"),
        ({"observed_at": NOW + timedelta(seconds=2)}, "future"),
        ({"portfolio_id": "beta"}, "mismatch"),
        ({"instrument_id": "binance_eth_spot"}, "mismatch"),
        ({"ask": D("100.005")}, "precision"),
        ({"ask_size": D("0.5")}, "liquidity"),
    ],
)
def test_quote_gates(change: dict, error: str) -> None:
    instrument = spec()
    account = PaperAccount("alpha", D("1000"))
    with pytest.raises(TradingError, match=error):
        account.execute(
            instrument, order(instrument), replace(quote(instrument), **change), now=NOW
        )
    assert account.cash == D("1000")


@pytest.mark.parametrize(
    "change",
    [
        {"bid": D("101")},
        {"bid": D("0")},
        {"ask_size": D("NaN")},
        {"observed_at": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_quote_construction(change: dict) -> None:
    with pytest.raises(TradingError):
        replace(quote(spec()), **change)


@pytest.mark.parametrize(
    "side,exit_price,expected",
    [
        ("buy", "120", "40"),
        ("sell", "80", "40"),
        ("buy", "80", "-40"),
    ],
)
def test_perpetual_long_short_realization(side: str, exit_price: str, expected: str) -> None:
    instrument = spec(InstrumentKind.PERPETUAL)
    account = PaperAccount("alpha", D("1000"))
    account.execute(
        instrument, order(instrument, side, "2", fee="0", leverage="2"), quote(instrument), now=NOW
    )
    assert account.cash == 1000
    assert D(account.summary()["margin"]) == D("100")
    account.mark(quote(instrument, price=exit_price), now=NOW)
    assert D(account.summary()["unrealized_pnl"]) == D(expected)
    fill = account.execute(
        instrument,
        order(
            instrument,
            "sell" if side == "buy" else "buy",
            "2",
            "close",
            fee="0",
            leverage="2",
            reduce=True,
        ),
        quote(instrument, price=exit_price),
        now=NOW,
    )
    assert fill.realized_pnl == D(expected)
    assert account.cash == 1000 + D(expected)
    assert D(account.summary()["margin"]) == 0


def test_perpetual_reversal_realizes_old_direction_only() -> None:
    instrument = spec(InstrumentKind.PERPETUAL)
    account = PaperAccount("alpha", D("1000"))
    account.execute(instrument, order(instrument, fee="0"), quote(instrument), now=NOW)
    fill = account.execute(
        instrument,
        order(instrument, "sell", "3", "reverse", fee="0"),
        quote(instrument, price="110"),
        now=NOW,
    )
    assert fill.realized_pnl == 10
    position = account.positions[instrument.instrument_id]
    assert position.quantity == -2
    assert position.average_price == 110
    assert account.cash == 1010


def test_perpetual_margin_and_reduce_only_limits() -> None:
    instrument = spec(InstrumentKind.PERPETUAL)
    account = PaperAccount("alpha", D("100"))
    account.execute(
        instrument, order(instrument, fee="0", leverage="2"), quote(instrument), now=NOW
    )
    for bad in [
        order(instrument, "sell", "2", "bad", leverage="2", reduce=True),
        order(instrument, "buy", "1", "bad", leverage="2", reduce=True),
        order(instrument, "buy", "1", "bad", leverage="1"),
        order(instrument, "buy", "3", "bad", leverage="2"),
    ]:
        with pytest.raises(TradingError):
            account.execute(instrument, bad, quote(instrument), now=NOW)
    assert account.positions[instrument.instrument_id].quantity == 1


def test_funding_is_separate_signed_once_and_halts_on_maintenance_breach() -> None:
    instrument = spec(InstrumentKind.PERPETUAL)
    account = PaperAccount("alpha", D("100"))
    account.execute(
        instrument, order(instrument, "buy", "2", fee="0", leverage="3"), quote(instrument), now=NOW
    )
    payment = account.apply_funding("funding-1", quote(instrument), D("0.01"), now=NOW)
    assert payment == -2
    assert account.cash == 98
    assert D(account.summary()["funding"]) == -2
    assert D(account.summary()["realized_pnl"]) == 0
    assert account.apply_funding("funding-1", quote(instrument), D("0.01"), now=NOW) == -2
    assert account.cash == 98
    with pytest.raises(TradingError, match="Idempotency"):
        account.apply_funding("funding-1", quote(instrument), D("0.02"), now=NOW)
    account.mark(quote(instrument, price="50"), now=NOW)
    assert account.halted_reason == "maintenance_margin_breach_manual_review_required"
    with pytest.raises(TradingError, match="halted"):
        account.execute(
            instrument,
            order(instrument, key="cannot-add", leverage="3"),
            quote(instrument, price="50"),
            now=NOW,
        )
    assert len(account.orders) == 1  # No fictional liquidation execution.


def test_short_positive_funding_receives_cash() -> None:
    instrument = spec(InstrumentKind.PERPETUAL)
    account = PaperAccount("alpha", D("1000"))
    account.execute(instrument, order(instrument, "sell", "2", fee="0"), quote(instrument), now=NOW)
    assert account.apply_funding("f1", quote(instrument), D("0.01"), now=NOW) == 2


def test_multi_instrument_margin_rejects_stale_other_collateral() -> None:
    instrument = spec(InstrumentKind.PERPETUAL)
    account = PaperAccount("alpha", D("1000"))
    account.execute(instrument, order(instrument), quote(instrument), now=NOW)
    second = replace(instrument, instrument_id="binance_eth_perpetual", symbol="ETHUSDT")
    later = NOW + timedelta(seconds=15)
    with pytest.raises(TradingError, match="stale"):
        account.execute(second, order(second, key="next"), quote(second, at=later), now=later)


def test_gamma_mapping_uses_outcome_indices_not_array_assumptions() -> None:
    yes, no = event_instruments(gamma())
    assert yes.outcome == "YES" and yes.token_id == "111111"
    assert no.outcome == "NO" and no.token_id == "222222"
    assert yes.instrument_id == "polymarket_event_765432_yes"
    for bad in [
        dict(gamma(), outcomes='["Up", "Down"]'),
        dict(gamma(), clobTokenIds='["111111", "111111"]'),
        dict(gamma(), active=False),
        dict(gamma(), acceptingOrders=False),
        dict(gamma(), negRisk=True),
        dict(gamma(), conditionId="wrong"),
    ]:
        with pytest.raises(TradingError):
            event_instruments(bad)


@pytest.mark.parametrize("outcome_index,payout,profit", [(0, "10", "6"), (1, "0", "-4")])
def test_event_final_settlement(outcome_index: int, payout: str, profit: str) -> None:
    instrument = event_instruments(gamma())[outcome_index]
    account = PaperAccount("alpha", D("100"), "USDC")
    account.execute(
        instrument,
        order(instrument, quantity="10", fee="0"),
        quote(instrument, price="0.40"),
        now=NOW,
    )
    then = NOW + timedelta(days=1)
    assert account.settle_event(instrument.instrument_id, resolved(), now=then) == D(payout)
    assert D(account.summary()["realized_pnl"]) == D(profit)
    assert account.cash == 100 + D(profit)
    assert account.settle_event(instrument.instrument_id, resolved(), now=then) == 0
    with pytest.raises(TradingError, match="settled"):
        account.execute(
            instrument,
            order(instrument, quantity="10", key="again"),
            quote(instrument, price="0.4", at=then),
            now=then,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"id": "123"},
        {"conditionId": "0x" + "cc" * 32},
        {"clobTokenIds": '["111111","222222"]'},
        {"umaResolutionStatus": "disputed"},
        {"outcomePrices": '["0.1","0.9"]'},
        {"closed": False},
    ],
)
def test_event_settlement_rejects_unverified_or_mismatched_resolution(change: dict) -> None:
    instrument = event_instruments(gamma())[0]
    account = PaperAccount("alpha", D("100"), "USDC")
    account.execute(
        instrument,
        order(instrument, quantity="10", fee="0"),
        quote(instrument, price="0.40"),
        now=NOW,
    )
    with pytest.raises(TradingError):
        account.settle_event(
            instrument.instrument_id, {**resolved(), **change}, now=NOW + timedelta(days=1)
        )
    assert account.cash == 96


def test_ledger_restart_idempotence_history_and_portfolio_isolation(tmp_path: Path) -> None:
    instrument = spec()
    path = tmp_path / "nested" / "paper.db"
    ledger = SQLitePaperLedger(path)
    fill, _ = ledger.execute(instrument, order(instrument), quote(instrument), now=NOW)
    restarted = SQLitePaperLedger(path)
    later = NOW + timedelta(hours=1)
    same, account = restarted.execute(
        instrument,
        order(instrument),
        quote(instrument, price="110"),
        now=later,
    )
    assert same == fill
    assert account.cash == D("899.9")
    assert len(restarted.history("alpha")) == 1
    with pytest.raises(TradingError, match="Idempotency conflict"):
        restarted.execute(instrument, order(instrument, quantity="2"), quote(instrument), now=NOW)
    _, other = restarted.execute(
        instrument,
        order(instrument, portfolio="beta"),
        quote(instrument, portfolio="beta"),
        now=NOW,
    )
    assert other.portfolio_id == "beta"
    assert len(restarted.history("beta")) == 1
    assert restarted.account("alpha").to_state() == account.to_state()
    assert restarted.account("missing") is None


def test_ledger_concurrent_retries_fill_once(tmp_path: Path) -> None:
    instrument = spec()
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    with ThreadPoolExecutor(max_workers=6) as pool:
        fills = list(
            pool.map(
                lambda _: ledger.execute(
                    instrument,
                    order(instrument),
                    quote(instrument),
                    now=NOW,
                )[0],
                range(12),
            )
        )
    assert len(set(fills)) == 1
    assert len(ledger.history("alpha")) == 1
    assert ledger.account("alpha").cash == D("899.9")


def test_ledger_rejected_first_order_creates_no_account(tmp_path: Path) -> None:
    instrument = spec()
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    with pytest.raises(TradingError):
        ledger.execute(instrument, order(instrument, "sell"), quote(instrument), now=NOW)
    assert ledger.account("alpha") is None
    assert ledger.history("alpha") == []


def test_repeated_quote_cannot_reuse_consumed_depth_after_restart(tmp_path: Path) -> None:
    instrument = spec()
    path = tmp_path / "liquidity.db"
    book = replace(quote(instrument), ask_size=D("1.5"))
    SQLitePaperLedger(path).execute(instrument, order(instrument), book, now=NOW)
    with pytest.raises(TradingError, match="liquidity"):
        SQLitePaperLedger(path).execute(
            instrument,
            order(instrument, key="second"),
            book,
            now=NOW,
        )
    assert len(SQLitePaperLedger(path).history("alpha")) == 1


def test_registered_instrument_cannot_change_contract_identity() -> None:
    instrument = spec()
    account = PaperAccount("alpha", D("1000"))
    account.execute(instrument, order(instrument), quote(instrument), now=NOW)
    changed = replace(instrument, symbol="ETHUSDT", base_currency="ETH")
    with pytest.raises(TradingError, match="identity changed"):
        account.execute(changed, order(changed, key="second"), quote(changed), now=NOW)


def test_ledger_currency_mismatch_and_restore_funding_mark_state(tmp_path: Path) -> None:
    instrument = spec(InstrumentKind.PERPETUAL)
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    ledger.execute(instrument, order(instrument, fee="0"), quote(instrument), now=NOW)
    ledger.funding("payment", quote(instrument), D("0.01"), now=NOW)
    ledger.mark(quote(instrument, price="110"), now=NOW)
    account = SQLitePaperLedger(tmp_path / "paper.db").account("alpha")
    assert account.cash == 999
    assert D(account.summary()["unrealized_pnl"]) == 10
    assert [row["event_type"] for row in ledger.history("alpha")] == ["mark", "funding", "fill"]
    assert PaperAccount.from_state(json.loads(json.dumps(account.to_state()))).summary() == (
        account.summary()
    )
    event = event_instruments(gamma())[0]
    with pytest.raises(TradingError, match="Currency mismatch"):
        ledger.execute(
            event, order(event, quantity="10", key="event"), quote(event, price="0.4"), now=NOW
        )
