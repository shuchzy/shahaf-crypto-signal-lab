from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class SignalStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    exchange TEXT NOT NULL DEFAULT 'Binance',
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    relevance TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    price REAL NOT NULL,
                    stop_loss REAL,
                    take_profit_1 REAL,
                    take_profit_2 REAL,
                    risk_reward REAL,
                    setup_quality INTEGER NOT NULL DEFAULT 0,
                    estimated_win_rate REAL,
                    setup_type TEXT,
                    score REAL NOT NULL,
                    reasons_json TEXT NOT NULL,
                    timeframes_json TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    outcome_return REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC)"
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()
            }
            if "exchange" not in columns:
                conn.execute(
                    "ALTER TABLE signals ADD COLUMN exchange TEXT NOT NULL DEFAULT 'Binance'"
                )
            if "setup_quality" not in columns:
                conn.execute(
                    "ALTER TABLE signals ADD COLUMN setup_quality INTEGER NOT NULL DEFAULT 0"
                )
            if "estimated_win_rate" not in columns:
                conn.execute("ALTER TABLE signals ADD COLUMN estimated_win_rate REAL")
            if "setup_type" not in columns:
                conn.execute("ALTER TABLE signals ADD COLUMN setup_type TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL UNIQUE,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    notional REAL NOT NULL,
                    quantity REAL NOT NULL,
                    risk_amount REAL NOT NULL,
                    risk_reward REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    exit_price REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    close_reason TEXT,
                    FOREIGN KEY(signal_id) REFERENCES signals(id)
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_demo_open_symbol
                ON demo_trades(exchange, symbol) WHERE status = 'OPEN'
                """
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def add_signal(self, signal: dict) -> int:
        with self.lock, closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO signals (
                    created_at, exchange, symbol, direction, relevance, confidence, price,
                    stop_loss, take_profit_1, take_profit_2, risk_reward, score,
                    setup_quality, estimated_win_rate, setup_type,
                    reasons_json, timeframes_json, features_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal["created_at"],
                    signal["exchange"],
                    signal["symbol"],
                    signal["direction"],
                    signal["relevance"],
                    signal["confidence"],
                    signal["price"],
                    signal.get("stop_loss"),
                    signal.get("take_profit_1"),
                    signal.get("take_profit_2"),
                    signal.get("risk_reward"),
                    signal["score"],
                    signal.get("setup_quality", 0),
                    signal.get("estimated_win_rate"),
                    signal.get("setup_type"),
                    json.dumps(signal["reasons"]),
                    json.dumps(signal["timeframes"]),
                    json.dumps(signal["features"]),
                ),
            )
            return int(cursor.lastrowid)

    def latest_signals(self, limit: int = 200) -> list[dict]:
        with self.lock, closing(self._connect()) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(item.pop("reasons_json"))
            item["timeframes"] = json.loads(item.pop("timeframes_json"))
            item["features"] = json.loads(item.pop("features_json"))
            output.append(item)
        return output

    def pending_for_evaluation(self, max_rows: int = 500) -> list[dict]:
        with self.lock, closing(self._connect()) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM signals
                WHERE status = 'pending' AND relevance = 'ACTIONABLE'
                ORDER BY id ASC LIMIT ?
                """,
                (max_rows,),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve(self, signal_id: int, status: str, outcome_return: float) -> None:
        with self.lock, closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE signals SET status = ?, outcome_return = ? WHERE id = ?",
                (status, outcome_return, signal_id),
            )

    def open_demo_trade(self, signal_id: int, signal: dict, notional: float = 10.0) -> bool:
        if signal["relevance"] != "ACTIONABLE" or signal.get("risk_reward", 0) < 3:
            return False
        entry = float(signal["price"])
        stop = float(signal["stop_loss"])
        target = float(signal["take_profit_1"])
        quantity = notional / entry
        risk_amount = abs(entry - stop) * quantity
        try:
            with self.lock, closing(self._connect()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO demo_trades (
                        signal_id, opened_at, exchange, symbol, direction,
                        entry_price, stop_loss, take_profit, notional, quantity,
                        risk_amount, risk_reward
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id,
                        signal["created_at"],
                        signal["exchange"],
                        signal["symbol"],
                        signal["direction"],
                        entry,
                        stop,
                        target,
                        notional,
                        quantity,
                        risk_amount,
                        signal["risk_reward"],
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def has_open_demo_trade(self, exchange: str, symbol: str) -> bool:
        with self.lock, closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM demo_trades
                WHERE exchange = ? AND symbol = ? AND status = 'OPEN'
                LIMIT 1
                """,
                (exchange, symbol),
            ).fetchone()
        return row is not None

    def evaluate_demo_trades(
        self, exchange: str, symbol: str, candles: list[dict]
    ) -> int:
        with self.lock, closing(self._connect()) as conn, conn:
            conn.row_factory = sqlite3.Row
            trades = conn.execute(
                """
                SELECT * FROM demo_trades
                WHERE exchange = ? AND symbol = ? AND status = 'OPEN'
                """,
                (exchange, symbol),
            ).fetchall()
            resolved = 0
            for trade in trades:
                opened_ms = int(
                    datetime.fromisoformat(trade["opened_at"]).timestamp() * 1000
                )
                for candle in candles:
                    if candle["open_time"] <= opened_ms:
                        continue
                    if trade["direction"] == "LONG":
                        stop_hit = candle["low"] <= trade["stop_loss"]
                        target_hit = candle["high"] >= trade["take_profit"]
                    else:
                        stop_hit = candle["high"] >= trade["stop_loss"]
                        target_hit = candle["low"] <= trade["take_profit"]
                    if not stop_hit and not target_hit:
                        continue
                    close_reason = "STOP_LOSS" if stop_hit else "TAKE_PROFIT"
                    exit_price = (
                        trade["stop_loss"] if stop_hit else trade["take_profit"]
                    )
                    sign = 1 if trade["direction"] == "LONG" else -1
                    pnl = trade["quantity"] * (exit_price - trade["entry_price"]) * sign
                    pnl_pct = pnl / trade["notional"] * 100
                    closed_at = datetime.fromtimestamp(
                        candle["close_time"] / 1000,
                        tz=timezone.utc,
                    ).isoformat()
                    conn.execute(
                        """
                        UPDATE demo_trades
                        SET status = 'CLOSED', closed_at = ?, exit_price = ?,
                            pnl = ?, pnl_pct = ?, close_reason = ?
                        WHERE id = ?
                        """,
                        (
                            closed_at,
                            exit_price,
                            pnl,
                            pnl_pct,
                            close_reason,
                            trade["id"],
                        ),
                    )
                    resolved += 1
                    break
            return resolved

    def demo_stats(self) -> dict:
        with self.lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed,
                    SUM(CASE WHEN close_reason = 'TAKE_PROFIT' THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN close_reason = 'STOP_LOSS' THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN pnl ELSE 0 END), 0) AS pnl
                FROM demo_trades
                """
            ).fetchone()
        total, open_count, closed, wins, losses, pnl = [
            value or 0 for value in row
        ]
        return {
            "total_trades": total,
            "open_trades": open_count,
            "closed_trades": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / closed * 100 if closed else None,
            "realized_pnl": round(pnl, 4),
            "return_pct": round(pnl / (closed * 10) * 100, 3) if closed else None,
            "trade_size": 10,
            "minimum_risk_reward": 3,
        }

    def stats(self) -> dict:
        with self.lock, closing(self._connect()) as conn, conn:
            total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            actionable = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE relevance = 'ACTIONABLE'"
            ).fetchone()[0]
            wins = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE status = 'win'"
            ).fetchone()[0]
            losses = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE status = 'loss'"
            ).fetchone()[0]
        resolved = wins + losses
        return {
            "total": total,
            "actionable": actionable,
            "wins": wins,
            "losses": losses,
            "resolved": resolved,
            "win_rate": wins / resolved * 100 if resolved else None,
            "demo": self.demo_stats(),
        }
