import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from prediction_trader.research.context import FileResearchContextProvider
from prediction_trader.research.export_cli import context_from_full_state, export_full_state, main

NOW = datetime(2026, 9, 19, 6, 0, tzinfo=UTC)
GENERATED = NOW - timedelta(minutes=2)
AS_OF = NOW - timedelta(minutes=3)


def full_state(rating: str = "Overweight", horizon: str = "3-6 months") -> dict[str, object]:
    # This is the exact flat shape and render_pm_decision section order used by
    # TradingAgentsGraph._log_state. No invented final_decision object is needed.
    return {
        "company_of_interest": "BTC-USD",
        "trade_date": "2026-09-19",
        "market_report": "Earlier source report: Sell is discussed as an alternative.",
        "final_trade_decision": (
            f"**Rating**: {rating}\n\n"
            "**Executive Summary**: A source portfolio allocation view.\n\n"
            "**Investment Thesis**: Conflicting Buy and Sell arguments were considered.\n\n"
            "**Price Target**: not provided\n\n"
            f"**Time Horizon**: {horizon}"
        ),
    }


def make_context(state: dict[str, object], **overrides: object) -> object:
    parameters: dict[str, object] = {
        "data_as_of": AS_OF,
        "generated_at": GENERATED,
        "available_at": NOW,
        "ttl_seconds": 900,
        "source_run_id": "test-run",
    }
    parameters.update(overrides)
    return context_from_full_state(state, **parameters)  # type: ignore[arg-type]


def test_export_reads_actual_full_states_format_and_preserves_time(tmp_path: Path) -> None:
    source = tmp_path / "full_states_log_2026-09-19.json"
    source.write_text(json.dumps(full_state()))
    output = tmp_path / "context.json"
    context = export_full_state(
        source,
        output,
        data_as_of=AS_OF,
        generated_at=GENERATED,
        now=NOW,
    )
    assert context.rating == "Overweight"
    assert context.rationale == "A source portfolio allocation view."
    assert context.time_horizon == "3-6 months"
    assert context.generated_at == GENERATED
    assert context.available_at == NOW
    assert context.valid_until == GENERATED + timedelta(seconds=900)
    assert context.source_run_id.startswith("sha256:")
    assert FileResearchContextProvider(output).read(NOW)["status"] == "available"


def test_source_timestamp_cannot_be_overridden_or_inferred_from_mtime(tmp_path: Path) -> None:
    source = tmp_path / "full_states.json"
    output = tmp_path / "context.json"
    source.write_text(json.dumps(full_state()))
    with pytest.raises(ValueError, match="actual completion"):
        export_full_state(source, output, data_as_of=AS_OF, now=NOW)
    state = full_state()
    state["generated_at"] = GENERATED.isoformat()
    source.write_text(json.dumps(state))
    context = export_full_state(source, output, data_as_of=AS_OF, now=NOW)
    assert context.generated_at == GENERATED
    with pytest.raises(ValueError, match="must match"):
        export_full_state(source, output, data_as_of=AS_OF, generated_at=NOW, now=NOW)


@pytest.mark.parametrize("rating", ["REVIEW", "BUY", "Buy or Sell", "Unknown"])
def test_rejects_noncanonical_rating_even_if_prose_mentions_valid_one(rating: str) -> None:
    with pytest.raises(ValueError, match="rating"):
        make_context(full_state(rating))


@pytest.mark.parametrize(
    "decision",
    [
        "Buy",
        "**Rating**: Buy",
        {"rating": "Buy", "executive_summary": "Not the upstream log format"},
        full_state()["final_trade_decision"] + "\n\n**Rating**: Sell",
    ],
)
def test_rejects_ambiguous_or_noncanonical_decision(decision: object) -> None:
    state = full_state()
    state["final_trade_decision"] = decision
    with pytest.raises(ValueError):
        make_context(state)


