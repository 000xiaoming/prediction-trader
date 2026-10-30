import csv
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from prediction_trader.config import Settings
from prediction_trader.services.paper_trading import PaperTradingGateway
from prediction_trader.trading.catalog import get_instrument
from prediction_trader.trading.cli import main
from prediction_trader.trading.history_export import write_csv, write_jsonl
from prediction_trader.trading.ledger import SQLitePaperLedger
from prediction_trader.trading.models import PaperOrder, Quote, TradingError

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def fill(ledger: SQLitePaperLedger, key: str, portfolio: str = "alpha") -> None:
    instrument = get_instrument("binance_btc_spot")
    ledger.execute(
        instrument,
        PaperOrder(key, instrument.instrument_id, portfolio, "buy", Decimal("1"), Decimal("10")),
        Quote(instrument.instrument_id, portfolio, NOW, Decimal("100"), Decimal("101"),
              Decimal("100"), Decimal("100")),
        now=NOW,
    )


def test_history_cursor_is_stable_during_new_writes_and_isolates_portfolios(tmp_path: Path) -> None:
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    fill(ledger, "first")
    fill(ledger, "other", "beta")
    fill(ledger, "second")
    fill(ledger, "third")
    reader = SQLitePaperLedger(ledger.path, read_only=True)
    first = reader.history_page("alpha", limit=2, event_type="fill")
    assert [event["sequence"] for event in first["events"]] == [4, 3]
    assert first["next_before"] == 3
    fill(ledger, "new")
    last = reader.history_page("alpha", limit=2, before=first["next_before"], event_type="fill")
    assert [event["sequence"] for event in last["events"]] == [1]
    assert last["next_before"] is None
    assert reader.history_page("alpha", event_type="funding")["events"] == []
    assert reader.history_page("missing")["events"] == []


def test_status_reports_storage_and_pages_accounts_without_mutating_them(tmp_path: Path) -> None:
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    for key, portfolio in (("first", "alpha"), ("second", "alpha"), ("first", "beta")):
        fill(ledger, key, portfolio)
    expected = ledger.account("alpha").to_state()
    reader = SQLitePaperLedger(ledger.path, read_only=True)
    first = reader.status(limit=1)
    assert first["exists"] and first["account_count"] == 2 and first["event_count"] == 3
    assert first["events_by_type"] == {"fill": 3} and first["latest_sequence"] == 3
    assert first["portfolios"] == [{"portfolio_id": "alpha", "updated_at": NOW.isoformat()}]
    assert first["next_after"] == "alpha"
    second = reader.status(limit=1, after=first["next_after"])
    assert second["portfolios"][0]["portfolio_id"] == "beta" and second["next_after"] is None
    assert first["storage"]["database_bytes"] > 0 and first["storage"]["logical_bytes"] > 0
    assert first["storage"]["reusable_bytes"] <= first["storage"]["logical_bytes"]
    assert reader.account("alpha").to_state() == expected


def test_status_cli_is_offline_and_does_not_create_a_missing_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    market = Mock(side_effect=AssertionError("Status must stay offline"))
    monkeypatch.setattr("prediction_trader.trading.cli.PublicMarketData", market)
    path = tmp_path / "missing" / "paper.db"
    main(["status", "--db", str(path)])
    result = json.loads(capsys.readouterr().out)
    assert not result["exists"] and result["event_count"] == result["account_count"] == 0
    assert result["portfolios"] == [] and not path.parent.exists()
    market.assert_not_called()


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 1001}, {"limit": True},
                                     {"after": ""}, {"after": "x' OR 1=1 --"}])
def test_status_rejects_invalid_paging_before_file_access(tmp_path: Path, options: dict) -> None:
    with pytest.raises(TradingError):
        SQLitePaperLedger(tmp_path / "missing.db", read_only=True).status(**options)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("options", [
    {"limit": 0}, {"limit": 1001}, {"limit": True}, {"limit": "2"},
    {"before": 0}, {"before": -1}, {"before": 2**63}, {"before": True},
    {"event_type": "fill' OR 1=1 --"},
])
def test_history_validates_filters_even_without_a_database(tmp_path: Path, options: dict) -> None:
    reader = SQLitePaperLedger(tmp_path / "missing" / "paper.db", read_only=True)
    with pytest.raises(TradingError):
        reader.history_page("alpha", **options)
    assert not reader.path.parent.exists()


