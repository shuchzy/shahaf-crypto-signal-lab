from __future__ import annotations

import math
from dataclasses import dataclass


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains = []
    losses = []
    for previous, current in zip(values[-period - 1 : -1], values[-period:]):
        change = current - previous
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    ranges = []
    for previous, current in zip(candles[-period - 1 : -1], candles[-period:]):
        ranges.append(
            max(
                current["high"] - current["low"],
                abs(current["high"] - previous["close"]),
                abs(current["low"] - previous["close"]),
            )
        )
    return sum(ranges) / max(1, len(ranges))


def zscore(value: float, values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((item - mean) ** 2 for item in values) / len(values)
    deviation = math.sqrt(variance)
    return 0.0 if deviation == 0 else (value - mean) / deviation


def recent_fvg(candles: list[dict], lookback: int = 40) -> dict | None:
    found = None
    start = max(2, len(candles) - lookback)
    for index in range(start, len(candles)):
        first = candles[index - 2]
        third = candles[index]
        if third["low"] > first["high"]:
            found = {
                "direction": "bullish",
                "low": first["high"],
                "high": third["low"],
                "index": index,
            }
        elif third["high"] < first["low"]:
            found = {
                "direction": "bearish",
                "low": third["high"],
                "high": first["low"],
                "index": index,
            }
    return found


def recent_order_block(candles: list[dict], lookback: int = 35) -> dict | None:
    if len(candles) < 8:
        return None
    current_atr = atr(candles)
    if current_atr <= 0:
        return None
    for index in range(len(candles) - 4, max(1, len(candles) - lookback), -1):
        candle = candles[index]
        next_two = candles[index + 1 : index + 3]
        impulse = next_two[-1]["close"] - candle["close"]
        if candle["close"] < candle["open"] and impulse > 1.35 * current_atr:
            return {
                "direction": "bullish",
                "low": candle["low"],
                "high": candle["open"],
            }
        if candle["close"] > candle["open"] and impulse < -1.35 * current_atr:
            return {
                "direction": "bearish",
                "low": candle["open"],
                "high": candle["high"],
            }
    return None


@dataclass
class TimeframeView:
    timeframe: str
    bias: float
    trend: str
    rsi: float
    atr: float
    atr_pct: float
    volume_z: float
    structure: str
    liquidity_sweep: str | None
    fvg: dict | None
    order_block: dict | None
    wyckoff: str
    reasons: list[str]


def analyze_timeframe(timeframe: str, candles: list[dict]) -> TimeframeView:
    closes = [candle["close"] for candle in candles]
    volumes = [candle["volume"] for candle in candles]
    current = candles[-1]
    ema20 = ema(closes, 20)[-1]
    ema50 = ema(closes, 50)[-1]
    ema200 = ema(closes, 200)[-1]
    current_rsi = rsi(closes)
    current_atr = atr(candles)
    atr_pct = current_atr / current["close"] * 100 if current["close"] else 0
    volume_z = zscore(volumes[-1], volumes[-31:-1])
    recent_high = max(c["high"] for c in candles[-21:-1])
    recent_low = min(c["low"] for c in candles[-21:-1])
    prior_high = max(c["high"] for c in candles[-55:-21])
    prior_low = min(c["low"] for c in candles[-55:-21])

    if current["close"] > ema20 > ema50 > ema200:
        trend, trend_score = "strong_bullish", 2.0
    elif current["close"] > ema20 and ema20 > ema50:
        trend, trend_score = "bullish", 1.2
    elif current["close"] < ema20 < ema50 < ema200:
        trend, trend_score = "strong_bearish", -2.0
    elif current["close"] < ema20 and ema20 < ema50:
        trend, trend_score = "bearish", -1.2
    else:
        trend, trend_score = "range", 0.0

    if recent_high > prior_high and recent_low > prior_low:
        structure, structure_score = "HH_HL", 1.2
    elif recent_high < prior_high and recent_low < prior_low:
        structure, structure_score = "LH_LL", -1.2
    else:
        structure, structure_score = "mixed", 0.0

    sweep = None
    sweep_score = 0.0
    if current["low"] < recent_low and current["close"] > recent_low:
        sweep, sweep_score = "sell_side_sweep", 1.4
    elif current["high"] > recent_high and current["close"] < recent_high:
        sweep, sweep_score = "buy_side_sweep", -1.4

    current_fvg = recent_fvg(candles)
    fvg_score = 0.0
    if current_fvg:
        distance = min(
            abs(current["close"] - current_fvg["low"]),
            abs(current["close"] - current_fvg["high"]),
        )
        if distance <= current_atr * 1.5:
            fvg_score = 0.65 if current_fvg["direction"] == "bullish" else -0.65

    block = recent_order_block(candles)
    block_score = 0.0
    if block and current["low"] <= block["high"] + current_atr and current["high"] >= block["low"] - current_atr:
        block_score = 0.75 if block["direction"] == "bullish" else -0.75

    range_position = (current["close"] - prior_low) / max(prior_high - prior_low, 1e-12)
    if range_position < 0.3 and volume_z > 0.3 and current["close"] > current["open"]:
        wyckoff, wyckoff_score = "possible_accumulation", 0.8
    elif range_position > 0.7 and volume_z > 0.3 and current["close"] < current["open"]:
        wyckoff, wyckoff_score = "possible_distribution", -0.8
    elif trend_score > 0 and current["close"] > recent_high:
        wyckoff, wyckoff_score = "markup", 0.5
    elif trend_score < 0 and current["close"] < recent_low:
        wyckoff, wyckoff_score = "markdown", -0.5
    else:
        wyckoff, wyckoff_score = "neutral", 0.0

    momentum_score = clamp((current_rsi - 50) / 18, -1.0, 1.0)
    bias = trend_score + structure_score + sweep_score + fvg_score + block_score
    bias += wyckoff_score + momentum_score
    reasons = [
        f"Trend {trend}",
        f"Structure {structure}",
        f"RSI {current_rsi:.1f}",
        f"Volume z-score {volume_z:.2f}",
        f"Wyckoff {wyckoff}",
    ]
    if sweep:
        reasons.append(sweep.replace("_", " "))
    if current_fvg:
        reasons.append(f"{current_fvg['direction']} FVG")
    if block:
        reasons.append(f"{block['direction']} order block")
    return TimeframeView(
        timeframe=timeframe,
        bias=bias,
        trend=trend,
        rsi=current_rsi,
        atr=current_atr,
        atr_pct=atr_pct,
        volume_z=volume_z,
        structure=structure,
        liquidity_sweep=sweep,
        fvg=current_fvg,
        order_block=block,
        wyckoff=wyckoff,
        reasons=reasons,
    )


def build_features(views: dict[str, TimeframeView], change_24h: float) -> dict[str, float]:
    return {
        "bias_15m": clamp(views["15m"].bias / 6, -1, 1),
        "bias_1h": clamp(views["1h"].bias / 6, -1, 1),
        "bias_4h": clamp(views["4h"].bias / 6, -1, 1),
        "bias_1d": clamp(views["1d"].bias / 6, -1, 1),
        "rsi_15m": clamp((views["15m"].rsi - 50) / 50, -1, 1),
        "volume_15m": clamp(views["15m"].volume_z / 3, -1, 1),
        "volatility": clamp(views["15m"].atr_pct / 5, 0, 1),
        "change_24h": clamp(change_24h / 20, -1, 1),
    }
