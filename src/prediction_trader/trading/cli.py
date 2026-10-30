"""Public quotes and durable, explicitly requested local paper actions."""

import argparse
import json
import os
import sqlite3
import sys
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from prediction_trader.trading.adapters import PublicMarketData
from prediction_trader.trading.backup import (
    backup_ledger,
    restore_backup,
    restore_stream,
    verify_backup,
)
from prediction_trader.trading.catalog import INSTRUMENTS
from prediction_trader.trading.history_export import write_csv, write_jsonl
from prediction_trader.trading.ledger import EVENT_TYPES, SQLitePaperLedger
from prediction_trader.trading.models import PaperOrder, TradingError, jsonable


def _decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Expected a decimal number") from exc
    if not number.is_finite():
        raise argparse.ArgumentTypeError("Expected a finite decimal number")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List explicit supported instruments")
    for name in ("quote", "paper-run", "mark", "settle", "funding"):
        command = commands.add_parser(name)
        command.add_argument("instrument_id")
        command.add_argument("--portfolio", default="default")
        if name == "quote":
            continue
        command.add_argument("--db", default=os.environ.get("PAPER_LEDGER_PATH", "data/paper.db"))
        if name == "funding":
            command.add_argument("--payment-id", required=True)
            command.add_argument(
                "--rate",
                type=_decimal,
                required=True,
                help="Explicit paper funding rate, e.g. 0.0001 = 0.01%%",
            )
        if name != "paper-run":
            continue
        command.add_argument("--side", choices=("buy", "sell"), required=True)
        quantity = command.add_mutually_exclusive_group(required=True)
        quantity.add_argument("--quantity", type=_decimal)
        quantity.add_argument("--stake", type=_decimal, help="Quote-currency notional before fees")
        command.add_argument(
            "--fee-bps",
            type=_decimal,
            required=True,
            help="Explicit paper flat-fee assumption; not venue fee discovery",
        )
        command.add_argument("--client-order-id", required=True)
        command.add_argument("--leverage", type=_decimal, default=Decimal("1"))
        command.add_argument("--reduce-only", action="store_true")
        command.add_argument("--initial-cash", type=_decimal, default=Decimal("1000"))
    account = commands.add_parser("account")
    account.add_argument("portfolio")
    account.add_argument("--db", default=os.environ.get("PAPER_LEDGER_PATH", "data/paper.db"))
    for name in ("history", "export"):
        command = commands.add_parser(
            name, help="Read recorded paper events without market requests",
        )
        command.add_argument("portfolio")
        command.add_argument("--db", default=os.environ.get("PAPER_LEDGER_PATH", "data/paper.db"))
        command.add_argument("--event-type", choices=EVENT_TYPES)
        if name == "history":
            command.add_argument("--limit", type=int, default=50)
            command.add_argument("--before", type=int)
        else:
            command.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    backup = commands.add_parser(
        "backup", help="Create a verified SQLite backup without overwriting",
    )
    backup.add_argument("destination")
    backup.add_argument("--db", default=os.environ.get("PAPER_LEDGER_PATH", "data/paper.db"))
    backup.add_argument(
        "--timeout", type=float, default=30, help="Maximum SQLite copy time in seconds",
    )
    verify = commands.add_parser("verify-backup", help="Validate a standalone paper-ledger backup")
    verify.add_argument("backup")
    restore = commands.add_parser(
        "restore", help="Restore a verified backup into a NEW ledger file",
    )
    restore.add_argument("backup", help="Backup filename, or - to read a backup from stdin")
    restore.add_argument("--output", required=True, help="New ledger filename; must not exist")
    restore.add_argument("--timeout", type=float, default=30)
    return parser


def _paper_order(args: argparse.Namespace, quantity: Decimal) -> PaperOrder:
    return PaperOrder(
        args.client_order_id,
        args.instrument_id,
        args.portfolio,
        args.side,
        quantity,
        args.fee_bps,
        args.leverage,
        args.reduce_only,
    )


