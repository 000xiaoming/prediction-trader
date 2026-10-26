"""Immutable, explicit market profiles; one profile per running trader process."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal

DEFAULT_PROFILE = "polymarket_btc_5m"


@dataclass(frozen=True, slots=True)
class MarketProfile:
    id: str
    label: str
    asset: Literal["BTC", "ETH"]
    duration_seconds: int
    slug_prefix: str
    question_prefix: str
    chainlink_symbol: str
    reference_symbol: str
    binance_symbol: str
    research_symbol: str
    strategy_id: str = "remaining_time_v1"
    venue: str = "polymarket"
    market_kind: str = "crypto_up_down"
    legacy_replay_supported: bool = False

    def window_start(self, now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Market windows require a timezone-aware timestamp")
        bucket = int(now.timestamp()) // self.duration_seconds * self.duration_seconds
        return datetime.fromtimestamp(bucket, UTC)

    def slug_at(self, now: datetime) -> str:
        return f"{self.slug_prefix}-{int(self.window_start(now).timestamp())}"

    def matches_market(self, slug: str, end_date: datetime | None) -> bool:
        if end_date is None or end_date.tzinfo is None or end_date.utcoffset() is None:
            return False
        prefix = f"{self.slug_prefix}-"
        if not slug.startswith(prefix):
            return False
        suffix = slug[len(prefix) :]
        if not suffix.isascii() or not suffix.isdecimal() or len(suffix) > 12:
            return False
        bucket = int(suffix)
        if str(bucket) != suffix or bucket % self.duration_seconds:
            return False
        try:
            expected_end = datetime.fromtimestamp(bucket, UTC) + timedelta(
                seconds=self.duration_seconds
            )
        except (ValueError, OverflowError, OSError):
            return False
        return end_date == expected_end

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _up_down(asset: Literal["BTC", "ETH"], minutes: int) -> MarketProfile:
    symbol = asset.lower()
    return MarketProfile(
        id=f"polymarket_{symbol}_{minutes}m",
        label=f"{asset} {minutes}分钟 Up/Down",
        asset=asset,
        duration_seconds=minutes * 60,
        slug_prefix=f"{symbol}-updown-{minutes}m",
        question_prefix=f"{'Bitcoin' if asset == 'BTC' else 'Ethereum'} Up or Down",
        chainlink_symbol=f"{symbol}/usd",
        reference_symbol=f"{asset}/USD",
        binance_symbol=f"{symbol}usdt",
        research_symbol=f"{asset}-USD",
        legacy_replay_supported=asset == "BTC" and minutes == 5,
    )


PROFILES = MappingProxyType(
    {
        profile.id: profile
        for profile in (_up_down("BTC", 5), _up_down("BTC", 15), _up_down("ETH", 5))
    }
)


def get_profile(profile_id: str) -> MarketProfile:
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise ValueError(f"Unknown trading profile; choose one of: {', '.join(PROFILES)}") from None


def list_profiles() -> tuple[MarketProfile, ...]:
    return tuple(PROFILES.values())
