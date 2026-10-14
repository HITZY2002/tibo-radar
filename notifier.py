"""QQ 群提醒：向配置的群发送 Tibo 重要公告。"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

IMPORTANT_EVENT_LABELS = {
    "model_release": "🚀 新模型发布",
    "price_reduction": "💰 模型/服务降价",
    "usage_policy_change": "♾️ 使用额度调整",
    "compensation": "🎁 用户补偿",
}


class GroupNotifier:
    """QQ 群消息推送器。"""

    def __init__(self, context, platform_id: str = "qq-onebot"):
        self.context = context
        self.platform_id = platform_id

    def _build_umo(self, group_id: str) -> str:
        """构建 AstrBot 统一消息来源标识 (UMO)。"""
        return f"{self.platform_id}:GroupMessage:{group_id}"

    async def send_text(self, group_id: str, text: str) -> bool:
        """Send a plain notification without requiring mention-all permission."""
        umo = self._build_umo(group_id)
        chain = MessageChain(chain=[Plain(text)])
        for attempt in range(3):
            try:
                ok = await asyncio.wait_for(
                    self.context.send_message(umo, chain), timeout=20
                )
                if ok:
                    logger.info(f"[tibo] 提醒已发送到群 {group_id}")
                    return True
                logger.warning(
                    f"[tibo] 提醒发送到群 {group_id} 返回失败（{attempt + 1}/3）"
                )
            except TimeoutError:
                logger.error(
                    f"[tibo] sending alert to group {group_id} timed out "
                    f"({attempt + 1}/3)"
                )
            except Exception as e:
                logger.error(
                    f"[tibo] 发送提醒到群 {group_id} 失败（{attempt + 1}/3）: {e!r}"
                )
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
        return False

    async def notify_event(
        self,
        event: dict[str, Any],
        groups: list[str],
        announced_template: str,
        completed_template: str,
        important_template: str,
        handle: str = "thsottiaux",
    ) -> list[str]:
        """根据事件类型发送提醒，返回成功发送的群号列表。"""
        event_type = event.get("type", "")
        if event_type == "reset_announced":
            template = announced_template
        elif event_type == "reset_completed":
            template = completed_template
        elif event_type in IMPORTANT_EVENT_LABELS:
            template = important_template
        else:
            logger.info(f"[tibo] 事件类型 {event_type} 不触发提醒")
            return []

        evidence = event.get("evidence", [])
        evidence_text = evidence[0] if evidence else "(无)"
        translation = event.get("translation_zh", "") or "(无翻译)"

        message = template.format(
            handle=handle,
            summary=event.get("summary", ""),
            evidence=evidence_text,
            url=event.get("post_url", ""),
            translation=translation,
            event_label=IMPORTANT_EVENT_LABELS.get(event_type, "📢 重要公告"),
        )
        if event.get("recovered"):
            original_time = event.get("post_timestamp") or "时间未知"
            message = f"🔎 遗漏补报（原帖 {original_time}）\n\n{message}"

        success_groups = []
        for group_id in groups:
            group_id = str(group_id).strip()
            if not group_id:
                continue
            if await self.send_text(group_id, message):
                success_groups.append(group_id)
        return success_groups

    async def notify_recovery_digest(
        self, events: list[dict[str, Any]], groups: list[str]
    ) -> list[str]:
        """Send one compact digest for multiple recovered historical events."""
        lines = ["🔎 Tibo 遗漏补报汇总", ""]
        for event in sorted(events, key=lambda item: item.get("post_timestamp", "")):
            label = IMPORTANT_EVENT_LABELS.get(event.get("type", ""), "📢 重要公告")
            if event.get("type") == "reset_announced":
                label = "⚠️ 重置预告"
            elif event.get("type") == "reset_completed":
                label = "✅ 重置完成"
            lines.extend(
                [
                    f"{label}｜{event.get('post_timestamp') or '时间未知'}",
                    event.get("summary", ""),
                    event.get("post_url", ""),
                    "",
                ]
            )
        message = "\n".join(lines).rstrip()
        successful = []
        for group_id in groups:
            if await self.send_text(group_id, message):
                successful.append(group_id)
        return successful
