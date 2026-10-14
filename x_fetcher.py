"""X 数据获取：通过子进程调用独立 Node.js 脚本抓取目标用户时间线。

脚本路径：插件目录/scripts/fetch_tweets.cjs
脚本输出：stdout 为 JSON 数组，每个元素包含 id/text/timestamp/url/isReply/isQuote/isRetweet
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from typing import Any

from astrbot.api import logger


class XFetchError(Exception):
    """X 抓取失败。"""


class XFetcher:
    """X 时间线抓取器。"""

    def __init__(
        self,
        plugin_dir: str,
        node_executable: str = "",
        firefox_profile_path: str = "",
        firefox_executable_path: str = "",
    ):
        self.plugin_dir = plugin_dir
        self.script_path = os.path.join(plugin_dir, "scripts", "fetch_tweets.cjs")
        self.node_executable = node_executable.strip() or "node"
        self.firefox_profile_path = firefox_profile_path.strip()
        self.firefox_executable_path = firefox_executable_path.strip()
        self.last_report: dict[str, Any] = {}

    async def fetch_recent(
        self,
        handle: str,
        limit: int = 30,
        since_id: str | None = None,
        timeout: int = 300,
        lookback_days: int = 7,
    ) -> list[dict[str, Any]]:
        """抓取目标用户的最近帖子（含回复，排除纯转推）。

        Args:
            handle: X 用户名（不含 @）
            limit: 抓取数量
            since_id: 只返回此 ID 之后的新帖子（可选）
            timeout: 超时秒数

        Returns:
            帖子列表，按时间倒序
        """
        if not os.path.exists(self.script_path):
            raise XFetchError(f"抓取脚本不存在: {self.script_path}")

        cmd = [
            self.node_executable,
            self.script_path,
            "--handle",
            handle,
            "--limit",
            str(limit),
        ]
        if self.firefox_profile_path:
            cmd.extend(["--profile", self.firefox_profile_path])
        if self.firefox_executable_path:
            cmd.extend(["--firefox", self.firefox_executable_path])
        cmd.extend(
            [
                "--state-file",
                os.path.join(self.plugin_dir, "data", "monitor_state.json"),
                "--days",
                str(lookback_days),
            ]
        )
        self.last_report = {}

        logger.info(
            f"[tibo] 开始抓取 X 动态: handle=@{handle}, limit={limit}, since_id={since_id or 'None'}"
        )

        proc = None
        # The collector uses an isolated profile; never terminate the source browser.
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.join(self.plugin_dir, "scripts"),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                err_msg = stderr or stdout or f"退出码 {proc.returncode}"
                # 提取最后几行错误信息
                err_lines = err_msg.splitlines()[-5:]
                raise XFetchError(
                    f"抓取脚本执行失败 (code={proc.returncode}): {' | '.join(err_lines)}"
                )

            if not stdout:
                raise XFetchError("Fetcher returned empty output")

            # 脚本可能输出日志行 + 最后一行 JSON，尝试找到 JSON 数组
            self.last_report = self._parse_output(stdout)
            posts = self.last_report["posts"]
            logger.info(f"[tibo] 抓取完成，获取 {len(posts)} 条帖子")
            return posts

        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise XFetchError(
                f"抓取超时（{timeout}s），X 页面可能加载缓慢或登录态失效"
            ) from exc
        except XFetchError:
            raise
        except Exception as e:
            raise XFetchError(f"抓取异常: {e}")

    @staticmethod
    def _parse_output(stdout: str) -> dict[str, Any]:
        """从脚本输出中解析 JSON 帖子数组。

        脚本设计为最后一行输出 JSON，但兼容中间有日志的情况。
        """
        lines = stdout.strip().splitlines()
        # 从后往前找第一个可解析为 JSON 数组的行
        for line in reversed(lines):
            line = line.strip()
            if not line.startswith(("[", "{")):
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict) and isinstance(data.get("posts"), list):
                    if not isinstance(data.get("sources"), dict):
                        raise XFetchError("Fetcher report lacks source coverage")
                    if any(
                        not isinstance(p, dict) or not str(p.get("id", "")).isdigit()
                        for p in data["posts"]
                    ):
                        raise XFetchError("Fetcher report contains invalid posts")
                    return data
            except json.JSONDecodeError:
                continue
        raise XFetchError("Fetcher returned invalid JSON instead of a coverage report")
