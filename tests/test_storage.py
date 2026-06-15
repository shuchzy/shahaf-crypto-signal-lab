import tempfile
import unittest
from pathlib import Path

from crypto_scanner.storage import SignalStore


class StorageTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
