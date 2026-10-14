"""状态管理：水位线、已处理帖子、事件记录、生命周期，持久化到 JSON。

Persists plugin-specific retry queues, receipts and event transitions.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

DEFAULT_STATE: dict[str, Any] = {
    "schema_version": 4,
    "classifier_version": 4,
    "baseline_id": None,
    "pending_posts": {},
    "delivery_receipts": {},
    "last_audit_epoch": 0,
    "last_audit_at": None,
    "last_audit_result": {},
    "last_scan": {},
    "recovered_total": 0,
    "baseline_established": False,
    "high_water_id": None,
    "seen_ids": [],
    "posts": [],  # 已分析的帖子记录（保留最近 200 条）
    "events": [],  # Detected reset or important product events
    "lifecycle": {
        "status": "idle",  # idle / planned / completed / cancelled
        "cycle": 0,
        "expected_at": None,
        "completed_at": None,
        "last_post_id": "0",
    },
    "last_check_at": None,
    "last_check_error": None,
    "last_check_fetched": 0,
    "last_check_new": 0,
    "last_check_new_events": 0,
    "consecutive_failures": 0,
}

MAX_POSTS_RETAINED = 1000
MAX_EVENTS_RETAINED = 1000
MAX_SEEN_IDS = 2000
PIPELINE_VERSION = 5


class StateManager:
    """监控状态管理器，线程不安全（AstrBot 插件单线程 async 足够）。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.state_file = os.path.join(data_dir, "monitor_state.json")
        self.state: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    self.state = json.load(f)
                if "baseline_id" not in self.state and self.state.get(
                    "baseline_established"
                ):
                    analyzed = {p["post"]["id"] for p in self.state.get("posts", [])}
                    baseline = []
                    for post_id in self.state.get("seen_ids", []):
                        if post_id in analyzed:
                            break
                        baseline.append(post_id)
                    self.state["baseline_id"] = (
                        max(baseline, key=int) if baseline else "0"
                    )
                # Fill missing top-level and nested fields without replacing data.
                for k, v in DEFAULT_STATE.items():
                    if k not in self.state:
                        self.state[k] = (
                            v
                            if not isinstance(v, (dict, list))
                            else json.loads(json.dumps(v))
                        )
                self.state["classifier_version"] = DEFAULT_STATE["classifier_version"]
                self.state["schema_version"] = 4
                for key, value in DEFAULT_STATE["lifecycle"].items():
                    self.state["lifecycle"].setdefault(key, value)
                for event in self.state["events"]:
                    if event.get("notified"):
                        self.state["delivery_receipts"].setdefault(
                            event["post_id"], event.get("notify_groups", []).copy()
                        )
                # Rebuild this counter from durable event history when an older or
                # interrupted audit discovered events before record_scan ran.
                recovered_in_history = sum(
                    1 for event in self.state["events"] if event.get("recovered")
                )
                self.state["recovered_total"] = max(
                    int(self.state.get("recovered_total", 0)),
                    recovered_in_history,
                )
                self._save()
                logger.info(
                    f"[tibo] 状态已加载，已处理 {len(self.state.get('seen_ids', []))} 条帖子，{len(self.state.get('events', []))} 个事件"
                )
                return
            except Exception as e:
                raise RuntimeError(
                    "Tibo state is unreadable; refusing to erase notification history"
                ) from e
        self.state = json.loads(json.dumps(DEFAULT_STATE))
        self._save()

    def _save(self) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            temporary = Path(self.state_file).with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            temporary.replace(self.state_file)
        except Exception as e:
            raise RuntimeError("Failed to persist Tibo state") from e

    # ---------- 水位线 / 去重 ----------

    def get_high_water_id(self) -> str | None:
        return self.state.get("high_water_id")

    def get_baseline_id(self) -> str:
        """Return the first-install cutoff used to suppress historical alerts."""
        return str(self.state.get("baseline_id") or "0")

    def is_seen(self, post_id: str) -> bool:
        return post_id in self.state.get("seen_ids", [])

    def mark_seen(self, post_id: str) -> None:
        seen = self.state.setdefault("seen_ids", [])
        if post_id not in seen:
            seen.append(post_id)
        if len(seen) > MAX_SEEN_IDS:
            self.state["seen_ids"] = seen[-MAX_SEEN_IDS:]
        # 更新水位线（取更大的 snowflake ID）
        try:
            current = self.state.get("high_water_id")
            if current is None or int(post_id) > int(current):
                self.state["high_water_id"] = post_id
        except (ValueError, TypeError):
            pass

    # ---------- 帖子记录 ----------

    def add_analyzed_post(
        self, post: dict[str, Any], analysis_result: dict[str, Any]
    ) -> None:
        """记录一条已分析的帖子。"""
        record = {
            "post": {
                "id": post.get("id"),
                "timestamp": post.get("timestamp"),
                "text": post.get("text", ""),
                "content_complete": bool(post.get("content_complete", True)),
                "source": post.get("source", "unknown"),
                "url": post.get("url"),
                "isReply": post.get("isReply", False),
                "isQuote": post.get("isQuote", False),
                "isRetweet": post.get("isRetweet", False),
            },
            "analysis_status": "complete",
            "pipeline_version": PIPELINE_VERSION,
            "analysis_result": analysis_result,
            "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        posts = self.state.setdefault("posts", [])
        posts[:] = [r for r in posts if r["post"]["id"] != post["id"]]
        posts.insert(0, record)
        if len(posts) > MAX_POSTS_RETAINED:
            self.state["posts"] = posts[:MAX_POSTS_RETAINED]

    def needs_analysis(
        self, post_id: str, current_post: dict[str, Any] | None = None
    ) -> bool:
        """Return whether a post is missing analysis or now has fuller text."""
        for record in self.state.get("posts", []):
            if str(record.get("post", {}).get("id")) != str(post_id):
                continue
            stored_post = record.get("post", {})
            if int(record.get("pipeline_version", 0)) < 5 and any(
                event.get("type") == "none"
                and any(
                    marker in event.get("reason", "")
                    for marker in (
                        "Public feed marks this as context only.",
                        "Public Codex Reset feed routing applied.",
                    )
                )
                for event in record.get("analysis_result", {}).get("events", [])
            ):
                return True
            if record.get("analysis_status") != "complete":
                return True
            if stored_post.get("content_complete") is False:
                return True
            if current_post:
                stored_text = str(stored_post.get("text", "")).strip()
                current_text = str(current_post.get("text", "")).strip()
                if current_text and len(current_text) > len(stored_text) + 20:
                    return True
            return False
        return True

    def put_pending_post(self, post: dict[str, Any], error: str) -> None:
        """Persist a post so transient fetch or classification failures are retried."""
        post_id = str(post.get("id", ""))
        if not post_id:
            return
        pending = self.state.setdefault("pending_posts", {})
        previous = pending.get(post_id, {})
        pending[post_id] = {
            "post": post,
            "attempts": int(previous.get("attempts", 0)) + 1,
            "last_error": error[:500],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._save()

    def clear_pending_post(self, post_id: str) -> None:
        """Remove a successfully processed post from the retry queue."""
        self.state.setdefault("pending_posts", {}).pop(str(post_id), None)

    def get_pending_posts(self) -> list[dict[str, Any]]:
        """Return posts waiting for content recovery or classification retry."""
        return [
            item.get("post", {})
            for item in self.state.get("pending_posts", {}).values()
            if item.get("post")
        ]

    def get_recent_context_posts(
        self, exclude_id: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """获取最近已分析的帖子作为上下文（排除当前帖子）。"""
        result = []
        for record in self.state.get("posts", []):
            p = record.get("post", {})
            if (
                int(p.get("id", "0")) < int(exclude_id)
                and record.get("analysis_status") == "complete"
            ):
                result.append(p)
            if len(result) >= limit:
                break
        return result

    # ---------- 事件 ----------

    def event_exists(self, post_id: str, event_type: str) -> bool:
        for e in self.state.get("events", []):
            if e.get("post_id") == post_id and e.get("type") == event_type:
                return True
        return False

    def add_event(
        self, post: dict[str, Any], finding: dict[str, Any], translation_zh: str = ""
    ) -> dict[str, Any] | None:
        """添加一个重要事件。如果已存在相同 post_id+type 则返回 None。"""
        post_id = post.get("id", "")
        event_type = finding.get("type", "none")
        if event_type == "none":
            return None
        if any(e.get("post_id") == post_id for e in self.state["events"]):
            return None
        if post_id in self.state["delivery_receipts"]:
            return None

        event = {
            "id": f"evt_{post_id}",
            "post_id": post_id,
            "type": event_type,
            "confidence": finding.get("confidence", 0),
            "explicit": finding.get("explicit", False),
            "needs_human_review": finding.get("needs_human_review", False),
            "cycle": self.state.get("lifecycle", {}).get("cycle", 0),
            "effective_at": finding.get("effective_at"),
            "time_text": finding.get("time_text", ""),
            "summary": finding.get("summary", ""),
            "translation_zh": translation_zh,
            "evidence": finding.get("evidence", []),
            "reason": finding.get("reason", ""),
            "post_url": post.get("url", ""),
            "post_text": post.get("text", ""),
            "post_timestamp": post.get("timestamp", ""),
            "recovered": bool(post.get("recovered")),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "notified": False,
            "notified_at": None,
            "notify_groups": [],
        }
        self._apply_lifecycle(event)
        events = self.state.setdefault("events", [])
        events.insert(0, event)
        if len(events) > MAX_EVENTS_RETAINED:
            self.state["events"] = events[:MAX_EVENTS_RETAINED]
        return event

    def get_delivered_groups(self, post_id: str) -> list[str]:
        """Return durable per-group delivery receipts for one source post."""
        return list(self.state.get("delivery_receipts", {}).get(str(post_id), []))

    def mark_event_notified(
        self,
        event_id: str,
        groups: list[str],
        targets: list[str] | None = None,
    ) -> None:
        for e in self.state.get("events", []):
            if e.get("id") == event_id:
                receipt = self.state["delivery_receipts"].setdefault(e["post_id"], [])
                for group in groups:
                    if group not in receipt:
                        receipt.append(group)
                e["notified"] = set(targets or groups).issubset(receipt)
                e["notified_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                e["notify_groups"] = receipt.copy()
                self._save()
                break

    def get_pending_events(self) -> list[dict[str, Any]]:
        """获取尚未通知的有效事件。"""
        return [
            e
            for e in self.state.get("events", [])
            if not e.get("notified")
            and not e.get("needs_human_review")
            and e.get("type")
            in (
                "reset_announced",
                "reset_completed",
                "model_release",
                "price_reduction",
                "usage_policy_change",
                "compensation",
            )
        ]

    def get_recent_events(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.state.get("events", [])[:limit]

    # ---------- 生命周期 ----------

    def _apply_lifecycle(self, event: dict[str, Any]) -> None:
        """根据事件更新生命周期状态。"""
        lifecycle = self.state.setdefault("lifecycle", {"status": "idle", "cycle": 0})
        event_type = event.get("type")
        event["cycle"] = lifecycle.get("cycle", 0)
        if int(event["post_id"]) <= int(lifecycle.get("last_post_id", "0")):
            return
        if event_type == "reset_announced":
            if lifecycle.get("status") in ("completed", "cancelled"):
                lifecycle["cycle"] = int(lifecycle.get("cycle", 0)) + 1
            lifecycle["status"] = "planned"
            lifecycle["expected_at"] = event.get("effective_at")
        elif event_type == "reset_completed":
            lifecycle["status"] = "completed"
            lifecycle["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        elif event_type == "reset_cancelled":
            lifecycle["status"] = "cancelled"
        else:
            return
        lifecycle["last_post_id"] = event["post_id"]
        event["cycle"] = lifecycle.get("cycle", 0)

    def get_lifecycle(self) -> dict[str, Any]:
        return self.state.get("lifecycle", {}).copy()

    # ---------- 检查统计 ----------

    def record_check(
        self, fetched: int, new_count: int, new_events: int, error: str | None = None
    ) -> None:
        self.state["last_check_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.state["last_check_fetched"] = fetched
        self.state["last_check_new"] = new_count
        self.state["last_check_new_events"] = new_events
        self.state["last_check_error"] = error
        if error:
            self.state["consecutive_failures"] = (
                int(self.state.get("consecutive_failures", 0)) + 1
            )
        else:
            self.state["consecutive_failures"] = 0
        self._save()

    def record_scan(
        self,
        scan: dict[str, Any],
        *,
        audit: bool = False,
        recovered: int = 0,
    ) -> None:
        """Record source coverage and optional deep-audit statistics."""
        self.state["last_scan"] = scan
        if recovered:
            self.state["recovered_total"] = (
                int(self.state.get("recovered_total", 0)) + recovered
            )
        if audit:
            self.state["last_audit_epoch"] = int(time.time())
            self.state["last_audit_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.state["last_audit_result"] = scan

    def get_status_summary(self) -> dict[str, Any]:
        return {
            "baseline_established": self.state.get("baseline_established", False),
            "high_water_id": self.state.get("high_water_id"),
            "seen_count": len(self.state.get("seen_ids", [])),
            "event_count": len(self.state.get("events", [])),
            "pending_notify": len(self.get_pending_events()),
            "lifecycle": self.get_lifecycle(),
            "last_check_at": self.state.get("last_check_at"),
            "last_check_error": self.state.get("last_check_error"),
            "consecutive_failures": self.state.get("consecutive_failures", 0),
            "pending_classify": len(self.state["pending_posts"]),
            "last_scan": self.state["last_scan"],
            "last_audit_at": self.state["last_audit_at"],
            "last_audit_result": self.state["last_audit_result"],
            "recovered_total": self.state["recovered_total"],
        }

    def set_baseline(self, high_water_id: str) -> None:
        """首次成功抓取后建立基线，不补发历史提醒。"""
        self.state["baseline_established"] = True
        self.state["high_water_id"] = high_water_id
        self.state["baseline_id"] = high_water_id
        self._save()

    def save(self) -> None:
        self._save()
