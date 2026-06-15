from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


BASE_URLS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
)

EXCLUDED_BASES = {
    "USDC",
    "FDUSD",
    "TUSD",
    "USDP",
    "DAI",
    "USD1",
    "USDE",
    "PYUSD",
    "BUSD",
    "EURI",
    "USTC",
    "BFUSD",
    "EUR",
    "TRY",
    "BRL",
    "BIDR",
}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


class BinanceMarketData:
    name = "Binance"

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout
        self.user_agent = "ShahafCryptoSignalLab/1.0 local-research-tool"

    def _get(self, path: str, params: dict | None = None):
        query = urllib.parse.urlencode(params or {})
        suffix = f"?{query}" if query else ""
        last_error: Exception | None = None
        for base in BASE_URLS:
            request = urllib.request.Request(
                f"{base}{path}{suffix}",
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        return json.loads(response.read().decode("utf-8"))
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                    last_error = exc
                    time.sleep(0.7 * (attempt + 1))
        raise RuntimeError(f"Market data request failed: {last_error}")

    def top_usdt_symbols(self, limit: int) -> list[dict]:
        exchange = self._get("/api/v3/exchangeInfo")
        allowed = {
            item["symbol"]
            for item in exchange["symbols"]
            if item.get("status") == "TRADING"
            and item.get("quoteAsset") == "USDT"
            and item.get("isSpotTradingAllowed", True)
        }
        tickers = self._get("/api/v3/ticker/24hr", {"type": "FULL"})
        ranked = []
        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            if symbol not in allowed or not symbol.endswith("USDT"):
                continue
            base = symbol[:-4]
            if base in EXCLUDED_BASES or base.endswith(LEVERAGED_SUFFIXES):
                continue
            try:
                ranked.append(
                    {
                        "symbol": symbol,
                        "base": base,
                        "price": float(ticker["lastPrice"]),
                        "quote_volume": float(ticker["quoteVolume"]),
                        "change_24h": float(ticker["priceChangePercent"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        ranked.sort(key=lambda item: item["quote_volume"], reverse=True)
        return ranked[:limit]

    def klines(self, symbol: str, interval: str, limit: int = 240) -> list[dict]:
        raw = self._get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        return [
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": int(row[6]),
                "quote_volume": float(row[7]),
                "trades": int(row[8]),
            }
            for row in raw
        ]


class BybitMarketData:
    name = "Bybit"
    base_urls = ("https://api.bybit.com", "https://api.bytick.com")
    interval_map = {"5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout
        self.user_agent = "ShahafCryptoSignalLab/1.0 cloud-research-tool"

    def _get(self, path: str, params: dict | None = None):
        query = urllib.parse.urlencode(params or {})
        suffix = f"?{query}" if query else ""
        last_error: Exception | None = None
        for base in self.base_urls:
            request = urllib.request.Request(
                f"{base}{path}{suffix}",
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            )
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    if payload.get("retCode") != 0:
                        raise RuntimeError(payload.get("retMsg", "Unknown Bybit error"))
                    return payload["result"]
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    json.JSONDecodeError,
                    RuntimeError,
                ) as exc:
                    last_error = exc
                    time.sleep(0.7 * (attempt + 1))
        raise RuntimeError(f"Bybit market data request failed: {last_error}")

    def top_usdt_symbols(self, limit: int) -> list[dict]:
        instruments = self._get("/v5/market/instruments-info", {"category": "spot"})
        allowed = {
            item["symbol"]
            for item in instruments.get("list", [])
            if item.get("status") == "Trading" and item.get("quoteCoin") == "USDT"
        }
        tickers = self._get("/v5/market/tickers", {"category": "spot"})
        ranked = []
        for ticker in tickers.get("list", []):
            symbol = ticker.get("symbol", "")
            if symbol not in allowed or not symbol.endswith("USDT"):
                continue
            base = symbol[:-4]
            if base in EXCLUDED_BASES or base.endswith(LEVERAGED_SUFFIXES):
                continue
            try:
                ranked.append(
                    {
                        "symbol": symbol,
                        "base": base,
                        "price": float(ticker["lastPrice"]),
                        "quote_volume": float(ticker["turnover24h"]),
                        "change_24h": float(ticker["price24hPcnt"]) * 100,
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        ranked.sort(key=lambda item: item["quote_volume"], reverse=True)
        return ranked[:limit]

    def klines(self, symbol: str, interval: str, limit: int = 240) -> list[dict]:
        result = self._get(
            "/v5/market/kline",
            {
                "category": "spot",
                "symbol": symbol,
                "interval": self.interval_map[interval],
                "limit": limit,
            },
        )
        rows = list(reversed(result.get("list", [])))
        return [
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": int(row[0]),
                "quote_volume": float(row[6]),
                "trades": 0,
            }
            for row in rows
        ]
