from sonder_runtime.domain.session_privacy import EventPrivacyClass, export_decision, rule_for


def test_privacy_rules_make_export_and_retention_explicit():
    assert rule_for(EventPrivacyClass.PUBLIC_METADATA).allow_export
    assert rule_for(EventPrivacyClass.SENSITIVE_CONTENT).retention_days == 30
    assert export_decision(EventPrivacyClass.SECRET) == {"allowed": False, "redact": True}
