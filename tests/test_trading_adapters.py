import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from prediction_trader.trading.adapters import PublicMarketData
from prediction_trader.trading.cli import main
from prediction_trader.trading.models import InstrumentKind, TradingError

D = Decimal


def exchange_info(perpetual: bool = False) -> dict:
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "contractType": "PERPETUAL" if perpetual else None,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {
                        "filterType": "LOT_SIZE",
                        "stepSize": "0.001",
                        "minQty": "0.001",
                        "maxQty": "1000",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "stepSize": "0",
                        "minQty": "0",
                        "maxQty": "50",
                    },
                    {"filterType": "MIN_NOTIONAL", "notional" if perpetual else "minNotional": "5"},
                ],
            }
        ]
    }


def gamma() -> dict:
    return {
        "id": "123456",
        "conditionId": "0x" + "ab" * 32,
        "question": "Example event",
        "outcomes": '["No", "Yes"]',
        "clobTokenIds": '["222222", "111111"]',
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
        "negRisk": False,
        "endDate": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }


def clob_book() -> dict:
    return {
        "market": "0x" + "ab" * 32,
        "asset_id": "111111",
        "bids": [{"price": "0.2", "size": "100"}, {"price": "0.4", "size": "11"}],
        "asks": [{"price": "0.9", "size": "100"}, {"price": "0.6", "size": "12"}],
        "timestamp": str(int(datetime.now(UTC).timestamp() * 1000)),
        "tick_size": "0.01",
        "min_order_size": "5",
    }


@pytest.mark.parametrize("perpetual", [False, True])
def test_binance_public_paths_rules_and_reused_client(perpetual: bool) -> None:
    requests: list[httpx.Request] = []
    observed = datetime.now(UTC)
    milliseconds = int(observed.timestamp() * 1000)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert "x-mbx-apikey" not in request.headers
        assert "signature" not in request.url.params
        assert request.extensions["timeout"]["read"] == 3
        if request.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json=exchange_info(perpetual))
        assert request.url.params["symbol"] == "BTCUSDT"
        if request.url.path.endswith("bookTicker"):
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "bidPrice": "100",
                    "askPrice": "100.1",
                    "bidQty": "1",
                    "askQty": "2",
                    "time": milliseconds,
                },
            )
        assert request.url.path == "/fapi/v1/premiumIndex"
        return httpx.Response(
            200,
            json={
                "symbol": "BTCUSDT",
                "markPrice": "100.05",
                "lastFundingRate": "0.0001",
                "nextFundingTime": milliseconds + 100000,
                "time": milliseconds - 500,
            },
        )

    kind = "perpetual" if perpetual else "spot"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with PublicMarketData(client, timeout_seconds=3) as market:
            instrument, quote = market.snapshot(f"binance_btc_{kind}", "paper-btc")
            market.snapshot(f"binance_btc_{kind}", "paper-btc")
        assert not client.is_closed  # Ownership remains with the caller.
    assert sum(r.url.path.endswith("exchangeInfo") for r in requests) == 1
    assert instrument.price_tick == D("0.10")
    assert instrument.quantity_step == D("0.001")
    assert instrument.minimum_notional == D("5")
    assert instrument.maximum_quantity == D("50")
    assert quote.portfolio_id == "paper-btc"
    assert quote.ask == D("100.1")
    assert instrument.kind == (InstrumentKind.PERPETUAL if perpetual else InstrumentKind.SPOT)
    if perpetual:
        assert quote.mark_price == D("100.05")
        assert quote.funding_rate == D("0.0001")
        assert abs((observed - quote.observed_at).total_seconds() - 0.5) < 0.002
        assert {r.url.host for r in requests} == {"fapi.binance.com"}
    else:
        assert quote.mark_price is None
        assert "no_exchange_timestamp" in quote.timestamp_basis
        assert {r.url.host for r in requests} == {"data-api.binance.vision"}


def test_event_snapshot_uses_gamma_index_mapping_and_unsorted_book() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "gamma-api.polymarket.com":
            assert request.url.path == "/markets/123456"
            return httpx.Response(200, json=gamma())
        assert request.url.host == "clob.polymarket.com"
        assert request.url.path == "/book"
        assert request.url.params["token_id"] == "111111"
        return httpx.Response(200, json=clob_book())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        spec, quote = PublicMarketData(client).snapshot("polymarket_event_123456_yes", "event")
    assert spec.token_id == "111111"
    assert spec.outcome == "YES"
    assert spec.quote_currency == "USDC"
    assert quote.bid == D("0.4") and quote.ask == D("0.6")
    assert quote.bid_size == 11 and quote.ask_size == 12
    assert len(requests) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        {"asset_id": "wrong"},
        {"market": "wrong"},
        {"asks": []},
        {"bids": [{"price": "0.4", "size": "NaN"}]},
        {"asks": [{"price": "1.01", "size": "1"}]},
        {"timestamp": "NaN"},
    ],
)
def test_bad_clob_books_fail_closed(mutation: dict) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=(
                gamma()
                if request.url.host == "gamma-api.polymarket.com"
                else {**clob_book(), **mutation}
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, pytest.raises(TradingError):
        PublicMarketData(client).snapshot("polymarket_event_123456_yes", "event")


@pytest.mark.parametrize(
    "mutation",
    [
        {"symbol": "ETHUSDT"},
        {"status": "BREAK"},
        {"quoteAsset": "USDC"},
        {"filters": []},
    ],
)
def test_bad_exchange_metadata_fail_closed(mutation: dict) -> None:
    payload = exchange_info()
    payload["symbols"][0].update(mutation)
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        ) as client,
        pytest.raises(TradingError),
    ):
        PublicMarketData(client).snapshot("binance_btc_spot")