def test_rejects_wrong_ticker_and_redated_historical_run() -> None:
    state = full_state()
    state["company_of_interest"] = "ETH-USD"
    with pytest.raises(ValueError, match="BTC-USD"):
        make_context(state)
    state = full_state()
    state["trade_date"] = "2026-09-18"
    with pytest.raises(ValueError, match="do not redate"):
        make_context(state)


@pytest.mark.parametrize("ticker", ["BTC-USD", "ETH", "eth-usd", "SOL-USD"])
def test_eth_export_rejects_other_tickers_and_aliases(ticker: str) -> None:
    state = full_state()
    state["company_of_interest"] = ticker
    with pytest.raises(ValueError, match="company_of_interest must be ETH-USD"):
        make_context(state, symbol="ETH-USD")


def test_export_rejects_unsupported_requested_symbol() -> None:
    with pytest.raises(ValueError, match="symbol must be BTC-USD or ETH-USD"):
        make_context(full_state(), symbol="SOL-USD")


def test_export_preserves_eth_symbol_and_timestamps(tmp_path: Path) -> None:
    source = tmp_path / "full_states.json"
    state = full_state()
    state["company_of_interest"] = "ETH-USD"
    source.write_text(json.dumps(state))
    output = tmp_path / "eth.json"
    context = export_full_state(
        source,
        output,
        symbol="ETH-USD",
        data_as_of=AS_OF,
        generated_at=GENERATED,
        now=NOW,
    )
    assert context.symbol == "ETH-USD"
    assert context.generated_at == GENERATED
    assert context.valid_until == GENERATED + timedelta(seconds=900)
    assert FileResearchContextProvider(output, expected_symbol="ETH-USD").read(NOW) == {
        "status": "available",
        "context": context.model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"ttl_seconds": 0},
        {"ttl_seconds": 3601},
        {"ttl_seconds": True},
        {"ttl_seconds": 1.5},
        {"generated_at": None},
        {"generated_at": GENERATED.replace(tzinfo=None)},
        {"generated_at": NOW + timedelta(seconds=1)},
        {"data_as_of": NOW + timedelta(seconds=1)},
        {"data_as_of": NOW - timedelta(hours=2)},
        {"generated_at": NOW - timedelta(minutes=16), "data_as_of": NOW - timedelta(minutes=17)},
    ],
)
def test_rejects_invalid_future_expired_or_missing_source_metadata(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        make_context(full_state(), **changes)


def test_failed_export_preserves_previous_valid_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "full_states.json"
    output = tmp_path / "context.json"
    source.write_text(json.dumps(full_state()))
    export_full_state(source, output, data_as_of=AS_OF, generated_at=GENERATED, now=NOW)
    previous = output.read_bytes()
    source.write_text(json.dumps(full_state("REVIEW")))
    with pytest.raises(ValueError):
        export_full_state(source, output, data_as_of=AS_OF, generated_at=GENERATED, now=NOW)
    assert output.read_bytes() == previous


def test_cli_reports_validation_failure_without_report_contents(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.json"
    state = full_state()
    state["final_trade_decision"] = "secret-token-in-malformed-source"
    source.write_text(json.dumps(state))
    with pytest.raises(SystemExit) as error:
        main(
            [
                str(source),
                "--output",
                str(tmp_path / "result.json"),
                "--data-as-of",
                AS_OF.isoformat(),
                "--generated-at",
                GENERATED.isoformat(),
            ]
        )
    assert error.value.code == 2
    assert "secret-token-in-malformed-source" not in capsys.readouterr().err


def test_cli_requires_data_coverage_and_offers_no_now_default_for_source_time(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as error:
        main([str(tmp_path / "source.json"), "--output", str(tmp_path / "result.json")])
    assert error.value.code == 2


def test_cli_success_calls_no_tradingagents_or_network(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "result.json"
    source.write_text(json.dumps(full_state(horizon="not provided")))
    with patch("prediction_trader.research.export_cli.datetime") as clock:
        clock.now.return_value = NOW
        main(
            [
                str(source),
                "--output",
                str(output),
                "--data-as-of",
                AS_OF.isoformat(),
                "--generated-at",
                GENERATED.isoformat(),
                "--ttl-seconds",
                "600",
            ]
        )
    result = FileResearchContextProvider(output).read(NOW)
    assert result["status"] == "available"
    assert result["context"]["time_horizon"] is None


def test_cli_exports_explicit_eth_source_without_relabeling(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "result.json"
    state = full_state()
    state["company_of_interest"] = "ETH-USD"
    source.write_text(json.dumps(state))
    arguments = [
        str(source),
        "--output", str(output),
        "--symbol", "ETH-USD",
        "--data-as-of", AS_OF.isoformat(),
        "--generated-at", GENERATED.isoformat(),
    ]
    with patch("prediction_trader.research.export_cli.datetime") as clock:
        clock.now.return_value = NOW
        main(arguments)
        result = FileResearchContextProvider(output, expected_symbol="ETH-USD").read(NOW)
        assert result["status"] == "available"
        assert result["context"]["symbol"] == "ETH-USD"
        previous = output.read_bytes()
        source.write_text(json.dumps(full_state()))
        with pytest.raises(SystemExit) as error:
            main(arguments)
        assert error.value.code == 2
        assert output.read_bytes() == previous


def test_export_uses_explicit_producer_calendar_across_utc_midnight(tmp_path: Path) -> None:
    state = full_state()
    state["trade_date"] = "2026-09-20"
    source = tmp_path / "full_states.json"
    source.write_text(json.dumps(state))
    output = tmp_path / "context.json"
    generated = datetime.fromisoformat("2026-09-20T00:05:00+08:00")
    now = generated + timedelta(minutes=1)
    context = export_full_state(
        source,
        output,
        data_as_of="2026-09-20T00:04:00+08:00",
        generated_at=generated,
        source_timezone="Asia/Singapore",
        now=now,
    )
    assert context.generated_at == datetime(2026, 9, 19, 16, 5, tzinfo=UTC)
    assert context.available_at == now.astimezone(UTC)
    assert FileResearchContextProvider(output).read(now)["status"] == "available"
    # The default remains UTC even when the exporter host runs Singapore time.
    with pytest.raises(ValueError, match="do not redate"):
        export_full_state(
            source,
            output,
            data_as_of=generated,
            generated_at=generated,
            now=now,
        )


def test_explicit_source_timezone_cannot_relabel_old_trade_date() -> None:
    state = full_state()
    state["trade_date"] = "2026-09-19"
    with pytest.raises(ValueError, match="do not redate"):
        make_context(
            state,
            data_as_of="2026-09-20T00:04:00+08:00",
            generated_at="2026-09-20T00:05:00+08:00",
            available_at=datetime.fromisoformat("2026-09-20T00:06:00+08:00"),
            source_timezone="Asia/Singapore",
        )


@pytest.mark.parametrize("zone", ["Unknown/Secret-Token", "", "/etc/passwd"])
def test_rejects_invalid_producer_timezone_without_echoing_input(zone: str) -> None:
    with pytest.raises(ValueError, match="available IANA timezone") as error:
        make_context(full_state(), source_timezone=zone)
    if zone:
        assert zone not in str(error.value)


def test_cli_threads_explicit_source_timezone_to_exporter(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "result.json"
    state = full_state()
    state["trade_date"] = "2026-09-20"
    source.write_text(json.dumps(state))
    now = datetime.fromisoformat("2026-09-20T00:06:00+08:00")
    with patch("prediction_trader.research.export_cli.datetime") as clock:
        clock.now.return_value = now
        main(
            [
                str(source),
                "--output",
                str(output),
                "--data-as-of",
                "2026-09-20T00:04:00+08:00",
                "--generated-at",
                "2026-09-20T00:05:00+08:00",
                "--source-timezone",
                "Asia/Singapore",
            ]
        )
    assert FileResearchContextProvider(output).read(now)["status"] == "available"
