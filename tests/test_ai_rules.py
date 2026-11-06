import pytest
from app.ai_rules import evaluate, is_conflict, report_priority


@pytest.mark.parametrize("species,conf,state,exp", [
    ("african elephant", 0.9, "completed", ("high", "review_urgently")),
    ("african elephant", 0.8, "completed", ("high", "review_urgently")),
    ("african elephant", 0.6, "completed", ("standard", "review")),
    ("african elephant", 0.5, "completed", ("standard", "review")),
    ("african elephant", 0.4, "completed", ("low", "review_low_priority")),
    ("giraffe", 0.9, "completed", ("low", "review_low_priority")),
    ("blank", 0.9, "completed", ("low", "likely_empty")),
    (None, 0.9, "completed", ("low", "likely_empty")),
    ("african elephant", None, "completed", ("low", "review_low_priority")),
    ("african elephant", 0.9, "unavailable", (None, "needs_manual_review")),
    ("african elephant", 0.9, "failed", (None, "needs_manual_review")),
])
def test_evaluate(species, conf, state, exp):
    assert evaluate(species, conf, state) == exp


def test_report_priority():
    class P:
        def __init__(self, state, priority):
            self.state = state
            self.priority = priority

    high = P("completed", "high")
    low = P("completed", "low")
    pending = P("pending", "high")
    assert report_priority([high, low]) == "high"
    assert report_priority([pending, low]) == "low"
    assert report_priority([pending]) is None
    assert report_priority([]) is None