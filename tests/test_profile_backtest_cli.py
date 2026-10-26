import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from prediction_trader.backtesting import cli
from prediction_trader.backtesting.engine import BacktestParameters


@pytest.mark.parametrize(
    "arguments",
    [
        ["--start", "2026-09-19T12:00:00"],
        ["--end", "bad"],
        ["--slippage-bps", "NaN"],
        ["--stake-usd", "-1"],
        ["--max-markets", "5001"],
        ["--profile", "polymarket_btc_5m", "--decision-delay-seconds", "300"],
        [
            "--start",
            "2026-09-01T00:00:00Z",
            "--end",
            "2026-09-19T00:00:00Z",
            "--as-of",
            "2026-09-19T01:00:00Z",
        ],
    ],
)
def test_invalid_cli_fails_before_settings_or_database(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    settings = Mock(side_effect=AssertionError("Must validate before settings or DB"))
    monkeypatch.setattr(cli, "Settings", settings)
    with pytest.raises(SystemExit) as exc:
        cli.main(arguments)
    assert exc.value.code == 2
    settings.assert_not_called()


def test_cli_defaults_to_bounded_utc_history_and_writes_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: SimpleNamespace(database_url="test-url"))
    market_repository = Mock()
    monkeypatch.setattr(cli, "MarketRepository", Mock(return_value=market_repository))
    backtest_repository = Mock()
    backtest_repository.run.return_value = {"profile_id": "polymarket_eth_5m", "metrics": {}}
    monkeypatch.setattr(cli, "BacktestRepository", Mock(return_value=backtest_repository))
    output = tmp_path / "reports" / "eth.json"
    cli.main(["--profile", "polymarket_eth_5m", "--output", str(output)])
    profile, parameters = backtest_repository.run.call_args.args
    assert profile.id == "polymarket_eth_5m"
    assert isinstance(parameters, BacktestParameters)
    assert parameters.end == parameters.as_of
    assert parameters.end - parameters.start == timedelta(hours=24)
    assert parameters.as_of.utcoffset() == timedelta(0)
    assert json.loads(output.read_text()) == json.loads(capsys.readouterr().out)
    market_repository.close.assert_called_once()


def test_cli_closes_database_when_replay_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "Settings", lambda: SimpleNamespace(database_url="test-url"))
    market_repository = Mock()
    monkeypatch.setattr(cli, "MarketRepository", Mock(return_value=market_repository))
    backtest_repository = Mock()
    backtest_repository.run.side_effect = RuntimeError("unavailable")
    monkeypatch.setattr(cli, "BacktestRepository", Mock(return_value=backtest_repository))
    with pytest.raises(RuntimeError, match="unavailable"):
        cli.main([])
    market_repository.close.assert_called_once()
