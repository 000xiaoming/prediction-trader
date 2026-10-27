"""Transactional SQLite paper ledger, partitioned by explicit portfolio namespace."""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict, TypeVar

from prediction_trader.trading.engine import PaperAccount
from prediction_trader.trading.models import (
    Fill,
    InstrumentSpec,
    PaperOrder,
    Quote,
    TradingError,
    identifier,
    jsonable,
)

T = TypeVar("T")
EVENT_TYPES = ("fill", "mark", "funding", "settlement")


class HistoryPage(TypedDict):
    portfolio_id: str
    events: list[dict[str, Any]]
    next_before: int | None


def _history_filter(portfolio_id: str, event_type: str | None) -> tuple[str, list[Any]]:
    identifier(portfolio_id, "portfolio_id")
    if event_type is not None and event_type not in EVENT_TYPES:
        raise TradingError("Unknown history event type")
    where = "portfolio_id=?"
    values: list[Any] = [portfolio_id]
    if event_type is not None:
        where += " AND event_type=?"
        values.append(event_type)
    return where, values


def _history_event(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(sequence=row[0], event_type=row[1], observed_at=row[2], payload=json.loads(row[3]))


class SQLitePaperLedger:
    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS paper_accounts (
                    portfolio_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_id TEXT NOT NULL REFERENCES paper_accounts(portfolio_id),
                    event_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(portfolio_id, event_key)
                );
                CREATE INDEX IF NOT EXISTS ix_paper_events_portfolio
                    ON paper_events(portfolio_id, sequence);
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro" if self.read_only else str(self.path),
            timeout=15,
            uri=self.read_only,
        )
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def account(self, portfolio_id: str) -> PaperAccount | None:
        identifier(portfolio_id, "portfolio_id")
        if self.read_only and not self.path.exists():
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM paper_accounts WHERE portfolio_id=?",
                (portfolio_id,),
            ).fetchone()
        return None if row is None else PaperAccount.from_state(json.loads(row[0]))

    def history(self, portfolio_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.history_page(portfolio_id, limit=limit)["events"]

    def history_page(
        self, portfolio_id: str, *, limit: int = 50, before: int | None = None,
        event_type: str | None = None,
    ) -> HistoryPage:
        where, values = _history_filter(portfolio_id, event_type)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise TradingError("History limit must be in [1, 1000]")
        if before is not None:
            if type(before) is not int or not 1 <= before <= 2**63 - 1:
                raise TradingError("History before must be a positive SQLite sequence number")
            where += " AND sequence<?"
            values.append(before)
        result: HistoryPage = {"portfolio_id": portfolio_id, "events": [], "next_before": None}
        if self.read_only and not self.path.exists():
            return result
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence,event_type,observed_at,payload FROM paper_events "
                f"WHERE {where} ORDER BY sequence DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        result["events"] = [_history_event(row) for row in rows[:limit]]
        if len(rows) > limit:
            result["next_before"] = rows[limit - 1][0]
        return result

    def iter_history(
        self, portfolio_id: str, *, event_type: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream all matching events oldest first from one consistent SQLite snapshot."""
        where, values = _history_filter(portfolio_id, event_type)
        if self.read_only and not self.path.exists():
            return
        with self._connect() as connection:
            connection.execute("BEGIN")
            cursor = connection.execute(
                "SELECT sequence,event_type,observed_at,payload FROM paper_events "
                f"WHERE {where} ORDER BY sequence ASC", values,
            )
            while rows := cursor.fetchmany(1000):
                for row in rows:
                    yield _history_event(row)

    def _transaction(
        self,
        portfolio_id: str,
        event_key: str,
        event_type: str,
        payload: dict[str, Any],
        operation: Callable[[PaperAccount], T],
        *,
        initial_cash: Decimal | None = None,
        currency: str = "USDT",
        now: datetime | None = None,
    ) -> tuple[T, PaperAccount]:
        identifier(portfolio_id, "portfolio_id")
        recorded_at = now or datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM paper_accounts WHERE portfolio_id=?",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                if initial_cash is None:
                    raise TradingError("Paper portfolio does not exist")
                account = PaperAccount(portfolio_id, initial_cash, currency)
            else:
                account = PaperAccount.from_state(json.loads(row[0]))
            result = operation(account)
            connection.execute(
                "INSERT INTO paper_accounts(portfolio_id,state,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(portfolio_id) DO UPDATE SET "
                "state=excluded.state,updated_at=excluded.updated_at",
                (
                    portfolio_id,
                    json.dumps(account.to_state(), sort_keys=True),
                    recorded_at.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO paper_events(portfolio_id,event_key,event_type,observed_at,payload) "
                "VALUES(?,?,?,?,?) ON CONFLICT(portfolio_id,event_key) DO NOTHING",
                (
                    portfolio_id,
                    event_key,
                    event_type,
                    recorded_at.isoformat(),
                    json.dumps(jsonable({**payload, "result": result}), sort_keys=True),
                ),
            )
            return result, account

    def execute(
        self,
        spec: InstrumentSpec,
        order: PaperOrder,
        quote: Quote,
        *,
        initial_cash: Decimal = Decimal("1000"),
        now: datetime | None = None,
    ) -> tuple[Fill, PaperAccount]:
        return self._transaction(
            order.portfolio_id,
            "order:" + order.client_order_id,
            "fill",
            {"instrument": jsonable(spec), "order": jsonable(order), "quote": jsonable(quote)},
            lambda account: account.execute(spec, order, quote, now=now),
            initial_cash=initial_cash,
            currency=spec.quote_currency,
            now=now,
        )

    def mark(self, quote: Quote, *, now: datetime | None = None) -> PaperAccount:
        fingerprint = hashlib.sha256(
            json.dumps(jsonable(quote), sort_keys=True).encode()
        ).hexdigest()
        _, account = self._transaction(
            quote.portfolio_id,
            "mark:" + fingerprint,
            "mark",
            {"quote": jsonable(quote)},
            lambda account: account.mark(quote, now=now),
            now=now,
        )
        return account

    def funding(
        self,
        payment_id: str,
        quote: Quote,
        rate: Decimal,
        *,
        now: datetime | None = None,
    ) -> tuple[Decimal, PaperAccount]:
        return self._transaction(
            quote.portfolio_id,
            "funding:" + payment_id,
            "funding",
            {"payment_id": payment_id, "quote": jsonable(quote), "rate": str(rate)},
            lambda account: account.apply_funding(payment_id, quote, rate, now=now),
            now=now,
        )

    def settle(
        self,
        portfolio_id: str,
        instrument_id: str,
        payload: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> tuple[Decimal, PaperAccount]:
        return self._transaction(
            portfolio_id,
            "settlement:" + instrument_id,
            "settlement",
            {"instrument_id": instrument_id, "gamma_market": payload},
            lambda account: account.settle_event(instrument_id, payload, now=now),
            now=now,
        )