@pytest.mark.parametrize(
    "instrument",
    [
        "binance_doge_spot",
        "https://example.org",
        "polymarket_event_123/secret_yes",
        "polymarket_event_123_up",
        "binance_btc_spot?symbol=other",
    ],
)
def test_unknown_selection_makes_no_network_request(instrument: str) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Unexpected network request")

    with (
        httpx.Client(transport=httpx.MockTransport(unexpected)) as client,
        pytest.raises(TradingError),
    ):
        PublicMarketData(client).snapshot(instrument)


@pytest.mark.parametrize("status", [301, 403, 429, 500])
def test_http_failure_no_redirect_or_unbounded_retry(status: int) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status, headers={"location": "https://private.example/", "retry-after": "1"}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as client,
        pytest.raises(TradingError, match=str(status)),
    ):
        PublicMarketData(client).snapshot("binance_btc_spot")
    assert len(calls) == 1


def test_cli_lists_catalog_and_forbids_live_flag(capsys: pytest.CaptureFixture[str]) -> None:
    main(["list"])
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["instruments"]) == 4
    assert payload["live_execution"] is False
    with pytest.raises(SystemExit) as error:
        main(["paper-run", "binance_btc_spot", "--live"])
    assert error.value.code == 2


def test_cli_paper_run_and_account_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from prediction_trader.trading.catalog import get_instrument
    from prediction_trader.trading.models import Quote

    def snapshot(self: PublicMarketData, instrument_id: str, portfolio_id: str = "default"):
        return get_instrument(instrument_id), Quote(
            instrument_id,
            portfolio_id,
            datetime.now(UTC),
            D("100"),
            D("100"),
            D("10"),
            D("10"),
        )

    monkeypatch.setattr(PublicMarketData, "snapshot", snapshot)
    database = str(tmp_path / "cli.db")
    command = [
        "paper-run",
        "binance_btc_spot",
        "--portfolio",
        "cli",
        "--side",
        "buy",
        "--quantity",
        "1",
        "--fee-bps",
        "10",
        "--client-order-id",
        "test-order",
        "--db",
        database,
    ]
    main(command)
    assert json.loads(capsys.readouterr().out)["account"]["cash"] == "899.9"

    def offline(*args, **kwargs):
        raise AssertionError("Committed quantity order retry must not require network")

    monkeypatch.setattr(PublicMarketData, "snapshot", offline)
    main(command)
    assert json.loads(capsys.readouterr().out)["account"]["fill_count"] == 1
    main(["account", "cli", "--db", database])
    payload = json.loads(capsys.readouterr().out)
    assert payload["account"]["cash"] == "899.9"
    assert len(payload["history"]) == 1


def test_cli_missing_fees_and_nonfinite_values_are_rejected() -> None:
    for options in ([], ["--fee-bps", "NaN"], ["--fee-bps", "Infinity"]):
        with pytest.raises(SystemExit) as error:
            main(
                [
                    "paper-run",
                    "binance_btc_spot",
                    "--side",
                    "buy",
                    "--quantity",
                    "1",
                    "--client-order-id",
                    "one",
                    *options,
                ]
            )
        assert error.value.code == 2


def test_cli_funding_retry_returns_prior_event_without_refetching_quote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from prediction_trader.trading.catalog import get_instrument
    from prediction_trader.trading.models import Quote

    calls = []

    def snapshot(self: PublicMarketData, instrument_id: str, portfolio_id: str = "default"):
        calls.append(instrument_id)
        return get_instrument(instrument_id), Quote(
            instrument_id,
            portfolio_id,
            datetime.now(UTC),
            D("100"),
            D("100"),
            D("10"),
            D("10"),
            mark_price=D("100"),
        )

    monkeypatch.setattr(PublicMarketData, "snapshot", snapshot)
    database = str(tmp_path / "funding.db")
    main(
        [
            "paper-run",
            "binance_btc_perpetual",
            "--portfolio",
            "f",
            "--side",
            "buy",
            "--quantity",
            "1",
            "--fee-bps",
            "0",
            "--client-order-id",
            "open",
            "--db",
            database,
        ]
    )
    capsys.readouterr()
    funding = [
        "funding",
        "binance_btc_perpetual",
        "--portfolio",
        "f",
        "--payment-id",
        "rate-1",
        "--rate",
        "0.001",
        "--db",
        database,
    ]
    main(funding)
    assert D(json.loads(capsys.readouterr().out)["account"]["cash"]) == D("999.9")
    main(funding)
    assert D(json.loads(capsys.readouterr().out)["account"]["cash"]) == D("999.9")
    assert len(calls) == 2
    with pytest.raises(SystemExit):
        main([*funding, "--rate", "0.002"])
