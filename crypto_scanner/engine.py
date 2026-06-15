from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analysis import TimeframeView, analyze_timeframe, build_features, clamp
from .market import BinanceMarketData
from .model import OnlineSignalModel
from .storage import SignalStore


TIMEFRAMES = ("15m", "1h", "4h", "1d")
TIMEFRAME_WEIGHTS = {"15m": 1.0, "1h": 1.45, "4h": 1.7, "1d": 1.1}


class MarketScanner:
    def __init__(self, store: SignalStore, top_symbols: int = 15) -> None:
        self.store = store
        self.top_symbols = top_symbols
        self.market = BinanceMarketData()
        self.model = OnlineSignalModel(Path(store.path).parent / "model.json")

    def _resolve_old_signals(self, prices: dict[str, float]) -> None:
        now = datetime.now(timezone.utc)
        for signal in self.store.pending_for_evaluation():
            created = datetime.fromisoformat(signal["created_at"])
            if now - created < timedelta(hours=4):
                continue
            current = prices.get(signal["symbol"])
            if not current:
                continue
            direction_sign = 1 if signal["direction"] == "LONG" else -1
            outcome = (current / signal["price"] - 1) * direction_sign
            threshold = max(
                0.0025,
                abs((signal["take_profit_1"] or signal["price"]) / signal["price"] - 1)
                * 0.35,
            )
            won = outcome > threshold
            status = "win" if won else "loss"
            self.store.resolve(signal["id"], status, outcome)
            features = json.loads(signal["features_json"])
            self.model.learn(features, signal["direction"], won)

    @staticmethod
    def _serialize_view(view: TimeframeView) -> dict:
        return {
            "bias": round(view.bias, 3),
            "trend": view.trend,
            "structure": view.structure,
            "rsi": round(view.rsi, 1),
            "atr_pct": round(view.atr_pct, 3),
            "volume_z": round(view.volume_z, 2),
            "wyckoff": view.wyckoff,
            "liquidity_sweep": view.liquidity_sweep,
            "fvg": view.fvg,
            "order_block": view.order_block,
        }

    def _build_signal(self, market_info: dict, views: dict[str, TimeframeView]) -> dict:
        weighted_score = sum(
            views[timeframe].bias * TIMEFRAME_WEIGHTS[timeframe]
            for timeframe in TIMEFRAMES
        )
        max_score = sum(6 * weight for weight in TIMEFRAME_WEIGHTS.values())
        normalized = clamp(weighted_score / max_score, -1, 1)
        direction = "LONG" if weighted_score >= 0 else "SHORT"
        features = build_features(views, market_info["change_24h"])
        model_probability = self.model.probability(features, direction)
        agreement = sum(
            1
            for view in views.values()
            if (view.bias > 0 and direction == "LONG")
            or (view.bias < 0 and direction == "SHORT")
        )
        technical_confidence = 50 + abs(normalized) * 42
        confidence = clamp(
            technical_confidence * 0.75 + model_probability * 100 * 0.25,
            0,
            99,
        )
        atr_15m = views["15m"].atr
        price = market_info["price"]
        too_extended = abs(market_info["change_24h"]) > 18
        low_volatility = views["15m"].atr_pct < 0.12
        actionable = (
            confidence >= 67
            and agreement >= 3
            and abs(weighted_score) >= 7.0
            and not too_extended
            and not low_volatility
        )
        relevance = "ACTIONABLE" if actionable else "NOT_RELEVANT"
        risk_distance = max(atr_15m * 1.35, price * 0.0035)
        sign = 1 if direction == "LONG" else -1
        stop = price - sign * risk_distance
        target_1 = price + sign * risk_distance * 1.5
        target_2 = price + sign * risk_distance * 2.5

        reasons = [
            f"{agreement}/4 timeframes align {direction.lower()}",
            f"Technical score {weighted_score:.2f}",
            f"Model probability {model_probability * 100:.1f}%",
            f"24h change {market_info['change_24h']:.2f}%",
        ]
        if too_extended:
            reasons.append("Move is extended; chasing risk is high")
        if low_volatility:
            reasons.append("15m volatility is too low")
        if not actionable:
            reasons.append("No sufficiently strong setup right now")
        for timeframe in TIMEFRAMES:
            view = views[timeframe]
            if view.liquidity_sweep:
                reasons.append(f"{timeframe}: {view.liquidity_sweep.replace('_', ' ')}")
            if view.fvg:
                reasons.append(f"{timeframe}: {view.fvg['direction']} FVG")
            if view.order_block:
                reasons.append(f"{timeframe}: {view.order_block['direction']} order block")

        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "symbol": market_info["symbol"],
            "direction": direction,
            "relevance": relevance,
            "confidence": round(confidence, 1),
            "price": price,
            "stop_loss": stop if actionable else None,
            "take_profit_1": target_1 if actionable else None,
            "take_profit_2": target_2 if actionable else None,
            "risk_reward": 1.5 if actionable else None,
            "score": round(weighted_score, 3),
            "reasons": reasons,
            "timeframes": {
                timeframe: self._serialize_view(views[timeframe])
                for timeframe in TIMEFRAMES
            },
            "features": features,
        }

    def run_scan(self) -> list[dict]:
        ranked = self.market.top_usdt_symbols(self.top_symbols)
        prices = {item["symbol"]: item["price"] for item in ranked}
        self._resolve_old_signals(prices)
        results = []
        for market_info in ranked:
            views = {}
            try:
                for timeframe in TIMEFRAMES:
                    candles = self.market.klines(market_info["symbol"], timeframe)
                    views[timeframe] = analyze_timeframe(timeframe, candles)
                signal = self._build_signal(market_info, views)
                self.store.add_signal(signal)
                results.append(signal)
                print(
                    f"[signal] {signal['symbol']} {signal['direction']} "
                    f"{signal['confidence']:.1f}% {signal['relevance']}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[signal] {market_info['symbol']} failed: {exc}", flush=True)
        return results
