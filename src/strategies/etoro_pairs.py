"""Screener pairs trading (mean-reversion, market-neutral): correlazione dei
rendimenti + z-score dello spread di prezzo. Nessuna dipendenza da notizie o
LLM - segnale puramente statistico, l'opposto della strategia momentum+
catalizzatore (che insegna un salto GIA' avvenuto). Qui non si scommette
sulla direzione del mercato: si compra il titolo relativamente a sconto e si
vende allo scoperto quello relativamente caro dentro una coppia storicamente
correlata, scommettendo che lo spread torni alla media.
"""
from __future__ import annotations

from dataclasses import dataclass

from collectors.etoro.instruments import InstrumentCandidate
from collectors.etoro.rates import DailyCandle
from core.enums import Direction

MIN_CORRELATION = 0.75
ENTRY_Z_SCORE = 2.0
LOOKBACK_SESSIONS = 60
MIN_HISTORY_LENGTH = LOOKBACK_SESSIONS + 1


@dataclass
class PairSignal:
    instrument_a_id: int
    instrument_a_name: str
    instrument_b_id: int
    instrument_b_name: str
    price_a: float
    price_b: float
    correlation: float
    z_score: float
    direction_a: Direction
    direction_b: Direction


def _daily_returns(closes: list[float]) -> list[float]:
    return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] > 0]


def _pearson(x: list[float], y: list[float]) -> float:
    n = min(len(x), len(y))
    if n < 2:
        return 0.0
    x, y = x[-n:], y[-n:]
    mean_x, mean_y = sum(x) / n, sum(y) / n
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    var_x = sum((v - mean_x) ** 2 for v in x)
    var_y = sum((v - mean_y) ** 2 for v in y)
    if var_x <= 0 or var_y <= 0:
        return 0.0
    return cov / (var_x**0.5 * var_y**0.5)


def find_pair_signals(pairs: list[tuple[InstrumentCandidate, list[DailyCandle]]]) -> list[PairSignal]:
    # Correlazione e rapporto medio/dev.std si stabiliscono sul periodo STORICO
    # (tutto tranne l'ultima seduta), lo z-score confronta il rapporto ATTUALE
    # contro quella baseline: includere il giorno di divergenza nella stessa
    # finestra usata per misurare la correlazione e' auto-referenziale - una
    # divergenza abbastanza forte da generare segnale finisce per far crollare
    # anche la correlazione misurata, il filtro che dovrebbe convalidare il
    # segnale lo uccide da solo.
    eligible = [(inst, hist) for inst, hist in pairs if len(hist) >= MIN_HISTORY_LENGTH]
    out: list[PairSignal] = []
    for i in range(len(eligible)):
        inst_a, hist_a = eligible[i]
        closes_a = [c.close for c in hist_a[-LOOKBACK_SESSIONS:]]
        history_a, today_a = closes_a[:-1], closes_a[-1]
        returns_a = _daily_returns(history_a)
        for j in range(i + 1, len(eligible)):
            inst_b, hist_b = eligible[j]
            closes_b = [c.close for c in hist_b[-LOOKBACK_SESSIONS:]]
            history_b, today_b = closes_b[:-1], closes_b[-1]
            returns_b = _daily_returns(history_b)
            correlation = _pearson(returns_a, returns_b)
            if correlation < MIN_CORRELATION:
                continue

            ratio = [a / b for a, b in zip(history_a, history_b) if b > 0]
            if len(ratio) < 2 or today_b <= 0:
                continue
            mean_ratio = sum(ratio) / len(ratio)
            variance = sum((r - mean_ratio) ** 2 for r in ratio) / len(ratio)
            std_ratio = variance**0.5
            if std_ratio <= 0:
                continue
            z_score = (today_a / today_b - mean_ratio) / std_ratio
            if abs(z_score) < ENTRY_Z_SCORE:
                continue

            # z > 0: A e' relativamente CARO rispetto a B -> short A, long B.
            # z < 0: A e' relativamente A SCONTO -> long A, short B.
            direction_a = Direction.SELL if z_score > 0 else Direction.BUY
            direction_b = Direction.BUY if z_score > 0 else Direction.SELL
            out.append(
                PairSignal(
                    instrument_a_id=inst_a.instrument_id, instrument_a_name=inst_a.name,
                    instrument_b_id=inst_b.instrument_id, instrument_b_name=inst_b.name,
                    price_a=closes_a[-1], price_b=closes_b[-1],
                    correlation=correlation, z_score=z_score,
                    direction_a=direction_a, direction_b=direction_b,
                )
            )
    out.sort(key=lambda s: abs(s.z_score), reverse=True)
    return out
