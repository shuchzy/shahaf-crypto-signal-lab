from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from crypto_scanner.engine import MarketScanner
from crypto_scanner.storage import SignalStore


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))


class AuthManager:
    def __init__(self) -> None:
        self.username = os.environ.get("APP_USERNAME", "")
        self.password_hash = os.environ.get("APP_PASSWORD_HASH", "")
        self.session_secret = os.environ.get(
            "SESSION_SECRET", secrets.token_urlsafe(48)
        ).encode("utf-8")
        self.secure_cookie = os.environ.get(
            "COOKIE_SECURE", "1" if os.environ.get("PORT") else "0"
        ) == "1"
        self.attempts: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password_hash)

    def verify_password(self, password: str) -> bool:
        try:
            algorithm, iterations, salt_hex, digest_hex = self.password_hash.split(
                "$", 3
            )
            if algorithm != "pbkdf2_sha256":
                return False
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                bytes.fromhex(salt_hex),
                int(iterations),
            )
            return hmac.compare_digest(candidate.hex(), digest_hex)
        except (ValueError, TypeError):
            return False

    def rate_limited(self, address: str) -> bool:
        now = time.time()
        with self.lock:
            recent = [
                timestamp
                for timestamp in self.attempts.get(address, [])
                if now - timestamp < 900
            ]
            self.attempts[address] = recent
            return len(recent) >= 8

    def record_failure(self, address: str) -> None:
        with self.lock:
            self.attempts.setdefault(address, []).append(time.time())

    def clear_failures(self, address: str) -> None:
        with self.lock:
            self.attempts.pop(address, None)

    def create_session(self) -> str:
        expires = int(time.time() + 7 * 24 * 60 * 60)
        payload = f"{self.username}|{expires}|{secrets.token_hex(12)}".encode("utf-8")
        signature = hmac.new(
            self.session_secret, payload, hashlib.sha256
        ).digest()
        encoded_payload = base64.urlsafe_b64encode(payload).decode("ascii")
        encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii")
        return f"{encoded_payload}.{encoded_signature}"

    def valid_session(self, token: str) -> bool:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            payload = base64.urlsafe_b64decode(encoded_payload.encode("ascii"))
            signature = base64.urlsafe_b64decode(encoded_signature.encode("ascii"))
            expected = hmac.new(
                self.session_secret, payload, hashlib.sha256
            ).digest()
            if not hmac.compare_digest(signature, expected):
                return False
            username, expires, _ = payload.decode("utf-8").split("|", 2)
            return username == self.username and int(expires) > time.time()
        except (ValueError, TypeError, UnicodeDecodeError):
            return False


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
        self.event_version = 0
        self.event_condition = threading.Condition()

    def status(self) -> dict:
        with self.lock:
            now = time.time()
            if self.last_finished_at:
                last_finished = datetime.fromisoformat(self.last_finished_at).timestamp()
                data_age_seconds = max(0, int(now - last_finished))
            else:
                data_age_seconds = None
            data_stale = (
                data_age_seconds is None
                or data_age_seconds > self.interval_seconds * 2
            )
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
                "data_stale": data_stale,
                "data_age_seconds": data_age_seconds,
                "source": "Binance + Bybit public spot market data",
                "connection": "live",
                "markets": self.scanner.market_health,
            }

    def publish(self) -> None:
        with self.event_condition:
            self.event_version += 1
            self.event_condition.notify_all()

    def live_payload(self) -> dict:
        return {
            "status": self.status(),
            "signals": self.store.latest_signals(limit=250),
            "stats": self.store.stats(),
            "version": self.event_version,
        }

    def scan_once(self) -> None:
        if not self.scan_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                self.last_started_at = datetime.now(timezone.utc).isoformat()
                self.last_error = None
            self.publish()
            self.scanner.run_scan()
            with self.lock:
                self.last_finished_at = datetime.now(timezone.utc).isoformat()
            self.publish()
        except Exception as exc:
            with self.lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.publish()
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
    auth: AuthManager

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

    def authenticated(self) -> bool:
        if not self.auth.enabled:
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get("session")
        return bool(session and self.auth.valid_session(session.value))

    def redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def require_auth(self, path: str) -> bool:
        if path in {"/health", "/login", "/login.html"}:
            return False
        if self.authenticated():
            return False
        if path.startswith("/api/"):
            self.send_json({"error": "authentication_required"}, 401)
        else:
            self.redirect("/login")
        return True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/login", "/login.html"}:
            if self.authenticated():
                self.redirect("/")
                return
            self.path = "/login.html"
            super().do_GET()
            return
        if self.require_auth(path):
            return
        if path == "/api/live":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            version = -1
            try:
                while self.state.running:
                    if version != self.state.event_version:
                        payload = json.dumps(
                            self.state.live_payload(), ensure_ascii=False
                        )
                        self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        version = self.state.event_version
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    with self.state.event_condition:
                        self.state.event_condition.wait(timeout=10)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path == "/api/status":
            status = self.state.status()
            if status["data_stale"] and not status["scanning"]:
                threading.Thread(target=self.state.scan_once, daemon=True).start()
                status["scanning"] = True
            self.send_json(status)
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
        path = urlparse(self.path).path
        if path == "/login":
            if self.auth.rate_limited(self.client_address[0]):
                self.redirect("/login?error=locked")
                return
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            username = form.get("username", [""])[0]
            password = form.get("password", [""])[0]
            if hmac.compare_digest(username, self.auth.username) and self.auth.verify_password(
                password
            ):
                self.auth.clear_failures(self.client_address[0])
                secure = "; Secure" if self.auth.secure_cookie else ""
                cookie = (
                    f"session={self.auth.create_session()}; Path=/; Max-Age=604800; "
                    f"HttpOnly; SameSite=Strict{secure}"
                )
                self.redirect("/", cookie)
            else:
                self.auth.record_failure(self.client_address[0])
                self.redirect("/login?error=invalid")
            return
        if path == "/logout":
            secure = "; Secure" if self.auth.secure_cookie else ""
            self.redirect(
                "/login",
                f"session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}",
            )
            return
        if self.require_auth(path):
            return
        if path != "/api/scan":
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
    parser.add_argument(
        "--top",
        type=int,
        default=int(os.environ.get("TOP_SYMBOLS", "20")),
        help="Number of top-volume USDT pairs",
    )
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    state = AppState(max(1, args.interval), max(3, min(args.top, 30)))
    DashboardHandler.state = state
    DashboardHandler.auth = AuthManager()
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
