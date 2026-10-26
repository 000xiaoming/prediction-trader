import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from prediction_trader.config import Settings
from prediction_trader.profile_cli import main
from prediction_trader.profiles import DEFAULT_PROFILE, MarketProfile, list_profiles
from prediction_trader.strategies.indicator_models import get_indicator_model


@pytest.mark.parametrize("profile", list_profiles(), ids=lambda profile: profile.id)
def test_profile_scopes_windows_and_contracts(profile: MarketProfile) -> None:
    now = datetime(2026, 9, 19, 12, 7, 59, tzinfo=UTC)
    start = profile.window_start(now)
    end = start + timedelta(seconds=profile.duration_seconds)
    assert start <= now < end
    assert start.minute == (0 if profile.duration_seconds == 900 else 5)
    assert profile.slug_at(now) == profile.slug_at(now.astimezone(timezone(timedelta(hours=8))))
    assert profile.matches_market(profile.slug_at(now), end)
    assert not profile.matches_market(profile.slug_at(now), end + timedelta(seconds=1))
    assert not profile.matches_market(profile.slug_at(now) + "-other", end)
    assert not profile.matches_market(profile.slug_at(now), end.replace(tzinfo=None))
    assert not profile.matches_market(profile.slug_at(now), None)
    assert profile.slug_at(end) != profile.slug_at(now)
    for other in list_profiles():
        if other.id != profile.id:
            assert not profile.matches_market(other.slug_at(now), end)
    with pytest.raises(ValueError, match="timezone-aware"):
        profile.window_start(now.replace(tzinfo=None))
    with pytest.raises(FrozenInstanceError):
        profile.asset = "wrong"
    assert get_indicator_model(profile.strategy_id) is not None


def test_selected_profile_overrides_leftover_btc_environment_fields(tmp_path: Path) -> None:
    config = tmp_path / "legacy.env"
    config.write_text(
        "BINANCE_SYMBOL=btcusdt\nPOLYMARKET_CHAINLINK_SYMBOL=btc/usd\n"
        "POLYMARKET_SERIES_SLUG_PREFIX=btc-updown-5m\n"
        "POLYMARKET_MARKET_FILTER=bitcoin\nPOLYMARKET_SEARCH_QUERY=bitcoin\n"
    )
    settings = Settings(
        _env_file=config,
        trading_profile="polymarket_eth_5m",
        research_context_path="/research/{symbol}.json",
    )
    assert settings.binance_symbol == "ethusdt"
    assert settings.polymarket_chainlink_symbol == "eth/usd"
    assert settings.polymarket_series_slug_prefix == "eth-updown-5m"
    assert settings.polymarket_market_filter == "Ethereum Up or Down"
    assert settings.polymarket_search_query == ""
    assert len(settings.profile_overrides) == 5
    assert settings.resolved_research_context_path == "/research/eth-usd.json"
    assert "btcusdt" in config.read_text()  # Loading does not mutate the operator's file.


def test_default_profile_and_invalid_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_PROFILE", raising=False)
    settings = Settings(_env_file=None)
    assert settings.profile.id == DEFAULT_PROFILE
    assert settings.profile.legacy_replay_supported
    assert settings.resolved_research_context_path == ""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trading_profile="typo")
    with pytest.raises(ValueError, match="Unknown indicator"):
        get_indicator_model("made-up-strategy")


def test_profile_cli_exposes_only_allowlisted_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://private:secret@private-db:5432/private")
    monkeypatch.setenv("TRADING_PROFILE", "polymarket_eth_5m")
    main(["check"])
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["profile"]["asset"] == "ETH"
    assert result["switching"] == "restart_required"
    assert result["live_execution"] is False
    assert "private" not in output
    assert "secret" not in output
    main(["list"])
    assert len(json.loads(capsys.readouterr().out)["profiles"]) == 3
    main(["show", "polymarket_btc_15m"])
    assert json.loads(capsys.readouterr().out)["profile"]["duration_seconds"] == 900
    with pytest.raises(SystemExit) as error:
        main(["show", "missing-profile"])
    assert error.value.code == 2


def test_preflight_rejects_live_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    with pytest.raises(SystemExit) as error:
        main(["check"])
    assert error.value.code == 2
    assert "Live execution is unavailable" in capsys.readouterr().err
