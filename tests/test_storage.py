import tempfile
import unittest
from pathlib import Path

from crypto_scanner.storage import SignalStore


class StorageTests(unittest.TestCase):
    @staticmethod
    def signal(risk_reward=3.0):
        return {
            "created_at": "2026-06-15T12:00:00+00:00",
            "exchange": "Bybit",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "relevance": "ACTIONABLE",
            "confidence": 75,
            "price": 100,
            "stop_loss": 99,
            "take_profit_1": 103,
            "take_profit_2": 105,
            "risk_reward": risk_reward,
            "setup_quality": 6,
            "estimated_win_rate": 42,
            "setup_type": "trend pullback",
            "score": 10,
            "reasons": [],
            "timeframes": {},
            "features": {},
        }

    def test_signal_exchange_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SignalStore(Path(directory) / "signals.db")
            store.add_signal(
                {
                    "created_at": "2026-06-15T12:00:00+00:00",
                    "exchange": "Bybit",
                    "symbol": "BTCUSDT",
                    "direction": "LONG",
                    "relevance": "NOT_RELEVANT",
                    "confidence": 50,
                    "price": 100,
                    "score": 0,
                    "reasons": [],
                    "timeframes": {},
                    "features": {},
                }
            )
            signal = store.latest_signals(limit=1)[0]
            self.assertEqual(signal["exchange"], "Bybit")
            self.assertEqual(signal["setup_quality"], 0)
            self.assertIsNone(signal["estimated_win_rate"])

    def test_demo_trade_closes_at_three_risk_units(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SignalStore(Path(directory) / "signals.db")
            signal = self.signal()
            signal_id = store.add_signal(signal)
            self.assertTrue(store.open_demo_trade(signal_id, signal))
            store.evaluate_demo_trades(
                "Bybit",
                "BTCUSDT",
                [
                    {
                        "open_time": 1781525700000,
                        "close_time": 1781526599999,
                        "high": 103.2,
                        "low": 100,
                    }
                ],
            )
            stats = store.demo_stats()
            self.assertEqual(stats["wins"], 1)
            self.assertAlmostEqual(stats["realized_pnl"], 0.3)
            self.assertAlmostEqual(stats["return_pct"], 3.0)
            self.assertEqual(store.stats()["demo"]["wins"], 1)

    def test_demo_trade_rejects_risk_reward_below_three(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SignalStore(Path(directory) / "signals.db")
            signal = self.signal(risk_reward=2.5)
            signal_id = store.add_signal(signal)
            self.assertFalse(store.open_demo_trade(signal_id, signal))


if __name__ == "__main__":
    unittest.main()
