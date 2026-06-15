import unittest

from crypto_scanner.analysis import analyze_timeframe, recent_fvg, rsi


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


if __name__ == "__main__":
    unittest.main()
