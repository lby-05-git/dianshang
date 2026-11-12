from decimal import Decimal

from app.domain.models import PolicyProfile
from app.services.policy_service import _resolve_as_dict


def test_policy_profile_serialization_uses_runtime_values():
    profile = PolicyProfile(
        id="policy-test",
        name="默认策略",
        amount_threshold=Decimal("300.00"),
        ocr_confidence=60,
        fraud_pass=20,
        fraud_suspend=60,
        blacklist_autoreject=True,
        sla_remind_hr=4,
        sla_escalate_hr=24,
        is_default=True,
    )

    result = _resolve_as_dict(profile)

    assert result["amount_threshold"] == 300.0
    assert result["ocr_confidence"] == 0.6
    assert result["is_default"] is True