def _retry_order(args: argparse.Namespace) -> dict[str, Any] | None:
    order = _paper_order(args, args.quantity)
    if not Path(args.db).is_file():
        return None
    account = SQLitePaperLedger(args.db).account(args.portfolio)
    prior = account.orders.get(order.client_order_id) if account else None
    if prior is None:
        return None
    if prior[0] != order:
        raise TradingError("Idempotency conflict: client_order_id has different payload")
    assert account is not None
    return {"fill": jsonable(prior[1]), "account": account.summary()}


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    result: object
    try:
        if args.command == "paper-run" and args.quantity is not None:
            retry = _retry_order(args)
            if retry is not None:
                print(json.dumps(retry, indent=2, ensure_ascii=False))
                return
        if args.command == "list":
            result = {
                "instruments": list(INSTRUMENTS.values()),
                "live_execution": False,
                "event_selection": "polymarket_event_<Gamma_numeric_market_id>_yes|no",
                "fees": "Every paper order requires an explicit flat fee_bps assumption.",
            }
        elif args.command == "backup":
            result = backup_ledger(args.db, args.destination, timeout_seconds=args.timeout)
        elif args.command == "verify-backup":
            result = verify_backup(args.backup)
        elif args.command == "restore":
            result = (
                restore_stream(sys.stdin.buffer, args.output, timeout_seconds=args.timeout)
                if args.backup == "-"
                else restore_backup(args.backup, args.output, timeout_seconds=args.timeout)
            )
        elif args.command in {"history", "export"}:
            ledger = SQLitePaperLedger(args.db, read_only=True)
            if args.command == "export":
                writer = write_csv if args.format == "csv" else write_jsonl
                writer(ledger.iter_history(args.portfolio, event_type=args.event_type),
                       args.portfolio, sys.stdout)
                return
            result = ledger.history_page(
                args.portfolio, limit=args.limit, before=args.before, event_type=args.event_type,
            )
        elif args.command == "account":
            ledger = SQLitePaperLedger(args.db, read_only=True)
            account = ledger.account(args.portfolio)
            result = {
                "account": account.summary() if account else None,
                "history": ledger.history(args.portfolio),
            }
        else:
            with PublicMarketData() as market_data:
                if args.command == "settle":
                    ledger = SQLitePaperLedger(args.db)
                    account = ledger.account(args.portfolio)
                    if account is None or args.instrument_id not in account.specs:
                        raise TradingError("No paper position for this instrument")
                    stored_spec = account.specs[args.instrument_id]
                    if stored_spec.market_id is None:
                        raise TradingError("Only binary events support final settlement")
                    payout, account = ledger.settle(
                        args.portfolio,
                        args.instrument_id,
                        market_data.event_market(stored_spec.market_id),
                    )
                    result = {"payout": payout, "account": account.summary()}
                elif args.command == "funding":
                    ledger = SQLitePaperLedger(args.db)
                    account = ledger.account(args.portfolio)
                    previous = account.funding_events.get(args.payment_id) if account else None
                    if previous is not None:
                        record = previous["record"]
                        if (
                            record["quote"]["instrument_id"] != args.instrument_id
                            or Decimal(record["rate"]) != args.rate
                        ):
                            raise TradingError("Idempotency conflict: funding payment")
                        payment = Decimal(previous["payment"])
                        assert account is not None
                    else:
                        _, quote = market_data.snapshot(args.instrument_id, args.portfolio)
                        payment, account = ledger.funding(args.payment_id, quote, args.rate)
                    result = {
                        "payment": payment,
                        "account": account.summary(),
                        "funding_source": "operator_supplied_paper_event_at_current_mark",
                    }
                else:
                    spec, quote = market_data.snapshot(args.instrument_id, args.portfolio)
                    if args.command == "quote":
                        result = {"instrument": spec, "quote": quote, "live_execution": False}
                    elif args.command == "mark":
                        account = SQLitePaperLedger(args.db).mark(quote)
                        result = {"account": account.summary()}
                    else:
                        quantity = args.quantity
                        if quantity is None:
                            if args.stake <= 0:
                                raise TradingError("stake must be positive")
                            price = quote.ask if args.side == "buy" else quote.bid
                            quantity = (args.stake / price / spec.quantity_step).to_integral_value(
                                rounding=ROUND_DOWN
                            ) * spec.quantity_step
                        order = _paper_order(args, quantity)
                        fill, account = SQLitePaperLedger(args.db).execute(
                            spec,
                            order,
                            quote,
                            initial_cash=args.initial_cash,
                        )
                        result = {"fill": fill, "account": account.summary()}
    except (TradingError, sqlite3.Error, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(jsonable(result), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
