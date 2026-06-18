from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

MIN_DEMO_RISK_REWARD = 1.2
INITIAL_DEMO_BALANCE = 100.0
MIN_DEMO_NOTIONAL = 5.0
MAX_DEMO_NOTIONAL = 25.0
MAX_DEMO_HOLD_HOURS = 24


class SignalStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
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
                    initial_stop_loss REAL,
                    take_profit REAL NOT NULL,
                    notional REAL NOT NULL,
                    quantity REAL NOT NULL,
                    risk_amount REAL NOT NULL,
                    initial_risk_amount REAL,
                    risk_reward REAL NOT NULL,
                    stop_stage TEXT NOT NULL DEFAULT 'INITIAL',
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    exit_price REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    current_price REAL,
                    current_pnl REAL NOT NULL DEFAULT 0,
                    current_pnl_pct REAL NOT NULL DEFAULT 0,
                    last_mark_at TEXT,
                    close_reason TEXT,
                    FOREIGN KEY(signal_id) REFERENCES signals(id)
                )
                """
            )
            trade_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(demo_trades)").fetchall()
            }
            for name, definition in {
                "initial_stop_loss": "REAL",
                "initial_risk_amount": "REAL",
                "stop_stage": "TEXT NOT NULL DEFAULT 'INITIAL'",
                "current_price": "REAL",
                "current_pnl": "REAL NOT NULL DEFAULT 0",
                "current_pnl_pct": "REAL NOT NULL DEFAULT 0",
                "last_mark_at": "TEXT",
            }.items():
                if name not in trade_columns:
                    conn.execute(f"ALTER TABLE demo_trades ADD COLUMN {name} {definition}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_wallet (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance REAL NOT NULL
                )
                """
            )
            wallet = conn.execute("SELECT balance FROM demo_wallet WHERE id = 1").fetchone()
            if wallet is None:
                open_notional = conn.execute(
                    "SELECT COALESCE(SUM(notional), 0) FROM demo_trades WHERE status = 'OPEN'"
                ).fetchone()[0]
                realized = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) FROM demo_trades WHERE status = 'CLOSED'"
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO demo_wallet (id, balance) VALUES (1, ?)",
                    (max(0.0, INITIAL_DEMO_BALANCE + realized - open_notional),),
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

    def wallet_balance(self, conn) -> float:
        row = conn.execute("SELECT balance FROM demo_wallet WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO demo_wallet (id, balance) VALUES (1, ?)",
                (INITIAL_DEMO_BALANCE,),
            )
            return INITIAL_DEMO_BALANCE
        return float(row[0])

    def _sized_notional(self, conn, signal: dict, requested: float | None) -> float:
        balance = self.wallet_balance(conn)
        if requested is not None:
            return min(float(requested), balance)
        entry = float(signal["price"])
        stop = float(signal["stop_loss"])
        risk_pct = abs(entry - stop) / entry if entry else 0.0
        if risk_pct <= 0:
            return 0.0
        setup_type = signal.get("setup_type") or ""
        base_risk = 0.012 if setup_type == "fast scalp" else 0.018
        confidence_bonus = max(0.0, float(signal.get("confidence", 0)) - 65.0) / 1000
        risk_budget = balance * min(0.025, base_risk + confidence_bonus)
        risk_sized = risk_budget / risk_pct
        exposure_cap = min(MAX_DEMO_NOTIONAL, balance * 0.25)
        return min(balance, exposure_cap, risk_sized)

    def open_demo_trade(self, signal_id: int, signal: dict, notional: float | None = None) -> bool:
        if signal["relevance"] != "ACTIONABLE" or signal.get("risk_reward", 0) < MIN_DEMO_RISK_REWARD:
            return False
        entry = float(signal["price"])
        stop = float(signal["stop_loss"])
        target = float(signal["take_profit_1"])
        try:
            with self.lock, closing(self._connect()) as conn, conn:
                notional = self._sized_notional(conn, signal, notional)
                if notional < MIN_DEMO_NOTIONAL:
                    return False
                quantity = notional / entry
                risk_amount = abs(entry - stop) * quantity
                conn.execute(
                    """
                    INSERT INTO demo_trades (
                        signal_id, opened_at, exchange, symbol, direction,
                        entry_price, stop_loss, initial_stop_loss, take_profit,
                        notional, quantity, risk_amount, initial_risk_amount,
                        risk_reward, current_price
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id,
                        signal["created_at"],
                        signal["exchange"],
                        signal["symbol"],
                        signal["direction"],
                        entry,
                        stop,
                        stop,
                        target,
                        notional,
                        quantity,
                        risk_amount,
                        risk_amount,
                        signal["risk_reward"],
                        entry,
                    ),
                )
                conn.execute(
                    "UPDATE demo_wallet SET balance = balance - ? WHERE id = 1",
                    (notional,),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def open_demo_positions(self) -> list[dict]:
        with self.lock, closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, exchange, symbol, direction, entry_price, stop_loss,
                       initial_stop_loss, take_profit, notional, quantity,
                       stop_stage
                FROM demo_trades
                WHERE status = 'OPEN'
                ORDER BY id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

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

    def mark_open_trade(
        self, exchange: str, symbol: str, price: float, marked_at: str | None = None
    ) -> None:
        marked_at = marked_at or datetime.now(timezone.utc).isoformat()
        with self.lock, closing(self._connect()) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, direction, entry_price, notional, quantity
                FROM demo_trades
                WHERE exchange = ? AND symbol = ? AND status = 'OPEN'
                """,
                (exchange, symbol),
            ).fetchall()
            for trade in rows:
                sign = 1 if trade["direction"] == "LONG" else -1
                pnl = trade["quantity"] * (price - trade["entry_price"]) * sign
                pnl_pct = pnl / trade["notional"] * 100
                conn.execute(
                    """
                    UPDATE demo_trades
                    SET current_price = ?, current_pnl = ?, current_pnl_pct = ?,
                        last_mark_at = ?
                    WHERE id = ?
                    """,
                    (price, pnl, pnl_pct, marked_at, trade["id"]),
                )

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
                    stop_loss = trade["stop_loss"]
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
                            pnl = ?, pnl_pct = ?, current_price = ?,
                            current_pnl = ?, current_pnl_pct = ?, close_reason = ?
                        WHERE id = ?
                        """,
                        (
                            closed_at,
                            exit_price,
                            pnl,
                            pnl_pct,
                            exit_price,
                            pnl,
                            pnl_pct,
                            close_reason,
                            trade["id"],
                        ),
                    )
                    conn.execute(
                        "UPDATE demo_wallet SET balance = balance + ? WHERE id = 1",
                        (trade["notional"] + pnl,),
                    )
                    resolved += 1
                    break
                else:
                    latest = candles[-1]
                    max_hold_ms = MAX_DEMO_HOLD_HOURS * 60 * 60 * 1000
                    if (
                        latest["close_time"] >= opened_ms + max_hold_ms
                        and "close" in latest
                    ):
                        exit_price = latest["close"]
                        sign = 1 if trade["direction"] == "LONG" else -1
                        pnl = trade["quantity"] * (exit_price - trade["entry_price"]) * sign
                        pnl_pct = pnl / trade["notional"] * 100
                        closed_at = datetime.fromtimestamp(
                            latest["close_time"] / 1000,
                            tz=timezone.utc,
                        ).isoformat()
                        conn.execute(
                            """
                            UPDATE demo_trades
                            SET status = 'CLOSED', closed_at = ?, exit_price = ?,
                                pnl = ?, pnl_pct = ?, current_price = ?,
                                current_pnl = ?, current_pnl_pct = ?,
                                close_reason = 'TIME_EXIT'
                            WHERE id = ?
                            """,
                            (
                                closed_at,
                                exit_price,
                                pnl,
                                pnl_pct,
                                exit_price,
                                pnl,
                                pnl_pct,
                                trade["id"],
                            ),
                        )
                        conn.execute(
                            "UPDATE demo_wallet SET balance = balance + ? WHERE id = 1",
                            (trade["notional"] + pnl,),
                        )
                        resolved += 1
                        continue
                    price = candles[-1]["close"]
                    sign = 1 if trade["direction"] == "LONG" else -1
                    pnl = trade["quantity"] * (price - trade["entry_price"]) * sign
                    pnl_pct = pnl / trade["notional"] * 100
                    original_risk = abs(trade["entry_price"] - (trade["initial_stop_loss"] or trade["stop_loss"]))
                    current_reward = max(0.0, (price - trade["entry_price"]) * sign)
                    progress_r = current_reward / original_risk if original_risk else 0.0
                    new_stop = trade["stop_loss"]
                    stop_stage = trade["stop_stage"] or "INITIAL"
                    if progress_r >= 1.8:
                        trailed = price - sign * original_risk * 0.65
                        new_stop = max(new_stop, trailed) if sign > 0 else min(new_stop, trailed)
                        stop_stage = "TRAILING"
                    elif progress_r >= 1.0:
                        breakeven = trade["entry_price"] + sign * original_risk * 0.08
                        new_stop = max(new_stop, breakeven) if sign > 0 else min(new_stop, breakeven)
                        stop_stage = "BREAKEVEN"
                    elif progress_r >= 0.55:
                        partial = trade["entry_price"] - sign * original_risk * 0.45
                        new_stop = max(new_stop, partial) if sign > 0 else min(new_stop, partial)
                        stop_stage = "REDUCED_RISK"
                    marked_at = datetime.fromtimestamp(
                        candles[-1]["close_time"] / 1000,
                        tz=timezone.utc,
                    ).isoformat()
                    conn.execute(
                        """
                        UPDATE demo_trades
                        SET stop_loss = ?, stop_stage = ?, current_price = ?,
                            current_pnl = ?, current_pnl_pct = ?, last_mark_at = ?
                        WHERE id = ?
                        """,
                        (new_stop, stop_stage, price, pnl, pnl_pct, marked_at, trade["id"]),
                    )
            return resolved

    def demo_stats(self) -> dict:
        with self.lock, closing(self._connect()) as conn, conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_count,
                    SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed,
                    SUM(CASE WHEN status = 'CLOSED' AND pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN status = 'CLOSED' AND pnl < 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN pnl ELSE 0 END), 0) AS pnl,
                    COALESCE(SUM(CASE WHEN status = 'OPEN' THEN current_pnl ELSE 0 END), 0) AS open_pnl
                FROM demo_trades
                """
            ).fetchone()
            wallet = self.wallet_balance(conn)
            open_notional = conn.execute(
                "SELECT COALESCE(SUM(notional), 0) FROM demo_trades WHERE status = 'OPEN'"
            ).fetchone()[0] or 0
        total, open_count, closed, wins, losses, pnl, open_pnl = [
            value or 0 for value in row
        ]
        total_pnl = pnl + open_pnl
        equity = wallet + open_notional + open_pnl
        return {
            "initial_balance": INITIAL_DEMO_BALANCE,
            "cash_balance": round(wallet, 4),
            "open_allocated": round(open_notional, 4),
            "equity": round(equity, 4),
            "total_trades": total,
            "open_trades": open_count,
            "closed_trades": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / closed * 100 if closed else None,
            "realized_pnl": round(pnl, 4),
            "open_pnl": round(open_pnl, 4),
            "total_pnl": round(total_pnl, 4),
            "return_pct": round(pnl / INITIAL_DEMO_BALANCE * 100, 3),
            "total_return_pct": round(total_pnl / INITIAL_DEMO_BALANCE * 100, 3),
            "max_trade_size": MAX_DEMO_NOTIONAL,
            "minimum_risk_reward": MIN_DEMO_RISK_REWARD,
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
