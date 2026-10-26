import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from prediction_trader.profiles import MarketProfile


@dataclass(frozen=True, slots=True)
class DiscoveredMarket:
    event_id: str
    event_slug: str
    event_title: str
    market_id: str
    market_slug: str
    question: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    end_date: datetime | None
    raw: Mapping[str, Any]


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _parse_tokens(
    market: Mapping[str, Any], *, require_up_down: bool = False
) -> tuple[str, str] | None:
    raw_tokens = market.get("clobTokenIds")
    if isinstance(raw_tokens, str):
        try:
            raw_tokens = json.loads(raw_tokens)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw_tokens, list) or len(raw_tokens) != 2:
        return None
    yes_token, no_token = raw_tokens
    if (
        not isinstance(yes_token, str)
        or not isinstance(no_token, str)
        or not yes_token
        or not no_token
        or yes_token == no_token
    ):
        return None
    raw_outcomes = market.get("outcomes")
    if raw_outcomes is None:
        # Preserve legacy binary fixtures; scoped discovery always requires labels.
        return None if require_up_down else (yes_token, no_token)
    if isinstance(raw_outcomes, str):
        try:
            raw_outcomes = json.loads(raw_outcomes)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw_outcomes, list) or len(raw_outcomes) != 2:
        return None
    if any(not isinstance(label, str) for label in raw_outcomes):
        return None
    labels = [label.strip().casefold() for label in raw_outcomes]
    expected = ("up", "down") if require_up_down or "up" in labels else ("yes", "no")
    if set(labels) != set(expected):
        return None
    return raw_tokens[labels.index(expected[0])], raw_tokens[labels.index(expected[1])]


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def parse_events(
    payload: object, market_filter: str = "", *, profile: MarketProfile | None = None
) -> list[DiscoveredMarket]:
    events = payload.get("events", []) if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise ValueError("Gamma response must contain an events list")

    needle = market_filter.casefold().strip()
    discovered: list[DiscoveredMarket] = []
    for raw_event in events:
        if not isinstance(raw_event, dict):
            continue
        event_id = _text(raw_event.get("id"))
        title = _text(raw_event.get("title"))
        slug = _text(raw_event.get("slug"))
        markets = raw_event.get("markets", [])
        if not isinstance(markets, list):
            continue
        for raw_market in markets:
            if not isinstance(raw_market, dict):
                continue
            question = _text(raw_market.get("question"))
            market_slug = _text(raw_market.get("slug"))
            searchable = f"{title} {slug} {question} {market_slug}".casefold()
            if needle and needle not in searchable:
                continue
            end_date = _parse_datetime(raw_market.get("endDate") or raw_event.get("endDate"))
            if profile is not None and (
                not profile.matches_market(market_slug, end_date)
                or slug != market_slug
                or not question.casefold().startswith(profile.question_prefix.casefold())
            ):
                continue
            tokens = _parse_tokens(raw_market, require_up_down=profile is not None)
            market_id = _text(raw_market.get("id"))
            if not event_id or not market_id or tokens is None:
                continue
            discovered.append(
                DiscoveredMarket(
                    event_id=event_id,
                    event_slug=slug,
                    event_title=title,
                    market_id=market_id,
                    market_slug=market_slug,
                    question=question,
                    condition_id=_text(raw_market.get("conditionId")),
                    yes_token_id=tokens[0],
                    no_token_id=tokens[1],
                    end_date=end_date,
                    raw=raw_market,
                )
            )
    return discovered


class GammaClient:
    def __init__(self, base_url: str, timeout_seconds: float = 20) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "prediction-trader/0.1 (paper research)"},
        )

    def list_active_events(self, limit: int) -> object:
        response = self._client.get(
            "/events/keyset", params={"closed": "false", "limit": limit}
        )
        response.raise_for_status()
        return response.json()

    def search_events(self, query: str) -> list[object]:
        response = self._client.get("/public-search", params={"q": query})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Gamma search response must be an object")
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise ValueError("Gamma search response must contain an events list")
        return events

    def get_event(self, event_id: str) -> object:
        response = self._client.get(f"/events/{event_id}")
        response.raise_for_status()
        return response.json()

    def get_event_by_slug(self, slug: str) -> object | None:
        response = self._client.get(f"/events/slug/{slug}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload: object = response.json()
        return payload

    def get_market(self, market_id: str) -> object:
        response = self._client.get(f"/markets/{market_id}")
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()
