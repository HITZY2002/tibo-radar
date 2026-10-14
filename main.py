"""Tibo Codex 重要公告监控插件 - 主入口。

监控 @thsottiaux 的 X 动态，AI 判断 GPT/Codex 重要公告并 QQ 群提醒。
The public distribution uses its own classification policy and browser collector.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .classifier import PostClassifier
from .codex_reset_fetcher import CodexResetFeedError, CodexResetFetcher
from .notifier import GroupNotifier
from .state_manager import PIPELINE_VERSION, StateManager
from .x_fetcher import XFetcher, XFetchError

PLUGIN_NAME = "astrbot_plugin_tibo_monitor"
CMD_PREFIX = "/tibo"


@register(
    PLUGIN_NAME,
    "HITZY2002",
    "监控 Tibo X 动态，AI 判断 Codex/GPT 重要产品公告并 QQ 群提醒",
    "0.3.0",
    "https://github.com/HITZY2002/tibo-radar",
)
class TiboMonitorPlugin(Star):
    """Tibo Codex 重要公告监控插件。"""

    def __init__(self, context: Context, config):
        super().__init__(context, config)
        self.context = context
        self.config = config

        # 解析配置
        self.enable_monitoring = bool(config.get("enable_monitoring", False))
        self.x_handle = str(config.get("x_handle", "thsottiaux")).strip()
        self.poll_interval = int(config.get("poll_interval_minutes", 15)) * 60
        self.fetch_limit = int(config.get("fetch_limit", 30))
        self.use_codex_reset_api = bool(config.get("use_codex_reset_api", True))
        self.codex_reset_feed_url = str(
            config.get("codex_reset_feed_url", "https://codex-reset.com/api/feed")
        ).strip()
        self.audit_interval = max(1, int(config.get("audit_interval_hours", 6))) * 3600
        self.audit_lookback_days = max(1, int(config.get("audit_lookback_days", 7)))
        self.audit_fetch_limit = max(
            self.fetch_limit, int(config.get("audit_fetch_limit", 80))
        )
        self.notify_groups = self._parse_group_list(config.get("notify_groups", ""))
        self.notify_on_announced = bool(config.get("notify_on_announced", True))
        self.notify_on_completed = bool(config.get("notify_on_completed", True))
        self.announced_template = (
            str(config.get("announced_template", ""))
            or self._default_announced_template()
        )
        self.completed_template = (
            str(config.get("completed_template", ""))
            or self._default_completed_template()
        )
        self.important_template = (
            str(config.get("important_template", ""))
            or self._default_important_template()
        )
        self.llm_provider_id = str(config.get("llm_provider_id", "")).strip()
        self.node_executable = str(config.get("node_executable", "")).strip()
        self.firefox_profile_path = str(config.get("firefox_profile_path", "")).strip()
        self.firefox_executable_path = str(
            config.get("firefox_executable_path", "")
        ).strip()

        # 自动检测 Firefox profile（如果未配置）
        if not self.firefox_profile_path:
            self.firefox_profile_path = self._detect_firefox_profile()

        # 插件目录
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = os.path.join(self.plugin_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)

        # 初始化模块
        self.state = StateManager(self.data_dir)
        self.classifier = PostClassifier(context, provider_id=self.llm_provider_id)
        self.fetcher = XFetcher(
            plugin_dir=self.plugin_dir,
            node_executable=self.node_executable,
            firefox_profile_path=self.firefox_profile_path,
            firefox_executable_path=self.firefox_executable_path,
        )
        self.feed_fetcher = CodexResetFetcher(self.codex_reset_feed_url)
        self.notifier = GroupNotifier(context)

        # 定时任务
        self._poll_task: asyncio.Task | None = None
        self._poll_running = False
        self._check_lock = asyncio.Lock()

        logger.info(
            f"[tibo] 插件初始化完成，监控 @{self.x_handle}，轮询间隔 {self.poll_interval // 60} 分钟，提醒群 {self.notify_groups}"
        )
        if self.enable_monitoring:
            logger.info("[tibo] 监控已启用，将在初始化后启动轮询")
        else:
            logger.info("[tibo] 监控未启用，使用 /tibo enable 启用")

    # ---------- 生命周期 ----------

    async def initialize(self):
        """插件初始化完成后启动定时轮询。"""
        if self.enable_monitoring:
            await self._start_polling()

    async def terminate(self):
        """插件卸载时停止轮询并保存状态。"""
        polling_task = self._poll_task
        self._stop_polling()
        if polling_task:
            await asyncio.gather(polling_task, return_exceptions=True)
        try:
            self.state.save()
        except Exception:
            pass
        logger.info("[tibo] 插件已终止")

    # ---------- 定时轮询 ----------

    async def _start_polling(self):
        if self._poll_task and not self._poll_task.done():
            logger.warning("[tibo] 轮询任务已在运行")
            return
        self._poll_running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(f"[tibo] 定时轮询已启动，间隔 {self.poll_interval // 60} 分钟")

    def _stop_polling(self):
        self._poll_running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = None

    async def _poll_loop(self):
        """定时轮询主循环。"""
        # 启动后延迟 30 秒再第一次检查，给 AstrBot 完全启动的时间
        await asyncio.sleep(30)
        while self._poll_running:
            try:
                last_audit = int(self.state.state.get("last_audit_epoch", 0))
                audit_due = time.time() - last_audit >= self.audit_interval
                await self._run_check(silent=True, audit=audit_due)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[tibo] 轮询异常: {e}")
            # 等待下一轮
            await asyncio.sleep(self.poll_interval)

    # ---------- 核心检查流程 ----------

    async def _fetch_posts(
        self, audit: bool
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        """Fetch from the public feed and optionally run the local X audit."""
        posts_by_id: dict[str, dict[str, Any]] = {}
        sources: dict[str, Any] = {}
        errors: list[str] = []

        if self.use_codex_reset_api:
            try:
                api_posts = await self.feed_fetcher.fetch_recent(
                    limit=self.audit_fetch_limit
                )
                sources["codex-reset"] = {"ok": True, **self.feed_fetcher.last_report}
                posts_by_id.update({post["id"]: post for post in api_posts})
            except CodexResetFeedError as exc:
                sources["codex-reset"] = {"ok": False, "error": str(exc)}
                errors.append(str(exc))

        run_x = audit or not posts_by_id
        if run_x:
            try:
                x_posts = await self.fetcher.fetch_recent(
                    handle=self.x_handle,
                    limit=self.audit_fetch_limit if audit else self.fetch_limit,
                    since_id=None,
                    lookback_days=self.audit_lookback_days,
                )
                x_report = self.fetcher.last_report
                sources["x-local"] = {
                    "ok": True,
                    "posts": len(x_posts),
                    "routes": x_report.get("sources", {}),
                    "route_errors": x_report.get("errors", []),
                    "expanded": x_report.get("expanded", 0),
                    "details_failed": x_report.get("details_failed", []),
                }
                for post in x_posts:
                    post["source"] = "x-local"
                    existing = posts_by_id.get(post["id"])
                    source_metadata = {
                        key: value
                        for key, value in (existing or {}).items()
                        if key.startswith("source_")
                    }
                    if (
                        existing is None
                        or (
                            bool(post.get("content_complete", True))
                            and not bool(existing.get("content_complete", True))
                        )
                        or (
                            bool(post.get("content_complete", True))
                            == bool(existing.get("content_complete", True))
                            and len(post.get("text") or "")
                            > len(existing.get("text") or "")
                        )
                    ):
                        for key, value in source_metadata.items():
                            post.setdefault(key, value)
                        posts_by_id[post["id"]] = post
            except XFetchError as exc:
                sources["x-local"] = {"ok": False, "error": str(exc)}
                errors.append(str(exc))

        if not posts_by_id:
            raise XFetchError(
                "; ".join(errors) or "No monitoring source returned posts"
            )
        posts = sorted(
            posts_by_id.values(), key=lambda post: int(post["id"]), reverse=True
        )
        return posts, sources, errors

    async def _run_check(
        self, silent: bool = False, audit: bool = False
    ) -> dict[str, Any]:
        """Fetch, recover omissions, classify, persist, and notify once."""
        result: dict[str, Any] = {
            "fetched": 0,
            "new": 0,
            "new_events": 0,
            "notified": 0,
            "recovered": 0,
            "pending": 0,
            "audit": audit,
            "sources": {},
            "error": None,
        }
        if self._check_lock.locked():
            result["error"] = "另一轮检查仍在运行"
            return result

        async with self._check_lock:
            try:
                posts, sources, source_errors = await self._fetch_posts(audit)
                result["sources"] = sources
                result["fetched"] = len(posts)

                if not self.state.get_status_summary()["baseline_established"]:
                    newest_id = posts[0].get("id")
                    if newest_id:
                        self.state.set_baseline(newest_id)
                        for post in posts:
                            self.state.mark_seen(post.get("id", ""))
                    scan = {**result, "baseline_created": bool(newest_id)}
                    self.state.record_scan(scan, audit=audit)
                    self.state.record_check(result["fetched"], 0, 0)
                    return result

                high_water = int(self.state.get_high_water_id() or "0")
                baseline = int(self.state.get_baseline_id())
                for pending_post in self.state.get_pending_posts():
                    posts.insert(0, pending_post)
                # Revisit recent posts suppressed by obsolete feed-routing rules.
                cutoff_ms = (time.time() - self.audit_lookback_days * 86400) * 1000
                for record in self.state.state.get("posts", []):
                    old_post = record.get("post", {})
                    old_id = str(old_post.get("id", ""))
                    if (
                        old_id.isdigit()
                        and (int(old_id) >> 22) + 1288834974657 >= cutoff_ms
                        and self.state.needs_analysis(old_id, old_post)
                    ):
                        posts.insert(0, old_post)
                posts_by_id = {str(post.get("id")): post for post in posts}

                candidates = [
                    post
                    for post in posts_by_id.values()
                    if str(post.get("id", "")).isdigit()
                    and int(post["id"]) > baseline
                    and self.state.needs_analysis(post["id"], post)
                ]
                candidates.sort(key=lambda post: int(post["id"]))
                result["new"] = len(candidates)
                logger.info(
                    f"[tibo] processing {len(candidates)} candidate post(s), audit={audit}"
                )
                # Limit each scan so a historical backlog cannot delay new alerts.
                for waiting_post in candidates[: -self.fetch_limit]:
                    if str(waiting_post["id"]) not in self.state.state["pending_posts"]:
                        self.state.put_pending_post(waiting_post, "Queued for recovery")
                candidates = candidates[-self.fetch_limit :]

                classification_failures = 0
                incomplete_posts = 0
                for index, post in enumerate(candidates):
                    post_id = str(post["id"])
                    recovered = self.state.is_seen(post_id) or int(post_id) < high_water
                    post["recovered"] = recovered
                    if post.get("isRetweet", False):
                        self.state.mark_seen(post_id)
                        self.state.clear_pending_post(post_id)
                        self.state.save()
                        continue
                    if not str(post.get("text") or "").strip():
                        self.state.mark_seen(post_id)
                        self.state.clear_pending_post(post_id)
                        continue
                    if not post.get("content_complete", True):
                        incomplete_posts += 1
                        self.state.put_pending_post(post, "source text is incomplete")
                        result["pending"] += 1
                        continue

                    try:
                        classification = await self.classifier.classify(
                            post=post,
                            context_posts=self.state.get_recent_context_posts(post_id),
                            lifecycle=self.state.get_lifecycle(),
                            handle=self.x_handle,
                        )
                        classification = self._apply_source_hints(post, classification)
                        self.state.add_analyzed_post(post, classification)
                        self.state.mark_seen(post_id)
                        self.state.clear_pending_post(post_id)
                        self.state.save()
                    except Exception as exc:
                        classification_failures += 1
                        self.state.put_pending_post(post, str(exc))
                        result["pending"] += 1
                        logger.warning(
                            f"[tibo] post {post_id} classification failed: {exc}"
                        )
                        if isinstance(exc.__cause__, TimeoutError) or any(
                            marker in str(exc).lower()
                            for marker in (
                                "error code: 4",
                                "error code: 5",
                                "insufficient credits",
                                "connection error",
                            )
                        ):
                            # Keep the remaining work durable during an outage.
                            for waiting_post in candidates[index + 1 :]:
                                self.state.put_pending_post(
                                    waiting_post,
                                    "Waiting for the classifier to recover",
                                )
                                result["pending"] += 1
                            break
                        continue

                    finding = (
                        classification.get("events", [{}])[0]
                        if classification.get("events")
                        else {}
                    )
                    if finding.get("type", "none") not in {
                        "reset_announced",
                        "reset_completed",
                        "reset_cancelled",
                        "model_release",
                        "price_reduction",
                        "usage_policy_change",
                        "compensation",
                    }:
                        continue
                    event = self.state.add_event(
                        post=post,
                        finding={
                            **finding,
                            "needs_human_review": classification.get(
                                "needs_human_review", False
                            ),
                        },
                        translation_zh=classification.get("translation_zh", ""),
                    )
                    if event:
                        result["new_events"] += 1
                        if recovered:
                            result["recovered"] += 1
                        logger.info(
                            "[tibo] detected event "
                            f"type={event['type']}, post={post_id}, recovered={recovered}"
                        )

                delivery_failures = 0
                pending_events = self.state.get_pending_events()
                recovered_events = [
                    event for event in pending_events if event.get("recovered")
                ]
                if len(recovered_events) > 1:
                    for group_id in self.notify_groups:
                        group_events = [
                            event
                            for event in recovered_events
                            if group_id
                            not in self.state.get_delivered_groups(
                                event.get("post_id", "")
                            )
                        ]
                        if not group_events:
                            continue
                        logger.info(
                            "[tibo] sending recovered-event digest "
                            f"to group {group_id}, events={len(group_events)}"
                        )
                        successes = await self.notifier.notify_recovery_digest(
                            group_events, [group_id]
                        )
                        if successes:
                            result["notified"] += 1
                            for recovered_event in group_events:
                                self.state.mark_event_notified(
                                    recovered_event["id"],
                                    successes,
                                    targets=self.notify_groups,
                                )
                        else:
                            delivery_failures += 1
                    pending_events = [
                        event for event in pending_events if not event.get("recovered")
                    ]

                for event in pending_events:
                    event_type = event.get("type", "")
                    if event_type == "reset_announced" and not self.notify_on_announced:
                        continue
                    if event_type == "reset_completed" and not self.notify_on_completed:
                        continue
                    delivered = set(
                        self.state.get_delivered_groups(event.get("post_id", ""))
                    )
                    targets = [g for g in self.notify_groups if g not in delivered]
                    if not targets:
                        self.state.mark_event_notified(
                            event["id"], [], targets=self.notify_groups
                        )
                        continue
                    logger.info(
                        f"[tibo] sending event {event['id']} to {len(targets)} group(s)"
                    )
                    success_groups = await self.notifier.notify_event(
                        event=event,
                        groups=targets,
                        announced_template=self.announced_template,
                        completed_template=self.completed_template,
                        important_template=self.important_template,
                        handle=self.x_handle,
                    )
                    self.state.mark_event_notified(
                        event["id"], success_groups, targets=self.notify_groups
                    )
                    result["notified"] += len(success_groups)
                    delivery_failures += len(targets) - len(success_groups)

                result["pending"] = len(self.state.get_pending_posts())
                partial_errors = list(source_errors)
                if classification_failures:
                    partial_errors.append(
                        f"{classification_failures} classification attempt(s) failed"
                    )
                if incomplete_posts:
                    partial_errors.append(
                        f"{incomplete_posts} post(s) have incomplete text"
                    )
                if delivery_failures:
                    partial_errors.append(
                        f"{delivery_failures} group delivery attempt(s) failed"
                    )
                result["error"] = "; ".join(partial_errors) or None
                scan = {
                    **result,
                    "pipeline_version": PIPELINE_VERSION,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                self.state.record_scan(scan, audit=audit, recovered=result["recovered"])
                self.state.record_check(
                    result["fetched"],
                    result["new"],
                    result["new_events"],
                    error=result["error"],
                )
                if not silent:
                    logger.info(f"[tibo] check completed: {scan}")
                return result
            except Exception as exc:
                result["error"] = str(exc)
                logger.error(f"[tibo] check failed: {exc}", exc_info=True)
                self.state.record_check(0, 0, 0, error=str(exc))
                return result

    @staticmethod
    def _apply_source_hints(
        post: dict[str, Any], classification: dict[str, Any]
    ) -> dict[str, Any]:
        """Correct confirmed reset categories without inventing feed-label events."""
        events = classification.get("events") or []
        if not events:
            return classification
        finding = events[0]
        if finding.get("type") in {None, "none", "uncertain"}:
            return classification
        source_kind = post.get("source_kind")
        source_event_type = post.get("source_event_type")

        forced_type = None
        if (source_kind == "banked" or source_event_type == "credits") and finding.get(
            "type"
        ) in {"reset_announced", "reset_completed"}:
            forced_type = "compensation"

        if forced_type is not None and forced_type != finding.get("type"):
            finding["type"] = forced_type
            finding["confidence"] = max(float(finding.get("confidence", 0)), 0.9)
            finding["reason"] = (
                f"{finding.get('reason', '')} Public Codex Reset feed routing applied."
            ).strip()
            if forced_type == "compensation" and not finding.get("summary"):
                finding["summary"] = "Tibo 宣布向受影响用户发放储备重置补偿。"

        if finding.get("type") in {
            "reset_announced",
            "reset_completed",
            "reset_cancelled",
        } and not re.search(
            r"\b(reset|resetting|reseted|usage|quota|limit|meter|allocation|button)\b|重置|额度",
            str(post.get("text") or ""),
            re.IGNORECASE,
        ):
            finding["type"] = "none"
            finding["confidence"] = 0.95
            finding["reason"] = "Reset wording is absent from the current post."
        return classification

    # ---------- 命令处理 ----------

    @staticmethod
    async def _send_text(event: AstrMessageEvent, text: str) -> None:
        """Send a command response through the current AstrBot event API."""
        await event.send(event.plain_result(text))

    @filter.command("tibo")
    async def handle_command(self, event: AstrMessageEvent):
        await self._handle_command(event)

    async def _handle_command(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        # AstrBot removes the configured wake prefix (normally "/") before
        # invoking command handlers, so accept both normalized and raw forms.
        if text.startswith("/"):
            text = text[1:].lstrip()
        parts = text.split(maxsplit=2)
        if not parts or parts[0].lower() != CMD_PREFIX.lstrip("/"):
            return

        sub_cmd = parts[1].lower() if len(parts) > 1 else "status"
        arg = parts[2] if len(parts) > 2 else ""

        # 只有管理员可以执行管理命令
        admin_only = {"enable", "disable", "check", "audit", "test"}
        if sub_cmd in admin_only and not self._is_admin(event):
            await self._send_text(event, "⚠️ 只有管理员可以执行此命令")
            return

        handler = getattr(self, f"_cmd_{sub_cmd}", None)
        if handler:
            try:
                await handler(event, arg)
            except Exception as e:
                logger.error(f"[tibo] 命令 /tibo {sub_cmd} 执行失败: {e}")
                await self._send_text(event, f"❌ 命令执行失败: {e}")
        else:
            await self._cmd_help(event)

    async def _cmd_help(self, event: AstrMessageEvent):
        help_text = (
            "📋 Tibo Codex 重要公告监控 - 命令列表\n\n"
            "/tibo status - 查看监控状态\n"
            "/tibo history [N] - 查看最近 N 个事件（默认 5）\n"
            "/tibo check - 手动触发一次检查（管理员）\n"
            "/tibo audit - 深度回查主帖、回复及遗漏（管理员）\n"
            "/tibo test - 用示例文本测试 AI 分类（管理员）\n"
            "/tibo enable - 启用定时监控（管理员）\n"
            "/tibo disable - 禁用定时监控（管理员）\n"
            "/tibo help - 显示此帮助"
        )
        await self._send_text(event, help_text)

    async def _cmd_status(self, event: AstrMessageEvent, arg: str):
        s = self.state.get_status_summary()
        lifecycle = s["lifecycle"]
        status_text = (
            "📊 Tibo 监控状态\n\n"
            f"监控目标: @{self.x_handle}\n"
            f"定时监控: {'✅ 启用' if self.enable_monitoring else '⏸️ 未启用'}\n"
            f"轮询间隔: {self.poll_interval // 60} 分钟\n"
            f"主数据源: {'Codex Reset API' if self.use_codex_reset_api else '本地 X'}\n"
            f"本地深度回查: 每 {self.audit_interval // 3600} 小时\n"
            f"提醒群: {', '.join(self.notify_groups)}\n\n"
            f"基线已建立: {'是' if s['baseline_established'] else '否（首次检查后建立）'}\n"
            f"已处理帖子: {s['seen_count']}\n"
            f"检测到事件: {s['event_count']}\n"
            f"待提醒事件: {s['pending_notify']}\n\n"
            f"待重试帖子: {s['pending_classify']}\n"
            f"累计遗漏补回: {s['recovered_total']}\n"
            f"上次深度回查: {s['last_audit_at'] or '从未'}\n\n"
            f"当前周期: 第 {lifecycle.get('cycle', 0)} 轮\n"
            f"生命周期状态: {lifecycle.get('status', 'idle')}\n"
            f"上次检查: {s['last_check_at'] or '从未'}\n"
            f"连续失败: {s['consecutive_failures']} 次"
        )
        if s.get("last_check_error"):
            status_text += f"\n\n⚠️ 上次检查错误: {s['last_check_error'][:200]}"
        await self._send_text(event, status_text)

    async def _cmd_history(self, event: AstrMessageEvent, arg: str):
        try:
            limit = int(arg) if arg else 5
        except ValueError:
            limit = 5
        limit = max(1, min(20, limit))
        events = self.state.get_recent_events(limit)
        if not events:
            await self._send_text(event, "📭 暂无检测到的重要事件")
            return
        lines = [f"📜 最近 {len(events)} 个重要事件\n"]
        for i, e in enumerate(events, 1):
            type_label = {
                "reset_announced": "🔔 预告",
                "reset_completed": "✅ 完成",
                "reset_cancelled": "❌ 取消",
                "model_release": "🚀 新模型",
                "price_reduction": "💰 降价",
                "usage_policy_change": "♾️ 额度调整",
                "compensation": "🎁 补偿",
                "uncertain": "❓ 不确定",
            }.get(e.get("type"), e.get("type"))
            notified = "已提醒" if e.get("notified") else "未提醒"
            lines.append(
                f"{i}. {type_label} | 置信度 {e.get('confidence', 0):.0%} | {notified}\n"
                f"   {e.get('summary', '')[:80]}\n"
                f"   {e.get('created_at', '')} | {e.get('post_url', '')}"
            )
        await self._send_text(event, "\n".join(lines))

    async def _cmd_check(self, event: AstrMessageEvent, arg: str):
        await self._send_text(event, "🔍 正在执行手动检查，请稍候...")
        result = await self._run_check(silent=False)
        source_names = (
            ", ".join(
                name
                for name, report in result.get("sources", {}).items()
                if report.get("ok")
            )
            or "无"
        )
        msg = (
            f"✅ 检查完成\n\n"
            f"成功数据源: {source_names}\n"
            f"抓取帖子: {result['fetched']}\n"
            f"待分析/重试: {result['new']}\n"
            f"新事件: {result['new_events']}\n"
            f"发送提醒: {result['notified']} 条"
        )
        if result.get("error"):
            msg += f"\n\n⚠️ 错误: {result['error'][:300]}"
        await self._send_text(event, msg)

    async def _cmd_audit(self, event: AstrMessageEvent, arg: str):
        await self._send_text(
            event,
            "🔎 正在执行遗漏回查：将核对外部 API、X 主帖、回复和长文全文...",
        )
        result = await self._run_check(silent=False, audit=True)
        source_lines = []
        for name, report in result.get("sources", {}).items():
            source_lines.append(f"{name}: {'✅' if report.get('ok') else '❌'}")
        msg = (
            "✅ 遗漏回查完成\n\n"
            f"数据源: {', '.join(source_lines) or '无'}\n"
            f"核对帖子: {result['fetched']}\n"
            f"待分析/重试: {result['new']}\n"
            f"补回事件: {result['recovered']}\n"
            f"新事件: {result['new_events']}\n"
            f"发送提醒: {result['notified']} 条\n"
            f"仍待重试: {result['pending']} 条"
        )
        if result.get("error"):
            msg += f"\n\n⚠️ 部分异常: {result['error'][:300]}"
        await self._send_text(event, msg)

    async def _cmd_test(self, event: AstrMessageEvent, arg: str):
        test_text = (
            arg
            or "We will reset usage limits for Codex and ChatGPT Work in the next hour."
        )
        await self._send_text(event, f"🧪 测试 AI 分类，文本: {test_text[:100]}")
        try:
            test_post = {
                "id": "test-only",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "text": test_text,
                "url": "https://x.com/test",
                "isQuote": False,
                "isReply": False,
                "isRetweet": False,
            }
            result = await self.classifier.classify(
                post=test_post,
                context_posts=[],
                lifecycle=self.state.get_lifecycle(),
                handle=self.x_handle,
            )
            finding = result.get("events", [{}])[0]
            msg = (
                f"🧪 分类结果\n\n"
                f"类型: {finding.get('type', 'none')}\n"
                f"置信度: {finding.get('confidence', 0):.0%}\n"
                f"摘要: {finding.get('summary', '')}\n"
                f"理由: {finding.get('reason', '')}\n"
                f"证据: {', '.join(finding.get('evidence', []))}\n"
                f"需人工复核: {'是' if result.get('needs_human_review') else '否'}\n\n"
                f"中文翻译: {result.get('translation_zh', '')[:200]}"
            )
            await self._send_text(event, msg)
        except Exception as e:
            await self._send_text(event, f"❌ 测试失败: {e}")

    async def _cmd_enable(self, event: AstrMessageEvent, arg: str):
        if self.enable_monitoring and self._poll_task and not self._poll_task.done():
            await self._send_text(event, "ℹ️ 监控已经在运行中")
            return
        self.enable_monitoring = True
        await self._start_polling()
        await self._send_text(
            event,
            f"✅ 定时监控已启用，间隔 {self.poll_interval // 60} 分钟，30 秒后开始第一次检查",
        )

    async def _cmd_disable(self, event: AstrMessageEvent, arg: str):
        self.enable_monitoring = False
        self._stop_polling()
        await self._send_text(
            event,
            "⏸️ 定时监控已禁用，手动检查 /tibo check 仍可使用",
        )

    # ---------- 工具方法 ----------

    @staticmethod
    def _parse_group_list(raw: str) -> list[str]:
        if not raw:
            return []
        return [g.strip() for g in str(raw).split(",") if g.strip()]

    @staticmethod
    def _detect_firefox_profile() -> str:
        """自动检测 Firefox 默认 profile 路径。"""
        try:
            appdata = os.environ.get("APPDATA", "")
            if not appdata:
                return ""
            profiles_ini = os.path.join(appdata, "Mozilla", "Firefox", "profiles.ini")
            if not os.path.exists(profiles_ini):
                return ""
            with open(profiles_ini, encoding="utf-8") as f:
                content = f.read()
            # 找 Default=1 的 profile 路径
            default_path = ""
            for section in content.split("["):
                if "Profile" in section and "Default=1" in section:
                    for line in section.split("\n"):
                        if line.strip().startswith("Path="):
                            default_path = line.split("=", 1)[1].strip()
                            break
            if default_path:
                if not os.path.isabs(default_path):
                    default_path = os.path.join(
                        appdata, "Mozilla", "Firefox", "Profiles", default_path
                    )
                return default_path
        except Exception as e:
            logger.warning(f"[tibo] 自动检测 Firefox profile 失败: {e}")
        return ""

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        """判断发送者是否为管理员。"""
        try:
            sender_id = str(event.get_sender_id() or "")
            # AstrBot 管理员配置在 cmd_config.json 的 admins_id
            # 这里简化：QQ 号 1603468474 为管理员（与 qq-bridge config 一致）
            admin_ids = {"1603468474"}
            return sender_id in admin_ids
        except Exception:
            return False

    @staticmethod
    def _default_announced_template() -> str:
        return (
            "⚠️ Codex 额度即将重置！\n\n"
            "Tibo (@{handle}) 已预告重置计划：\n{summary}\n\n"
            "原文证据：{evidence}\n"
            "中文翻译：{translation}\n\n"
            "🔗 {url}\n\n"
            "群友们趁重置前赶紧用额度！"
        )

    @staticmethod
    def _default_completed_template() -> str:
        return (
            "✅ Codex 额度已重置！\n\n"
            "Tibo (@{handle}) 宣布重置已完成：\n{summary}\n\n"
            "原文证据：{evidence}\n"
            "中文翻译：{translation}\n\n"
            "🔗 {url}\n\n"
            "额度已恢复，大家可以冲了！"
        )

    @staticmethod
    def _default_important_template() -> str:
        return (
            "{event_label}\n\n"
            "Tibo (@{handle}) 发布了与 Codex/ChatGPT 使用权益相关的重要消息：\n"
            "{summary}\n\n"
            "原文证据：{evidence}\n"
            "中文翻译：{translation}\n\n"
            "🔗 {url}"
        )
