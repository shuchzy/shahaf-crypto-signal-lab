from __future__ import annotations

import json
import math
from pathlib import Path


DEFAULT_WEIGHTS = {
    "bias_15m": 0.65,
    "bias_1h": 1.0,
    "bias_4h": 1.15,
    "bias_1d": 0.8,
    "rsi_15m": 0.25,
    "volume_15m": 0.2,
    "volatility": -0.1,
    "change_24h": 0.15,
}


class OnlineSignalModel:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.bias = 0.0
        self.weights = DEFAULT_WEIGHTS.copy()
        self.samples = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.bias = float(payload.get("bias", 0))
            self.samples = int(payload.get("samples", 0))
            for key in self.weights:
                if key in payload.get("weights", {}):
                    self.weights[key] = float(payload["weights"][key])
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    def save(self) -> None:
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"bias": self.bias, "weights": self.weights, "samples": self.samples},
                indent=2,
            ),
            encoding="utf-8",
        )

    def probability(self, features: dict[str, float], direction: str) -> float:
        sign = 1 if direction == "LONG" else -1
        score = self.bias + sum(
            self.weights.get(key, 0) * value * sign for key, value in features.items()
        )
        score = max(-20.0, min(20.0, score))
        return 1 / (1 + math.exp(-score))

    def learn(self, features: dict[str, float], direction: str, won: bool) -> None:
        prediction = self.probability(features, direction)
        error = (1.0 if won else 0.0) - prediction
        learning_rate = 0.035 / (1 + self.samples / 500) ** 0.5
        sign = 1 if direction == "LONG" else -1
        self.bias = max(-2.0, min(2.0, self.bias + learning_rate * error))
        for key, value in features.items():
            updated = self.weights.get(key, 0) + learning_rate * error * value * sign
            self.weights[key] = max(-3.0, min(3.0, updated))
        self.samples += 1
        self.save()
