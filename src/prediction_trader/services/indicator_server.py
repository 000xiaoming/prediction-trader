import json
import logging
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from typing import Any
from urllib.parse import parse_qs, urlsplit

from prediction_trader.backtesting.repository import BacktestRepository
from prediction_trader.config import Settings
from prediction_trader.indicator_page import INDICATOR_HTML
from prediction_trader.indicator_state import IndicatorStateReader
from prediction_trader.persistence import MarketRepository
from prediction_trader.profiles import list_profiles
from prediction_trader.services.paper_trading import PaperTradingGateway
from prediction_trader.services.snapshot_cache import SnapshotCache
from prediction_trader.trading.history_export import write_csv
from prediction_trader.trading.models import TradingError


class IndicatorServerService:
    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        self._settings = settings
        self._stop = stop
        self._logger = logging.getLogger("prediction_trader.indicators")

    def run(self) -> None:
        with ExitStack() as resources:
            repository = MarketRepository(self._settings.database_url)
            resources.callback(repository.close)
            self._serve(repository, resources)

    def _serve(self, repository: MarketRepository, resources: ExitStack) -> None:
        snapshots = {}
        for profile in self._settings.enabled_profiles:
            effective = (
                self._settings if profile.id == self._settings.profile.id
                else self._settings.for_profile(profile.id)
            )
            reader = IndicatorStateReader(repository, settings=effective)
            snapshots[profile.id] = SnapshotCache(
                reader.read, ttl_seconds=self._settings.indicator_cache_ttl_seconds,
                window_seconds=profile.duration_seconds,
            )
        profiles = {
            "active": self._settings.profile.as_dict(),
            "enabled": [p.as_dict() for p in self._settings.enabled_profiles],
            "profiles": [p.as_dict() for p in list_profiles()],
            "switching": "view_only",
        }
        reports = BacktestRepository(repository)
        paper = PaperTradingGateway(self._settings)
        resources.callback(paper.close)
        logger = self._logger
        default_profile = self._settings.profile.id

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(10)

            def reply(self, status: int, value: Any, *, html: bool = False) -> None:
                body = value.encode() if html else json.dumps(value, allow_nan=False).encode()
                self.send_response(status)
                self.send_header(
                    "Content-Type", "text/html; charset=utf-8" if html
                    else "application/json; charset=utf-8",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                try:
                    try:
                        path = urlsplit(self.path)
                        query = parse_qs(path.query, keep_blank_values=True, max_num_fields=8)
                    except ValueError as exc:
                        raise TradingError("Invalid request URL or query") from exc
                    if path.path in {"/", "/index.html"}:
                        self.reply(200, INDICATOR_HTML, html=True)
                    elif path.path == "/api/profiles":
                        self.reply(200, profiles)
                    elif path.path in {"/api/state", "/api/reports"}:
                        profile_id = query.get("profile", [default_profile])[0]
                        if profile_id not in snapshots:
                            raise TradingError("Trading profile is not enabled")
                        if path.path == "/api/state":
                            self.reply(200, snapshots[profile_id].read())
                        else:
                            self.reply(200, {
                                "profile": profile_id, "report": reports.latest(profile_id),
                            })
                    elif path.path == "/api/instruments":
                        self.reply(200, paper.instruments())
                    elif path.path == "/api/trading/state":
                        self.reply(200, paper.state(query.get("instrument", [""])[0]))
                    elif path.path == "/api/trading/account":
                        if (query.keys() != {"instrument"}
                                or len(query["instrument"]) != 1):
                            raise TradingError("Account query requires exactly one instrument")
                        self.reply(200, paper.stored_account(query["instrument"][0]))
                    elif path.path == "/api/trading/history":
                        if (query.keys() - {"instrument", "limit", "before", "event_type", "format"}
                                or any(len(values) != 1 for values in query.values())):
                            raise TradingError("Unknown or repeated history query parameter")
                        format_name = query.get("format", ["json"])[0]
                        if format_name not in {"json", "csv"}:
                            raise TradingError("History format must be json or csv")
                        try:
                            limit = int(query.get("limit", ["50"])[0])
                            before = int(query["before"][0]) if "before" in query else None
                        except ValueError as exc:
                            raise TradingError("History limit and before must be integers") from exc
                        event_type = query.get("event_type", ["fill"])[0]
                        result = paper.history(
                            query.get("instrument", [""])[0], limit=limit, before=before,
                            event_type=None if event_type == "all" else event_type,
                        )
                        if format_name == "csv":
                            output = StringIO()
                            write_csv(result["events"], result["portfolio_id"], output)
                            body = output.getvalue().encode("utf-8-sig")
                            self.send_response(200)
                            self.send_header("Content-Type", "text/csv; charset=utf-8")
                            self.send_header(
                                "Content-Disposition", 'attachment; filename="paper-history.csv"',
                            )
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.send_header("Content-Length", str(len(body)))
                            if result["next_before"] is not None:
                                self.send_header("X-Next-Before", str(result["next_before"]))
                            self.end_headers()
                            self.wfile.write(body)
                        else:
                            self.reply(200, result)
                    else:
                        self.reply(404, {"detail": "Not found"})
                except TradingError as exc:
                    self.reply(400, {"status": "error", "detail": str(exc)})
                except Exception:
                    logger.exception("Unable to load indicator snapshot")
                    self.reply(500, {"status": "error", "detail": "Indicator state unavailable"})

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/api/paper/orders":
                    self.reply(404, {"detail": "Not found"})
                    return
                try:
                    origin = self.headers.get("Origin")
                    try:
                        origin_host = urlsplit(origin).netloc if origin else None
                    except ValueError as exc:
                        raise TradingError("Invalid Origin header") from exc
                    if (self.headers.get("Sec-Fetch-Site") == "cross-site"
                            or origin and origin_host != self.headers.get("Host")):
                        self.reply(403, {"detail": "Cross-origin paper orders are not accepted"})
                        return
                    if self.headers.get_content_type() != "application/json":
                        raise TradingError("Content-Type must be application/json")
                    if self.headers.get("Transfer-Encoding"):
                        raise TradingError("Transfer-Encoding is not supported")
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError as exc:
                        raise TradingError("Invalid Content-Length") from exc
                    if not 0 < length <= 8192:
                        raise TradingError("Paper order body must be between 1 and 8192 bytes")
                    try:
                        payload = json.loads(self.rfile.read(length))
                    except (ValueError, TimeoutError) as exc:
                        raise TradingError("Invalid or incomplete JSON request body") from exc
                    self.reply(200, paper.order(payload))
                except TradingError as exc:
                    self.reply(400, {"status": "error", "detail": str(exc)})
                except Exception:
                    logger.exception("Paper order unavailable")
                    self.reply(500, {"status": "error", "detail": "Paper order unavailable"})

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(
            (self._settings.indicator_host, self._settings.indicator_port), Handler,
        )
        resources.callback(server.server_close)
        server.timeout = 1
        self._logger.info("Indicator dashboard listening on %d", self._settings.indicator_port)
        while not self._stop.is_set():
            server.handle_request()
