"""Modelli quantitativi (sez. 16): stima probabilita/expected move indipendente dagli LLM.

MVP: modello empirico bayesiano per-categoria basato sullo storico degli eventi
osservati (event study) + prior conservativo. Puo essere sostituito da
LightGBM/XGBoost quando lo storico e' sufficiente (sez. 16), senza cambiare
l'interfaccia `QuantModel.predict`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from core.enums import Category, Direction


@dataclass
class QuantPrediction:
    probability_direction: float  # P(il prezzo si muove nel verso atteso entro l'orizzonte)
    expected_move_pct: float  # ampiezza attesa (frazione, >=0)
    confidence: float
    model_name: str
    model_version: str
    sample_size: int
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "probability_direction": round(self.probability_direction, 4),
            "expected_move_pct": round(self.expected_move_pct, 6),
            "confidence": round(self.confidence, 3),
            "model": f"{self.model_name}:{self.model_version}",
            "sample_size": self.sample_size,
            **self.details,
        }


@dataclass
class _Bucket:
    n: int = 0
    hits: int = 0
    moves: list[float] = field(default_factory=list)


class EmpiricalBayesQuantModel:
    """Prior Beta(a,b) per categoria aggiornato con esiti osservati.

    `observe(category, hit, move)` viene chiamato dall'evaluation worker quando
    il post-signal alpha e' disponibile (patch sez. 35), cosi il modello impara
    dai dati e non dall'opinione degli LLM.
    """

    name = "empirical_bayes"
    version = "1"

    def __init__(self, prior_hits: float = 2.0, prior_misses: float = 2.0, prior_move_pct: float = 0.002):
        self.prior_hits = prior_hits
        self.prior_misses = prior_misses
        self.prior_move_pct = prior_move_pct
        self._buckets: dict[str, _Bucket] = {}

    def _key(self, category: Category | str, kind: str | None = None) -> str:
        cat = category.value if isinstance(category, Category) else str(category)
        return f"{cat}:{kind}" if kind else cat

    def observe(self, category: Category | str, *, hit: bool, move_pct: float, kind: str | None = None) -> None:
        bucket = self._buckets.setdefault(self._key(category, kind), _Bucket())
        bucket.n += 1
        bucket.hits += int(hit)
        bucket.moves.append(abs(move_pct))
        if len(bucket.moves) > 2000:
            del bucket.moves[:-2000]

    def predict(
        self,
        *,
        category: Category | str,
        kind: str | None,
        surprise_sigma: float | None,
        source_reliability: float,
        freshness_weight: float,
        cross_asset_score: float | None,
        direction: Direction,
    ) -> QuantPrediction:
        bucket = self._buckets.get(self._key(category, kind)) or self._buckets.get(self._key(category)) or _Bucket()
        a = self.prior_hits + bucket.hits
        b = self.prior_misses + (bucket.n - bucket.hits)
        base_p = a / (a + b)

        # aggiustamenti deterministici e limitati: fonte, freshness, cross-asset, sorpresa
        logit = math.log(base_p / (1 - base_p))
        logit += (source_reliability - 0.7) * 1.5
        logit += (freshness_weight - 0.5) * 1.0
        if cross_asset_score is not None:
            logit += cross_asset_score * 0.8
        if surprise_sigma is not None:
            logit += max(-1.0, min(1.0, surprise_sigma / 2.0)) * 0.6
        p = 1 / (1 + math.exp(-logit))
        p = max(0.05, min(0.95, p))

        if bucket.moves:
            ordered = sorted(bucket.moves)
            median_move = ordered[len(ordered) // 2]
        else:
            median_move = self.prior_move_pct
        if surprise_sigma is not None:
            median_move *= max(0.5, min(2.0, abs(surprise_sigma) / 1.5))

        confidence = min(0.9, 0.3 + 0.6 * (bucket.n / (bucket.n + 30)))
        return QuantPrediction(
            probability_direction=p,
            expected_move_pct=median_move,
            confidence=confidence,
            model_name=self.name,
            model_version=self.version,
            sample_size=bucket.n,
            details={"base_p": round(base_p, 4), "direction": direction.value},
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            key: {"n": bucket.n, "hit_rate": (bucket.hits / bucket.n) if bucket.n else None}
            for key, bucket in self._buckets.items()
        }

    def load(self, state: dict[str, Any]) -> None:
        for key, value in state.items():
            bucket = _Bucket(n=int(value.get("n", 0)), hits=int(value.get("hits", 0)))
            bucket.moves = list(value.get("moves", []))
            self._buckets[key] = bucket

    def dump(self) -> dict[str, Any]:
        return {k: {"n": b.n, "hits": b.hits, "moves": b.moves[-500:]} for k, b in self._buckets.items()}


_model: EmpiricalBayesQuantModel | None = None


def get_quant_model() -> EmpiricalBayesQuantModel:
    global _model
    if _model is None:
        _model = EmpiricalBayesQuantModel()
    return _model
