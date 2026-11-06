import os
from functools import lru_cache

PRIORITY_RANK = {"low": 0, "standard": 1, "high": 2}


def _conflict_species():
    raw = os.getenv("CONFLICT_SPECIES", "elephant,buffalo,hippopotamus,lion,leopard,hyena,baboon,crocodile")
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def _high():
    return float(os.getenv("AI_HIGH_CONFIDENCE", "0.80"))


def _low():
    return float(os.getenv("AI_LOW_CONFIDENCE", "0.50"))


def is_conflict(species):
    if not species:
        return False
    s = species.lower()
    return any(c in s for c in _conflict_species())


def evaluate(species, confidence, state):
    if state in ("unavailable", "failed"):
        return (None, "needs_manual_review")
    if species is None or str(species).lower() in ("blank", "vehicle", "human", "person", "no cv result"):
        return ("low", "likely_empty")
    conf = 0.0 if confidence is None else float(confidence)
    conflict = is_conflict(species)
    if conflict and conf >= _high():
        return ("high", "review_urgently")
    if conflict and conf >= _low():
        return ("standard", "review")
    return ("low", "review_low_priority")


def report_priority(predictions):
    best = None
    for p in predictions:
        if getattr(p, "state", None) != "completed":
            continue
        pr = getattr(p, "priority", None)
        if pr is None:
            continue
        if best is None or PRIORITY_RANK.get(pr, -1) > PRIORITY_RANK.get(best, -1):
            best = pr
    return best