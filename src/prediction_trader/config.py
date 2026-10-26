from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from prediction_trader.profiles import DEFAULT_PROFILE, MarketProfile, get_profile


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    trading_mode: TradingMode = TradingMode.PAPER
    trading_profile: str = DEFAULT_PROFILE
    enabled_trading_profiles: str = ""
    _profile_overrides: tuple[str, ...] = PrivateAttr(default=())
    log_level: str = "INFO"
    metrics_host: str = "0.0.0.0"
    metrics_port: int = 8000
    database_url: str = "postgresql+psycopg://prediction:change-me@postgres:5432/prediction"
    max_market_exposure_usd: Decimal = Field(default=Decimal("10"), gt=0)
    max_total_exposure_usd: Decimal = Field(default=Decimal("25"), gt=0)
    max_daily_loss_usd: Decimal = Field(default=Decimal("10"), gt=0)
    max_signal_age_seconds: int = Field(default=3, gt=0)
    min_net_edge: Decimal = Field(default=Decimal("0.02"), gt=0, lt=1)
    default_target_notional_usd: Decimal = Field(default=Decimal("2"), gt=0)
    polymarket_discovery_enabled: bool = True
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_discovery_interval_seconds: int = Field(default=60, ge=10)
    polymarket_discovery_page_size: int = Field(default=100, ge=1, le=500)
    polymarket_market_filter: str = "bitcoin"
    polymarket_search_query: str = ""
    polymarket_series_slug_prefix: str = "btc-updown-5m"
    polymarket_websocket_enabled: bool = True
    polymarket_market_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_websocket_refresh_seconds: int = Field(default=15, ge=10)
    binance_reference_enabled: bool = True
    binance_symbol: str = "btcusdt"
    binance_market_ws_base_url: str = "wss://data-stream.binance.vision/ws"
    binance_persist_interval_seconds: float = Field(default=1.0, ge=0.1)
    candle_refresh_seconds: int = Field(default=10, ge=2)
    cross_venue_lookback_seconds: int = Field(default=60, ge=10)
    cross_venue_min_btc_move: Decimal = Field(default=Decimal("0.0005"), gt=0)
    cross_venue_probability_sensitivity: Decimal = Field(default=Decimal("25"), gt=0)
    replay_order_latency_ms: int = Field(default=250, ge=0)
    replay_max_fill_wait_ms: int = Field(default=2000, gt=0)
    replay_slippage_bps: Decimal = Field(default=Decimal("10"), ge=0)
    replay_notional_usd: Decimal = Field(default=Decimal("2"), gt=0)
    settlement_sync_enabled: bool = True
    settlement_sync_interval_seconds: int = Field(default=60, ge=30)
    polymarket_rtds_enabled: bool = True
    polymarket_rtds_url: str = "wss://ws-live-data.polymarket.com"
    polymarket_chainlink_symbol: str = "btc/usd"
    polymarket_chainlink_twap_window_seconds: Literal[30, 60] = 60
    indicator_host: str = "0.0.0.0"
    indicator_port: int = 8080
    indicator_cache_ttl_seconds: float = Field(default=0.5, gt=0, le=5)
    indicator_max_reference_age_seconds: float = Field(default=5, gt=0, le=60)
    indicator_max_quote_age_seconds: float = Field(default=5, gt=0, le=60)
    indicator_opening_tolerance_seconds: float = Field(default=2, ge=0, le=10)
    research_context_path: str = ""
    tradingagents_research_dir: str = ""
    profile_backtests_enabled: bool = False
    profile_backtest_interval_seconds: int = Field(default=300, ge=60)
    profile_backtest_lookback_hours: int = Field(default=24, ge=1, le=168)
    paper_ledger_path: str = "data/paper-ledger.sqlite3"
    paper_initial_cash: Decimal = Field(default=Decimal("1000"), gt=0, le=1000000)

    @field_validator("trading_profile")
    @classmethod
    def known_profile(cls, value: str) -> str:
        get_profile(value)
        return value

    @field_validator("enabled_trading_profiles")
    @classmethod
    def known_enabled_profiles(cls, value: str) -> str:
        if not value.strip():
            return ""
        profile_ids = tuple(dict.fromkeys(item.strip() for item in value.split(",")))
        for profile_id in profile_ids:
            get_profile(profile_id)
        return ",".join(profile_ids)

    @model_validator(mode="after")
    def derive_market_settings(self) -> Self:
        profile = self.profile
        derived = {
            "polymarket_market_filter": profile.question_prefix,
            "polymarket_search_query": "",
            "polymarket_series_slug_prefix": profile.slug_prefix,
            "polymarket_chainlink_symbol": profile.chainlink_symbol,
            "binance_symbol": profile.binance_symbol,
        }
        self._profile_overrides = tuple(
            name
            for name, value in derived.items()
            if name in self.model_fields_set and getattr(self, name) != value
        )
        for name, value in derived.items():
            object.__setattr__(self, name, value)
        return self

    @property
    def profile(self) -> MarketProfile:
        return get_profile(self.trading_profile)

    @property
    def enabled_profiles(self) -> tuple[MarketProfile, ...]:
        """Primary first, then configured profiles; primary is always enabled."""
        additional = (
            self.enabled_trading_profiles.split(",") if self.enabled_trading_profiles else []
        )
        return tuple(
            get_profile(profile_id)
            for profile_id in dict.fromkeys((self.trading_profile, *additional))
        )

    def for_profile(self, profile_id: str) -> Self:
        """Return an independent, validated, immutable view for one enabled profile."""
        if profile_id not in {profile.id for profile in self.enabled_profiles}:
            raise ValueError(f"Trading profile is not enabled: {profile_id}")
        values = self.model_dump()
        values["trading_profile"] = profile_id
        # Preserve the complete enabled set when the primary changes in the view.
        values["enabled_trading_profiles"] = ",".join(p.id for p in self.enabled_profiles)
        profile = get_profile(profile_id)
        values.update(
            polymarket_market_filter=profile.question_prefix,
            polymarket_search_query="",
            polymarket_series_slug_prefix=profile.slug_prefix,
            polymarket_chainlink_symbol=profile.chainlink_symbol,
            binance_symbol=profile.binance_symbol,
        )
        values["_env_file"] = None
        return type(self)(**values)

    @property
    def profile_overrides(self) -> tuple[str, ...]:
        return self._profile_overrides

    @property
    def resolved_research_context_path(self) -> str:
        return self.research_context_path.replace("{symbol}", self.profile.research_symbol.lower())
