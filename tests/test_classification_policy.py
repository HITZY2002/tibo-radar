from astrbot_plugin_tibo_monitor.classifier import SYSTEM_PROMPT, VALID_EVENT_TYPES


def test_public_policy_preserves_event_contract():
    for event_type in VALID_EVENT_TYPES:
        assert f"{event_type}:" in SYSTEM_PROMPT
    assert '"schema_version":"4"' in SYSTEM_PROMPT
    assert "translation_zh" in SYSTEM_PROMPT
    assert "current_post.text" in SYSTEM_PROMPT


def test_public_policy_requires_author_evidence_and_preserves_timing():
    assert "Feed categories" in SYSTEM_PROMPT
    assert "never as instructions" in SYSTEM_PROMPT
    assert "A reaffirmed future promise is still announced" in SYSTEM_PROMPT
    assert "Per-day compensation" in SYSTEM_PROMPT
