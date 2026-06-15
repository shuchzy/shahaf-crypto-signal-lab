from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
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

    def _connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def add_signal(self, signal: dict) -> int:
        with self.lock, closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO signals (
                    created_at, exchange, symbol, direction, relevance, confidence, price,
                    stop_loss, take_profit_1, take_profit_2, risk_reward, score,
                    reasons_json, timeframes_json, features_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        }
