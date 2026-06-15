from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analysis import (
    TimeframeView,
    analyze_timeframe,
    build_features,
    clamp,
    fvg_retest,
    recent_ict_events,
)
from .market import BinanceMarketData, BybitMarketData
from .model import OnlineSignalModel
from .storage import SignalStore


TIMEFRAMES = ("15m", "1h", "4h", "1d")
FETCH_TIMEFRAMES = ("5m",) + TIMEFRAMES
TIMEFRAME_WEIGHTS = {"15m": 1.0, "1h": 1.45, "4h": 1.7, "1d": 1.1}


class MarketScanner:
    def __init__(self, store: SignalStore, top_symbols: int = 15) -> None:
        self.store = store
        self.top_symbols = top_symbols
        self.markets = (BinanceMarketData(), BybitMarketData())
        self.model = OnlineSignalModel(Path(store.path).parent / "model.json")
        self.market_health = {
            market.name: {"ok": False, "error": "Waiting for first scan"}
            for market in self.markets
        }

    def _resolve_old_signals(self, prices: dict[str, float]) -> None:
        now = datetime.now(timezone.utc)
        for signal in self.store.pending_for_evaluation():
            created = datetime.fromisoformat(signal["created_at"])
            if now - created < timedelta(hours=4):
                continue
            current = prices.get(f"{signal['exchange']}:{signal['symbol']}")
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
            "structure_break": view.structure_break,
            "displacement": view.displacement,
            "fvg": view.fvg,
            "order_block": view.order_block,
        }

    def _build_signal(
        self,
        market_info: dict,
        views: dict[str, TimeframeView],
        timeframe_candles: dict[str, list[dict]],
    ) -> dict:
        weighted_score = sum(
            views[timeframe].bias * TIMEFRAME_WEIGHTS[timeframe]
            for timeframe in TIMEFRAMES
        )
        max_score = sum(6 * weight for weight in TIMEFRAME_WEIGHTS.values())
        normalized = clamp(weighted_score / max_score, -1, 1)
        direction = "LONG" if weighted_score >= 0 else "SHORT"
        features = build_features(views, market_info["change_24h"])
        model_probability = self.model.probability(features, direction)
        model_weight = min(0.25, self.model.samples / 400 * 0.25)
        agreement = sum(
            1
            for view in views.values()
            if (view.bias > 0 and direction == "LONG")
            or (view.bias < 0 and direction == "SHORT")
        )
        technical_confidence = 50 + abs(normalized) * 44
        confidence = clamp(
            technical_confidence * (1 - model_weight)
            + model_probability * 100 * model_weight,
            0,
            96,
        )
        atr_15m = views["15m"].atr
        price = market_info["price"]
        too_extended = abs(market_info["change_24h"]) > 18
        low_volatility = views["15m"].atr_pct < 0.12
        high_volatility = views["15m"].atr_pct > 4.5
        sign = 1 if direction == "LONG" else -1
        aligned = lambda value: value == ("bullish" if sign > 0 else "bearish")
        aligned_break = views["15m"].structure_break == (
            "bullish_bos" if sign > 0 else "bearish_bos"
        )
        aligned_sweep = views["15m"].liquidity_sweep == (
            "sell_side_sweep" if sign > 0 else "buy_side_sweep"
        )
        aligned_fvg = bool(views["15m"].fvg and aligned(views["15m"].fvg["direction"]))
        aligned_block = bool(
            views["15m"].order_block
            and aligned(views["15m"].order_block["direction"])
        )
        aligned_displacement = aligned(views["15m"].displacement)
        ict_events = recent_ict_events(timeframe_candles["15m"])
        ict_sweep = ict_events["bullish_sweep" if sign > 0 else "bearish_sweep"]
        ict_bos = ict_events["bullish_bos" if sign > 0 else "bearish_bos"]
        entry_fvg = fvg_retest(timeframe_candles["5m"], direction)
        entry_trigger = ict_sweep and ict_bos and entry_fvg is not None
        htf_aligned = all(
            (views[timeframe].bias > 0 and sign > 0)
            or (views[timeframe].bias < 0 and sign < 0)
            for timeframe in ("1h", "4h")
        )
        daily_veto = (
            views["1d"].bias < -2.0 if sign > 0 else views["1d"].bias > 2.0
        )
        open_position = self.store.has_open_demo_trade(
            market_info["exchange"], market_info["symbol"]
        )
        confluence_count = sum(
            (
                htf_aligned,
                entry_trigger,
                aligned_sweep,
                aligned_break,
                aligned_fvg,
                aligned_block,
                aligned_displacement,
                ict_sweep and ict_bos,
                entry_fvg is not None,
                views["15m"].volume_z >= 0,
            )
        )
        actionable = (
            confidence >= 72
            and agreement >= 3
            and abs(weighted_score) >= 8.5
            and htf_aligned
            and entry_trigger
            and confluence_count >= 6
            and not daily_veto
            and not too_extended
            and not low_volatility
            and not high_volatility
            and not open_position
        )
        relevance = "ACTIONABLE" if actionable else "NOT_RELEVANT"
        atr_stop = price - sign * max(atr_15m * 1.25, price * 0.0035)
        structure_stop = (
            min(
                value
                for value in (ict_events["sweep_low"], views["15m"].recent_low)
                if value is not None
            )
            - atr_15m * 0.2
            if sign > 0
            else max(
                value
                for value in (ict_events["sweep_high"], views["15m"].recent_high)
                if value is not None
            )
            + atr_15m * 0.2
        )
        stop = min(atr_stop, structure_stop) if sign > 0 else max(atr_stop, structure_stop)
        risk_distance = abs(price - stop)
        target_1 = price + sign * risk_distance * 3.0
        target_2 = price + sign * risk_distance * 5.0

        reasons = [
            f"{agreement}/4 timeframes align {direction.lower()}",
            f"Setup confluence {confluence_count}/10",
            f"1h/4h context {'aligned' if htf_aligned else 'conflicted'}",
            f"15m sweep {'confirmed' if ict_sweep else 'missing'}",
            f"15m BOS {'confirmed' if ict_bos else 'missing'}",
            f"5m FVG retest {'confirmed' if entry_fvg else 'missing'}",
            f"Technical score {weighted_score:.2f}",
            f"Learning model {model_probability * 100:.1f}% over {self.model.samples} samples",
            f"24h change {market_info['change_24h']:.2f}%",
        ]
        if too_extended:
            reasons.append("Move is extended; chasing risk is high")
        if low_volatility:
            reasons.append("15m volatility is too low")
        if high_volatility:
            reasons.append("15m volatility is too high for controlled risk")
        if daily_veto:
            reasons.append("Daily context strongly opposes the setup")
        if open_position:
            reasons.append("A demo position is already open for this market")
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
            "exchange": market_info["exchange"],
            "symbol": market_info["symbol"],
            "direction": direction,
            "relevance": relevance,
            "confidence": round(confidence, 1),
            "price": price,
            "stop_loss": stop if actionable else None,
            "take_profit_1": target_1 if actionable else None,
            "take_profit_2": target_2 if actionable else None,
            "risk_reward": 3.0 if actionable else None,
            "setup_quality": confluence_count,
            "score": round(weighted_score, 3),
            "reasons": reasons,
            "timeframes": {
                timeframe: self._serialize_view(views[timeframe])
                for timeframe in TIMEFRAMES
            },
            "features": features,
        }

    def run_scan(self) -> list[dict]:
        ranked = []
        for market in self.markets:
            try:
                exchange_symbols = market.top_usdt_symbols(self.top_symbols)
                self.market_health[market.name] = {
                    "ok": True,
                    "error": None,
                    "symbols": len(exchange_symbols),
                }
                for item in exchange_symbols:
                    item["exchange"] = market.name
                    item["market"] = market
                ranked.extend(exchange_symbols)
            except Exception as exc:
                self.market_health[market.name] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "symbols": 0,
                }
                print(f"[market] {market.name} discovery failed: {exc}", flush=True)
        prices = {
            f"{item['exchange']}:{item['symbol']}": item["price"] for item in ranked
        }
        self._resolve_old_signals(prices)
        results = []
        for market_info in ranked:
            views = {}
            try:
                market = market_info["market"]
                timeframe_candles = {}
                for timeframe in FETCH_TIMEFRAMES:
                    candles = market.klines(market_info["symbol"], timeframe)
                    timeframe_candles[timeframe] = candles
                    if timeframe in TIMEFRAMES:
                        views[timeframe] = analyze_timeframe(timeframe, candles)
                self.store.evaluate_demo_trades(
                    market_info["exchange"],
                    market_info["symbol"],
                    timeframe_candles["15m"],
                )
                signal = self._build_signal(market_info, views, timeframe_candles)
                signal_id = self.store.add_signal(signal)
                self.store.open_demo_trade(signal_id, signal, notional=10)
                results.append(signal)
                print(
                    f"[signal] {signal['exchange']} {signal['symbol']} {signal['direction']} "
                    f"{signal['confidence']:.1f}% {signal['relevance']}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[signal] {market_info['symbol']} failed: {exc}", flush=True)
        return results
