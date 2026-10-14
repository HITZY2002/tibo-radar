"""Regression tests for feed ingestion and durable omission recovery."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_tibo_monitor.classifier import PostClassifier
from astrbot_plugin_tibo_monitor.codex_reset_fetcher import (
    CodexResetFeedError,
    CodexResetFetcher,
)
from astrbot_plugin_tibo_monitor.main import TiboMonitorPlugin
from astrbot_plugin_tibo_monitor.notifier import GroupNotifier
from astrbot_plugin_tibo_monitor.state_manager import StateManager


@pytest.mark.asyncio
async def test_public_feed_is_normalized(monkeypatch):
    fetcher = CodexResetFetcher()
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "content_age_days": 0,
        "stale": False,
        "source_scope": "timeline",
        "tweets": [
            {
                "id": "2097043464538264003",
                "text": "Global reset incoming",
                "at": "2026-09-07T19:24:57Z",
                "url": "https://x.com/thsottiaux/status/2097043464538264003",
                "is_reply": False,
                "kind": "candidate",
            }
        ],
        "events": [
            {
                "id": "2097043464538264003",
                "type": "reset",
                "announcement_state": "announced",
                "confidence": "medium",
            }
        ],
    }
    monkeypatch.setattr(fetcher, "_request_json", lambda: payload)

    posts = await fetcher.fetch_recent()

    assert posts[0]["source"] == "codex-reset"
    assert posts[0]["content_complete"] is True
    assert posts[0]["source_event_type"] == "reset"
    assert fetcher.last_report["posts"] == 1


@pytest.mark.asyncio
async def test_stale_public_feed_forces_fallback(monkeypatch):
    fetcher = CodexResetFetcher(max_age_hours=1)
    monkeypatch.setattr(
        fetcher,
        "_request_json",
        lambda: {
            "fetched_at": "2026-09-01T00:00:00Z",
            "content_age_days": 3,
            "stale": True,
            "tweets": [],
        },
    )

    with pytest.raises(CodexResetFeedError):
        await fetcher.fetch_recent()


@pytest.mark.asyncio
async def test_old_content_does_not_make_fresh_feed_stale(monkeypatch):
    fetcher = CodexResetFetcher(max_age_hours=1)
    monkeypatch.setattr(
        fetcher,
        "_request_json",
        lambda: {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "content_age_days": 3,
            "stale": False,
            "tweets": [],
            "events": [],
        },
    )

    assert await fetcher.fetch_recent() == []
    assert fetcher.last_report["feed_age_hours"] < 0.01
    assert fetcher.last_report["content_age_hours"] == 72


def test_delivery_receipts_and_retry_queue_are_durable(tmp_path):
    manager = StateManager(str(tmp_path))
    manager.set_baseline("100")
    post = {
        "id": "101",
        "text": "We will reset all paid subscriptions.",
        "timestamp": "2026-09-08T00:00:00Z",
        "url": "https://x.com/thsottiaux/status/101",
    }
    finding = {
        "type": "reset_announced",
        "confidence": 0.99,
        "summary": "Reset announced",
    }
    event = manager.add_event(post, finding)
    assert event is not None

    manager.mark_event_notified(event["id"], ["group-a"], ["group-a", "group-b"])
    assert manager.get_pending_events()
    manager.mark_event_notified(event["id"], ["group-b"], ["group-a", "group-b"])
    assert not manager.get_pending_events()

    manager.put_pending_post(post, "temporary provider failure")
    reloaded = StateManager(str(tmp_path))
    assert reloaded.get_delivered_groups("101") == ["group-a", "group-b"]
    assert reloaded.get_pending_posts()[0]["id"] == "101"
    assert json.loads((tmp_path / "monitor_state.json").read_text("utf-8"))


def test_recovered_total_is_rebuilt_after_interrupted_audit(tmp_path):
    manager = StateManager(str(tmp_path))
    manager.state["events"] = [
        {"id": "evt_101", "post_id": "101", "recovered": True},
        {"id": "evt_102", "post_id": "102", "recovered": False},
    ]
    manager.state["recovered_total"] = 0
    manager.save()

    reloaded = StateManager(str(tmp_path))

    assert reloaded.get_status_summary()["recovered_total"] == 1


def test_banked_feed_hint_overrides_reset_category():
    classification = {
        "events": [{"type": "reset_announced", "confidence": 0.7, "reason": ""}]
    }

    result = TiboMonitorPlugin._apply_source_hints(
        {"source_kind": "banked", "source_event_type": "credits"},
        classification,
    )

    assert result["events"][0]["type"] == "compensation"
    assert result["events"][0]["confidence"] == 0.9


def test_vague_context_cannot_create_reset_event():
    classification = {
        "events": [{"type": "reset_completed", "confidence": 0.9, "reason": "context"}]
    }

    result = TiboMonitorPlugin._apply_source_hints(
        {"text": "I think I can officially say: we are so back"},
        classification,
    )

    assert result["events"][0]["type"] == "none"


@pytest.mark.parametrize("source_kind", ["boost", "banked"])
@pytest.mark.parametrize("event_type", ["none", "uncertain"])
def test_positive_feed_label_cannot_create_announcement(source_kind, event_type):
    result = TiboMonitorPlugin._apply_source_hints(
        {
            "source_kind": source_kind,
            "text": "Good defaults matter. You can run it at 1M too but it costs more.",
        },
        {"events": [{"type": event_type, "confidence": 0.95}]},
    )
    assert result["events"][0]["type"] == event_type


def test_boost_label_preserves_confirmed_model_release():
    result = TiboMonitorPlugin._apply_source_hints(
        {"source_kind": "boost", "text": "New model is live."},
        {"events": [{"type": "model_release", "confidence": 0.95}]},
    )
    assert result["events"][0]["type"] == "model_release"


@pytest.mark.parametrize(
    "labels",
    [
        {"source_kind": "other"},
        {"source_event_type": "reset", "source_announcement_state": "none"},
    ],
)
def test_external_negative_label_cannot_erase_reset(labels):
    result = TiboMonitorPlugin._apply_source_hints(
        {"text": "I promised a reset for Tuesday. See you soon.", **labels},
        {"events": [{"type": "reset_announced", "confidence": 0.95}]},
    )
    assert result["events"][0]["type"] == "reset_announced"


def test_obsolete_suppression_is_rechecked_once_without_losing_receipts(tmp_path):
    manager = StateManager(str(tmp_path))
    post = {"id": "101", "text": "I promised a reset for Tuesday."}
    analysis = {
        "events": [
            {"type": "none", "reason": "Public feed marks this as context only."}
        ]
    }
    manager.add_analyzed_post(post, analysis)
    manager.state["posts"][0]["pipeline_version"] = 4
    manager.state["delivery_receipts"]["101"] = ["group-a"]
    assert manager.needs_analysis("101", post)
    manager.add_analyzed_post(post, analysis)
    assert not manager.needs_analysis("101", post)
    assert manager.get_delivered_groups("101") == ["group-a"]


@pytest.mark.asyncio
async def test_notifications_do_not_mention_all():
    context = AsyncMock()
    context.send_message.return_value = True
    assert await GroupNotifier(context).send_text("123456789", "Test notice")
    umo, message = context.send_message.call_args.args
    assert umo == "qq-onebot:GroupMessage:123456789"
    assert len(message.chain) == 1
    assert message.chain[0].type == "Plain"
    assert message.chain[0].text == "Test notice"


def test_classifier_refreshes_replaced_provider():
    context = Mock()
    first, second = Mock(), Mock()
    context.get_provider_by_id.side_effect = [first, second]
    classifier = PostClassifier(context, "test-provider")
    assert classifier._resolve_provider() is first
    assert classifier._resolve_provider() is second


@pytest.mark.asyncio
async def test_classifier_does_not_multiply_outage_retries():
    context = Mock()
    provider = Mock()
    provider.text_chat = AsyncMock(side_effect=RuntimeError("Error code: 503"))
    context.get_provider_by_id.return_value = provider
    classifier = PostClassifier(context, "test-provider")
    with pytest.raises(RuntimeError, match="503"):
        await classifier.classify({"id": "1", "text": "A reset is coming."})
    provider.text_chat.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_queued_backlog_is_not_a_failed_scan(tmp_path, provider_fails):
    plugin = object.__new__(TiboMonitorPlugin)
    plugin._check_lock = asyncio.Lock()
    plugin.state = StateManager(str(tmp_path))
    plugin.state.set_baseline("1")
    plugin.fetch_limit = 2
    plugin.audit_lookback_days = 7
    plugin.x_handle = "thsottiaux"
    plugin.notify_groups = []
    plugin._fetch_posts = AsyncMock(
        return_value=(
            [{"id": str(i), "text": "Ordinary discussion"} for i in (2, 3, 4)],
            {},
            [],
        )
    )
    plugin.classifier = Mock()
    plugin.classifier.classify = AsyncMock(
        return_value={"events": [{"type": "none"}]},
        side_effect=RuntimeError("Error code: 503") if provider_fails else None,
    )
    result = await plugin._run_check(silent=True)
    if provider_fails:
        assert result["pending"] == 3
        assert "classification attempt(s) failed" in result["error"]
        assert plugin.state.state["consecutive_failures"] == 1
    else:
        assert result["pending"] == 1
        assert result["error"] is None
        assert plugin.state.state["consecutive_failures"] == 0
