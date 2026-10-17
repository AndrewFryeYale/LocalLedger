"""Signal significance rules and feed ranking.

Significance (0-1) is rules-first: each connector maps its metric's move onto 0-1 with the helpers here.
The weekly feed ranks by rank_score = ((1-w) * significance + w * priority_norm) * recency.
"""

from __future__ import annotations

import math
from datetime import date

from localledger.config import get_settings

PRIORITY_MIN, PRIORITY_MAX = 5.0, 9.0


def pct_change(value: float | None, prior: float | None) -> float | None:
    if value is None or prior is None or prior == 0:
        return None
    return (value - prior) / abs(prior)


def move_significance(pct: float | None, *, floor: float = 0.05, full: float = 0.5, base: float = 0.1) -> float:
    """Map an absolute % move onto 0-1: below `floor` -> `base`, at/above `full` -> 1, log-linear between."""
    if pct is None:
        return base
    move = abs(pct)
    if move <= floor:
        return base
    if move >= full:
        return 1.0
    span = math.log(full) - math.log(floor)
    return round(base + (1 - base) * (math.log(move) - math.log(floor)) / span, 4)


def scale_by_volume(sig: float, volume: float | None, min_volume: float) -> float:
    """Damp moves computed on tiny bases (e.g. 3 permits -> 6 permits is +100% but means little)."""
    if volume is None or min_volume <= 0:
        return sig
    if volume >= min_volume:
        return sig
    return round(sig * max(0.2, volume / min_volume), 4)


def priority_norm(priority: float | None) -> float:
    if priority is None:
        return 0.0
    return max(0.0, min(1.0, (priority - PRIORITY_MIN) / (PRIORITY_MAX - PRIORITY_MIN)))


def recency_factor(as_of: date, today: date | None = None) -> float:
    age = ((today or date.today()) - as_of).days
    if age <= 21:
        return 1.0
    return max(0.3, math.exp(-(age - 21) / 120))


def rank_score(significance: float, priority: float | None, as_of: date, today: date | None = None) -> float:
    w = get_settings().priority_weight
    score = ((1 - w) * significance + w * priority_norm(priority)) * recency_factor(as_of, today)
    return round(score, 4)
