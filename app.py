from __future__ import annotations

import argparse
import json
import os
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from crypto_scanner.engine import MarketScanner
from crypto_scanner.storage import SignalStore


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"


class AppState:
    def __init__(self, interval_minutes: int, top_symbols: int) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        self.interval_seconds = interval_minutes * 60
        self.store = SignalStore(DATA_DIR / "signals.db")
        self.scanner = MarketScanner(self.store, top_symbols=top_symbols)
        self.lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.running = True
        self.last_started_at: str | None = None
        self.last_finished_at: str | None = None
        self.last_error: str | None = None
        self.next_scan_at = time.time()

    def status(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "scanning": self.scan_lock.locked(),
                "last_started_at": self.last_started_at,
                "last_finished_at": self.last_finished_at,
                "last_error": self.last_error,
                "next_scan_at": datetime.fromtimestamp(
                    self.next_scan_at, tz=timezone.utc
                ).isoformat(),
                "interval_seconds": self.interval_seconds,
                "source": "Binance public spot market data",
            }

    def scan_once(self) -> None:
        if not self.scan_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                self.last_started_at = datetime.now(timezone.utc).isoformat()
                self.last_error = None
            self.scanner.run_scan()
            with self.lock:
                self.last_finished_at = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            with self.lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
            print(f"[scanner] {self.last_error}", flush=True)
        finally:
            self.scan_lock.release()

    def scheduler(self) -> None:
        while self.running:
            now = time.time()
            if now >= self.next_scan_at:
                self.next_scan_at = now + self.interval_seconds
                self.scan_once()
            time.sleep(1)


class DashboardHandler(SimpleHTTPRequestHandler):
    state: AppState

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self.send_json(self.state.status())
            return
        if path == "/api/signals":
            self.send_json({"signals": self.state.store.latest_signals(limit=250)})
            return
        if path == "/api/stats":
            self.send_json(self.state.store.stats())
            return
        if path == "/health":
            self.send_json({"ok": True})
            return
        if path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/scan":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.state.scan_lock.locked():
            self.send_json({"accepted": False, "message": "Scan already running"}, 409)
            return
        threading.Thread(target=self.state.scan_once, daemon=True).start()
        self.send_json({"accepted": True}, 202)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local crypto market signal dashboard")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--interval", type=int, default=15, help="Scan interval in minutes")
    parser.add_argument("--top", type=int, default=15, help="Number of top-volume USDT pairs")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    state = AppState(max(1, args.interval), max(3, min(args.top, 30)))
    DashboardHandler.state = state
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    threading.Thread(target=state.scheduler, daemon=True).start()
    url = f"http://{args.host}:{args.port}"
    print(f"Shahaf Crypto Signal Lab is running at {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if not args.no_browser and os.environ.get("CRYPTO_SCANNER_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.running = False
        server.server_close()


if __name__ == "__main__":
    main()
