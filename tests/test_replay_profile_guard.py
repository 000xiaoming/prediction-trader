from types import ModuleType
from unittest.mock import patch

import pytest

from prediction_trader import execution_replay_cli, replay_cli
from prediction_trader.config import Settings


@pytest.mark.parametrize("cli", [replay_cli, execution_replay_cli])
@pytest.mark.parametrize("profile_id", ["polymarket_eth_5m", "polymarket_btc_15m"])
def test_unsupported_profile_replay_fails_before_database_access(
    cli: ModuleType,
    profile_id: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(_env_file=None, trading_profile=profile_id)
    with (
        patch.object(cli, "Settings", return_value=settings),
        patch.object(cli, "MarketRepository") as repository,
        patch.object(cli, "load_signal_replay_frames") as load_frames,
        pytest.raises(SystemExit) as error,
    ):
        cli.main([])
    assert error.value.code == 2
    repository.assert_not_called()
    load_frames.assert_not_called()
    output = capsys.readouterr().err
    assert "legacy replay supports only polymarket_btc_5m" in output
    assert profile_id in output


@pytest.mark.parametrize("cli", [replay_cli, execution_replay_cli])
def test_replay_help_documents_scope_without_reading_settings(
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch.object(cli, "Settings") as settings,
        patch.object(cli, "MarketRepository") as repository,
        pytest.raises(SystemExit) as error,
    ):
        cli.main(["--help"])
    assert error.value.code == 0
    settings.assert_not_called()
    repository.assert_not_called()
    assert "Only the polymarket_btc_5m" in capsys.readouterr().out


@pytest.mark.parametrize("cli", [replay_cli, execution_replay_cli])
def test_btc_five_minute_replay_still_loads_and_closes_repository(cli: ModuleType) -> None:
    settings = Settings(_env_file=None, trading_profile="polymarket_btc_5m")
    # Stop after reading frames: the compatibility guard must permit this profile,
    # and existing cleanup must still run when a replay data read fails.
    with (
        patch.object(cli, "Settings", return_value=settings),
        patch.object(cli, "MarketRepository") as repository,
        patch.object(cli, "load_signal_replay_frames", side_effect=RuntimeError("read failed")),
        pytest.raises(RuntimeError, match="read failed"),
    ):
        cli.main([])
    repository.assert_called_once_with(settings.database_url)
    repository.return_value.close.assert_called_once_with()
