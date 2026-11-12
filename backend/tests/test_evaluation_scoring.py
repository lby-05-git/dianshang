from app.services.evaluation_scoring import build_evaluation_scorecard


def test_scorecard_uses_measured_report_evidence():
    report = {
        "run_id": "run-test",
        "summary": {"total": 2, "passed": 1},
        "cases": [
            {
                "degraded": False,
                "execution": {},
                "judge": {"dimensions": [
                    {"name": "refund_side_effect", "ok": True, "measured": True},
                    {"name": "verification_order", "ok": False, "measured": True},
                ]},
            },
            {
                "degraded": True,
                "execution": {"error": "timeout"},
                "judge": {"dimensions": []},
            },
        ],
    }

    card = build_evaluation_scorecard(report)
    scores = {item["key"]: item["score"] for item in card["dimensions"]}
    assert scores == {
        "business_accuracy": 50.0,
        "safety_compliance": 50.0,
        "runtime_stability": 50.0,
    }


def test_scorecard_does_not_fabricate_unmeasured_scores():
    card = build_evaluation_scorecard(None)
    assert card["status"] == "PENDING"
    assert all(item["score"] is None for item in card["dimensions"])
    assert all(item["status"] == "PENDING" for item in card["dimensions"])
