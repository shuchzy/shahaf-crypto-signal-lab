import unittest

from crypto_scanner.analysis import (
    analyze_timeframe,
    fvg_retest,
    recent_fvg,
    recent_ict_events,
    rsi,
)


def candle(index, open_price, high, low, close, volume=100):
    return {
        "open_time": index,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "close_time": index + 1,
        "quote_volume": volume * close,
        "trades": 10,
    }


class AnalysisTests(unittest.TestCase):
    def test_rsi_rising_market(self):
        self.assertGreater(rsi([float(i) for i in range(1, 40)]), 90)

    def test_detects_bullish_fvg(self):
        candles = [
            candle(1, 10, 11, 9, 10),
            candle(2, 11, 14, 10, 13),
            candle(3, 15, 16, 14, 15),
        ]
        gap = recent_fvg(candles)
        self.assertEqual(gap["direction"], "bullish")
        self.assertEqual(gap["low"], 11)
        self.assertEqual(gap["high"], 14)

    def test_timeframe_view_has_positive_bias_in_uptrend(self):
        candles = []
        for index in range(240):
            price = 100 + index * 0.5
            candles.append(candle(index, price - 0.2, price + 0.4, price - 0.5, price, 100 + index))
        view = analyze_timeframe("15m", candles)
        self.assertGreater(view.bias, 0)
        self.assertIn("bullish", view.trend)

    def test_ignores_unclosed_final_candle(self):
        candles = []
        for index in range(240):
            price = 100 + index * 0.1
            candles.append(
                candle(index, price - 0.05, price + 0.15, price - 0.15, price, 100)
            )
        candles[-1] = candle(239, 124, 160, 80, 90, 10000)
        view = analyze_timeframe("15m", candles)
        self.assertGreater(view.bias, 0)
        self.assertNotEqual(view.displacement, "bearish")

    def test_detects_closed_bullish_break_and_displacement(self):
        candles = []
        for index in range(238):
            price = 100 + index * 0.01
            candles.append(candle(index, price, price + 0.2, price - 0.2, price + 0.05))
        candles.append(candle(238, 102.3, 105.5, 102.2, 105.2, 500))
        candles.append(candle(239, 105.2, 105.4, 105.0, 105.1))
        view = analyze_timeframe("15m", candles)
        self.assertEqual(view.structure_break, "bullish_bos")
        self.assertEqual(view.displacement, "bullish")

    def test_detects_fvg_retest_on_closed_entry_candle(self):
        candles = [
            candle(1, 10, 11, 9.5, 10.5),
            candle(2, 10.5, 14.5, 10.4, 14),
            candle(3, 15, 16, 14, 15.5),
        ]
        for index in range(4, 12):
            candles.append(candle(index, 15.5, 16, 15, 15.6))
        candles.append(candle(12, 15, 15.4, 13.8, 14.6))
        candles.append(candle(13, 14.6, 14.8, 14.4, 14.7))
        retest = fvg_retest(candles, "LONG")
        self.assertIsNotNone(retest)
        self.assertEqual(retest["low"], 11)
        self.assertEqual(retest["high"], 14)

    def test_detects_recent_sweep_and_bos_sequence(self):
        candles = []
        for index in range(30):
            candles.append(candle(index, 100, 102, 98, 100))
        candles.append(candle(30, 100, 101, 96, 99))
        candles.append(candle(31, 99, 103, 98.5, 102.5))
        candles.append(candle(32, 102.5, 103, 102, 102.8))
        events = recent_ict_events(candles, event_window=8)
        self.assertTrue(events["bullish_sweep"])
        self.assertTrue(events["bullish_bos"])


if __name__ == "__main__":
    unittest.main()
