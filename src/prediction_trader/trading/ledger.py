"""Transactional SQLite paper ledger, partitioned by explicit portfolio namespace."""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

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


class SQLitePaperLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
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
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def account(self, portfolio_id: str) -> PaperAccount | None:
        identifier(portfolio_id, "portfolio_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM paper_accounts WHERE portfolio_id=?",
                (portfolio_id,),
            ).fetchone()
        return None if row is None else PaperAccount.from_state(json.loads(row[0]))

    def history(self, portfolio_id: str, limit: int = 100) -> list[dict[str, Any]]:
        identifier(portfolio_id, "portfolio_id")
        if not 1 <= limit <= 1000:
            raise TradingError("History limit must be in [1, 1000]")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence,event_type,observed_at,payload FROM paper_events "
                "WHERE portfolio_id=? ORDER BY sequence DESC LIMIT ?",
                (portfolio_id, limit),
            ).fetchall()
        return [
            dict(sequence=row[0], event_type=row[1], observed_at=row[2], payload=json.loads(row[3]))
            for row in rows
        ]

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