def test_read_only_access_does_not_create_or_modify_accounts(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "paper.db"
    reader = SQLitePaperLedger(path, read_only=True)
    assert reader.account("alpha") is None
    assert reader.history_page("alpha")["events"] == []
    assert list(reader.iter_history("alpha")) == []
    assert not path.parent.exists()
    ledger = SQLitePaperLedger(path)
    fill(ledger, "first")
    before = ledger.account("alpha").to_state()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        fill(reader, "must-not-write")
    assert ledger.account("alpha").to_state() == before
    assert len(ledger.history("alpha")) == 1


def test_full_export_streams_past_one_batch_from_a_consistent_snapshot(tmp_path: Path) -> None:
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    fill(ledger, "first")
    with sqlite3.connect(ledger.path) as connection:
        connection.executemany(
            "INSERT INTO paper_events(portfolio_id,event_key,event_type,observed_at,payload) "
            "VALUES('alpha',?,'mark',?,?)",
            [(f"mark:{n}", NOW.isoformat(), '{"result":null}') for n in range(1005)],
        )
    reader = SQLitePaperLedger(ledger.path, read_only=True)
    stream = reader.iter_history("alpha")
    assert next(stream)["sequence"] == 1
    fill(ledger, "arrived-during-export")
    remaining = list(stream)
    assert len(remaining) == 1005
    assert remaining[-1]["sequence"] == 1006
    assert [event["sequence"] for event in reader.iter_history("alpha", event_type="fill")] == [
        1, 1007,
    ]


def test_export_preserves_decimal_text_unicode_and_blocks_formula_cells(tmp_path: Path) -> None:
    ledger = SQLitePaperLedger(tmp_path / "paper.db")
    fill(ledger, "first")
    events = ledger.history("alpha")
    events[0]["payload"]["note"] = '研究, "记录"\n第二行'
    events[0]["observed_at"] = '=HYPERLINK("https://example.invalid")'
    events[0]["payload"]["result"]["price"] = "101.000000000000000001"
    output = io.StringIO()
    write_csv(events, "alpha", output)
    rows = list(csv.DictReader(io.StringIO(output.getvalue())))
    assert len(rows) == 1
    assert rows[0]["price"] == "101.000000000000000001"
    assert rows[0]["observed_at"].startswith("'=")
    assert json.loads(rows[0]["payload_json"]) == events[0]["payload"]
    output = io.StringIO()
    write_jsonl(events, "alpha", output)
    assert json.loads(output.getvalue()) == {"portfolio_id": "alpha", **events[0]}


def test_cli_history_and_complete_exports_are_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    market = Mock(side_effect=AssertionError("History must not access market data"))
    monkeypatch.setattr("prediction_trader.trading.cli.PublicMarketData", market)
    path = tmp_path / "paper.db"
    ledger = SQLitePaperLedger(path)
    fill(ledger, "first")
    fill(ledger, "second")
    main(["history", "alpha", "--db", str(path), "--limit", "1"])
    page = json.loads(capsys.readouterr().out)
    assert page["next_before"] == 2
    main(["history", "alpha", "--db", str(path), "--before", "2"])
    assert json.loads(capsys.readouterr().out)["events"][0]["sequence"] == 1
    main(["export", "alpha", "--db", str(path)])
    assert [row["sequence"] for row in csv.DictReader(io.StringIO(capsys.readouterr().out))] == [
        "1", "2",
    ]
    main(["export", "alpha", "--db", str(path), "--format", "jsonl", "--event-type", "fill"])
    assert len(capsys.readouterr().out.splitlines()) == 2
    main(["account", "missing", "--db", str(tmp_path / "not-created.db")])
    assert json.loads(capsys.readouterr().out)["account"] is None
    assert not (tmp_path / "not-created.db").exists()
    market.assert_not_called()


def test_history_gateway_is_offline_and_does_not_add_mark_events(tmp_path: Path) -> None:
    gateway = PaperTradingGateway(
        Settings(_env_file=None, paper_ledger_path=str(tmp_path / "p.db")),
    )
    try:
        fill(gateway.ledger, "first", "paper:binance_btc_spot")
        before = gateway.ledger.account("paper:binance_btc_spot").to_state()
        gateway._data.snapshot = Mock(side_effect=AssertionError("Must not fetch quotes"))
        result = gateway.history("binance_btc_spot")
        assert result["portfolio_id"] == "paper:binance_btc_spot"
        assert result["events"][0]["event_type"] == "fill"
        assert gateway.history("binance_eth_spot")["events"] == []
        assert len(gateway.ledger.history("paper:binance_btc_spot")) == 1
        assert gateway.ledger.account("paper:binance_btc_spot").to_state() == before
        with pytest.raises(TradingError, match="Unknown instrument"):
            gateway.history("alpha")
    finally:
        gateway.close()


def test_funding_and_settlement_exports_keep_signed_results() -> None:
    output = io.StringIO()
    events = [{
        "sequence": sequence, "event_type": kind,
        "observed_at": (NOW + timedelta(seconds=sequence)).isoformat(),
        "payload": {"instrument_id": "event", "result": result},
    } for sequence, kind, result in [(1, "funding", "-0.00001"), (2, "settlement", "10.50")]]
    write_csv(events, "alpha", output)
    assert [row["result"] for row in csv.DictReader(io.StringIO(output.getvalue()))] == [
        "-0.00001", "10.50",
    ]
