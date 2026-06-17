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
MIN_TARGET_WIN_RATE = 40.0
MIN_SCALP_RISK_REWARD = 1.2


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

    def _market_by_name(self, exchange: str):
        for market in self.markets:
            if market.name == exchange:
                return market
        raise KeyError(exchange)

    def _update_open_demo_trades(self) -> None:
        for trade in self.store.open_demo_positions():
            try:
                market = self._market_by_name(trade["exchange"])
                candles = market.klines(trade["symbol"], "5m", limit=80)
                self.store.evaluate_demo_trades(
                    trade["exchange"],
                    trade["symbol"],
                    candles,
                )
            except Exception as exc:
                print(
                    f"[trade] {trade['exchange']} {trade['symbol']} update failed: {exc}",
                    flush=True,
                )

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
        scalp_view = analyze_timeframe("5m", timeframe_candles["5m"])
        atr_5m = scalp_view.atr
        price = market_info["price"]
        too_extended = abs(market_info["change_24h"]) > 24
        low_volatility = views["15m"].atr_pct < 0.12
        high_volatility = views["15m"].atr_pct > 5.5
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
        scalp_aligned_break = scalp_view.structure_break == (
            "bullish_bos" if sign > 0 else "bearish_bos"
        )
        scalp_aligned_displacement = aligned(scalp_view.displacement)
        scalp_aligned_fvg = bool(scalp_view.fvg and aligned(scalp_view.fvg["direction"]))
        scalp_aligned_block = bool(
            scalp_view.order_block and aligned(scalp_view.order_block["direction"])
        )
        ict_events = recent_ict_events(timeframe_candles["15m"])
        ict_sweep = ict_events["bullish_sweep" if sign > 0 else "bearish_sweep"]
        ict_bos = ict_events["bullish_bos" if sign > 0 else "bearish_bos"]
        entry_fvg = fvg_retest(timeframe_candles["5m"], direction)
        htf_aligned = all(
            (views[timeframe].bias > 0 and sign > 0)
            or (views[timeframe].bias < 0 and sign < 0)
            for timeframe in ("1h", "4h")
        )
        htf_support = sum(
            1
            for timeframe in ("1h", "4h", "1d")
            if (views[timeframe].bias > 0 and sign > 0)
            or (views[timeframe].bias < 0 and sign < 0)
        )
        daily_veto = (
            views["1d"].bias < -3.25 if sign > 0 else views["1d"].bias > 3.25
        )
        open_position = self.store.has_open_demo_trade(
            market_info["exchange"], market_info["symbol"]
        )
        momentum_ok = (
            42 <= views["15m"].rsi <= 78 if sign > 0 else 22 <= views["15m"].rsi <= 58
        )
        scalp_momentum_ok = (
            45 <= scalp_view.rsi <= 82 if sign > 0 else 18 <= scalp_view.rsi <= 55
        )
        scalp_volatility_ok = 0.05 <= scalp_view.atr_pct <= 1.9
        volume_ok = views["15m"].volume_z >= -0.8
        scalp_volume_ok = scalp_view.volume_z >= -0.4
        fast_scalp_setup = bool(
            htf_support >= 1
            and (views["15m"].bias * sign > 0 or scalp_view.bias * sign >= 2.4 or htf_aligned)
            and (scalp_aligned_break or scalp_aligned_displacement)
            and (
                scalp_aligned_fvg
                or scalp_aligned_block
                or entry_fvg
                or abs(scalp_view.bias) >= 3.0
            )
            and scalp_momentum_ok
            and scalp_volatility_ok
            and scalp_volume_ok
            and not daily_veto
        )
        continuation_setup = bool(htf_support >= 2 and (
            ict_bos or aligned_break or aligned_displacement
        ) and (entry_fvg or aligned_fvg or aligned_block))
        pullback_setup = bool(htf_support >= 2 and (
            entry_fvg or aligned_block
        ) and views["15m"].structure in ("HH_HL", "LH_LL"))
        reversal_setup = (
            ict_sweep
            and (ict_bos or aligned_break or aligned_displacement)
            and not daily_veto
        )
        entry_trigger = continuation_setup or pullback_setup or reversal_setup or fast_scalp_setup
        if fast_scalp_setup:
            setup_type = "fast scalp"
        elif reversal_setup:
            setup_type = "liquidity reversal"
        elif pullback_setup:
            setup_type = "trend pullback"
        elif continuation_setup:
            setup_type = "breakout continuation"
        else:
            setup_type = "waiting"
        confluence_count = sum(
            (
                htf_aligned,
                htf_support >= 2,
                entry_trigger,
                aligned_sweep,
                aligned_break,
                aligned_fvg,
                aligned_block,
                aligned_displacement,
                ict_sweep and ict_bos,
                entry_fvg is not None,
                momentum_ok,
                volume_ok,
                fast_scalp_setup,
                scalp_momentum_ok,
                scalp_volume_ok,
            )
        )
        estimated_win_rate = 31.0 + confluence_count * 3.1
        estimated_win_rate += 3.0 if htf_aligned else 0.0
        estimated_win_rate += 2.0 if entry_fvg else 0.0
        estimated_win_rate += 2.0 if aligned_block else 0.0
        estimated_win_rate += 1.5 if momentum_ok else -2.5
        estimated_win_rate += 1.5 if volume_ok else -2.0
        estimated_win_rate += 5.0 if fast_scalp_setup else 0.0
        estimated_win_rate += 2.0 if scalp_momentum_ok and scalp_volume_ok else 0.0
        if self.model.samples >= 40:
            estimated_win_rate = estimated_win_rate * 0.75 + model_probability * 100 * 0.25
        estimated_win_rate = clamp(estimated_win_rate, 5.0, 78.0)
        standard_actionable = (
            confidence >= 64
            and agreement >= 2
            and abs(weighted_score) >= 4.5
            and entry_trigger
            and confluence_count >= 5
            and estimated_win_rate >= MIN_TARGET_WIN_RATE
            and (momentum_ok or fast_scalp_setup)
            and (volume_ok or fast_scalp_setup)
            and not daily_veto
            and not too_extended
            and not low_volatility
            and not high_volatility
            and not open_position
        )
        scalp_actionable = (
            fast_scalp_setup
            and confidence >= 58
            and confluence_count >= 5
            and estimated_win_rate >= 43
            and not too_extended
            and not open_position
        )
        actionable = standard_actionable or scalp_actionable
        relevance = "ACTIONABLE" if actionable else "NOT_RELEVANT"
        risk_reward = 3.0
        if fast_scalp_setup:
            risk_reward = 1.35
            if htf_aligned and confluence_count >= 8:
                risk_reward = 1.8
            if htf_aligned and confluence_count >= 10 and entry_fvg:
                risk_reward = 2.2
        elif reversal_setup:
            risk_reward = 2.0
        elif pullback_setup and confluence_count < 7:
            risk_reward = 2.0
        max_risk_distance = price * (0.009 if fast_scalp_setup else 0.025)
        atr_basis = atr_5m if fast_scalp_setup and atr_5m > 0 else atr_15m
        atr_stop = price - sign * min(
            max(atr_basis * (1.05 if fast_scalp_setup else 1.15), price * 0.0025),
            max_risk_distance,
        )
        structure_stop = (
            min(
                value
                for value in (
                    ict_events["sweep_low"],
                    scalp_view.recent_low if fast_scalp_setup else views["15m"].recent_low,
                )
                if value is not None
            )
            - atr_basis * 0.15
            if sign > 0
            else max(
                value
                for value in (
                    ict_events["sweep_high"],
                    scalp_view.recent_high if fast_scalp_setup else views["15m"].recent_high,
                )
                if value is not None
            )
            + atr_basis * 0.15
        )
        stop = min(atr_stop, structure_stop) if sign > 0 else max(atr_stop, structure_stop)
        risk_distance = abs(price - stop)
        if risk_distance > max_risk_distance:
            stop = price - sign * max_risk_distance
            risk_distance = max_risk_distance
        target_1 = price + sign * risk_distance * risk_reward
        target_2 = price + sign * risk_distance * max(risk_reward * 1.7, risk_reward + 0.8)

        reasons = [
            f"{agreement}/4 timeframes align {direction.lower()}",
            f"Setup type {setup_type}",
            f"Setup confluence {confluence_count}/15",
            f"Dynamic R:R 1:{risk_reward:.2f}",
            f"Estimated win-rate target {estimated_win_rate:.1f}% (demo will verify)",
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
        if not momentum_ok:
            reasons.append("15m RSI is not in the preferred entry zone")
        if not volume_ok:
            reasons.append("Volume confirmation is too weak")
        if fast_scalp_setup:
            reasons.append("Fast scalp mode: tighter stop and quicker target")
        if open_position:
            reasons.append("A demo position is already open for this market")
        if risk_distance >= max_risk_distance:
            reasons.append("Risk capped to keep the 3R target reachable")
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
            "risk_reward": round(risk_reward, 2) if actionable else None,
            "setup_quality": confluence_count,
            "estimated_win_rate": round(estimated_win_rate, 1),
            "setup_type": setup_type,
            "score": round(weighted_score, 3),
            "reasons": reasons,
            "timeframes": {
                timeframe: self._serialize_view(views[timeframe])
                for timeframe in TIMEFRAMES
            },
            "features": features,
        }

    def run_scan(self) -> list[dict]:
        self._update_open_demo_trades()
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
                self.store.open_demo_trade(signal_id, signal)
                results.append(signal)
                print(
                    f"[signal] {signal['exchange']} {signal['symbol']} {signal['direction']} "
                    f"{signal['confidence']:.1f}% {signal['relevance']}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[signal] {market_info['symbol']} failed: {exc}", flush=True)
        return results
