# Tibo Codex 重要公告监控插件 (AstrBot)

监控 X 用户 @thsottiaux (Tibo) 的公开动态，用 AI 判断 GPT/Codex 的重置、新模型、降价、额度调整与补偿公告，并在配置的 QQ 群发送普通消息，不使用 @全体成员。

功能思路参考 [rxy331/Tibo-monitor](https://github.com/rxy331/Tibo-monitor)。公开版本重新编写了分类提示词与浏览器抓取工具，不分发上游 Electron 应用、提示词或 DOM 抓取实现。致谢与依赖说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 功能

- **低干扰轮询**：每隔 N 分钟读取 Codex Reset 公共 API，不需要启动 Firefox
- **深度补漏**：默认每 6 小时才使用 Firefox 核对一次主页和回复；API 不可用或过期时也会自动回退到本地抓取
- **AI 分类**：使用 AstrBot 自带 LLM Provider 判断帖子是否为重要公告
  - `reset_announced`：预告即将重置
  - `reset_completed`：已完成重置
  - `reset_cancelled`：取消重置
  - `model_release`：新模型发布或开始推送
  - `price_reduction`：模型、套餐或 Token 降价
  - `usage_policy_change`：不限对话、提额或限额规则调整
  - `compensation`：重置卡、额度或其他用户补偿
  - `uncertain` / `none`：不确定或无关
- **QQ 群提醒**：检测到信号时发送原文证据、中文翻译和链接，不需要 @全体成员权限
- **可靠补发**：保存逐群送达回执，分类或发送失败会在后续轮询重试；深度核查能补回遗漏消息
- **生命周期管理**：跟踪重置周期（idle → planned → completed/cancelled）

## 前置要求

1. **AstrBot 已配置至少一个 LLM Provider**（CHAT_COMPLETION 类型）
2. **可选：Firefox 已安装并登录 X (x.com)**
   - 仅用于定期深度补漏与公共 API 故障时的回退
3. **可选：Node.js 已安装**（启用 Firefox 深度补漏时需要）

## 安装

1. 在 AstrBot 插件管理中通过 `https://github.com/HITZY2002/astrbot_plugin_tibo_monitor` 安装，或将插件目录放入 `AstrBot/data/plugins/astrbot_plugin_tibo_monitor/`
2. 如果需要 Firefox 深度回查，安装 Node 依赖；仅使用公共 API 时可跳过：
   ```bash
   cd AstrBot/data/plugins/astrbot_plugin_tibo_monitor/scripts
   npm install
   ```
3. 重启 AstrBot 或在插件管理中重新加载插件
4. 在插件配置中填写：
   - `firefox_profile_path`：已登录 X 的 Firefox profile 路径（留空自动检测）
   - `notify_groups`：提醒的 QQ 群号（逗号分隔）
   - `llm_provider_id`：用于分类的 LLM Provider ID（留空自动选第一个）
5. 发送 `/tibo test` 测试 AI 分类是否正常
6. 发送 `/tibo check` 手动触发一次检查，确认 X 抓取正常
7. 发送 `/tibo audit` 手动执行 API + Firefox 深度补漏
8. 发送 `/tibo enable` 启用定时监控

## 命令

| 命令 | 说明 | 权限 |
|---|---|---|
| `/tibo status` | 查看监控状态 | 所有人 |
| `/tibo history [N]` | 查看最近 N 个事件 | 所有人 |
| `/tibo check` | 手动触发一次检查 | 管理员 |
| `/tibo audit` | 手动执行深度补漏检查 | 管理员 |
| `/tibo test [文本]` | 用示例文本测试 AI 分类 | 管理员 |
| `/tibo enable` | 启用定时监控 | 管理员 |
| `/tibo disable` | 禁用定时监控 | 管理员 |
| `/tibo help` | 显示帮助 | 所有人 |

## 配置项

| 配置 | 说明 | 默认值 |
|---|---|---|
| `enable_monitoring` | 是否启用定时轮询 | false |
| `x_handle` | 监控的 X 用户名 | thsottiaux |
| `use_codex_reset_api` | 普通轮询优先使用 Codex Reset 公共 API | true |
| `codex_reset_feed_url` | Codex Reset JSON Feed 地址 | https://codex-reset.com/api/feed |
| `firefox_profile_path` | Firefox profile 路径 | 自动检测 |
| `firefox_executable_path` | firefox.exe 路径 | 自动检测 |
| `poll_interval_minutes` | 轮询间隔（分钟） | 15 |
| `fetch_limit` | 每次抓取帖子数 | 30 |
| `audit_interval_hours` | Firefox 深度补漏间隔（小时） | 6 |
| `audit_lookback_days` | 深度补漏回看天数 | 7 |
| `audit_fetch_limit` | 深度补漏每条线路最多抓取数 | 80 |
| `notify_groups` | 提醒的 QQ 群号 | 留空，必须自行配置 |
| `notify_on_announced` | 预告时提醒 | true |
| `notify_on_completed` | 完成时提醒 | true |
| `llm_provider_id` | LLM Provider ID | 自动 |
| `announced_template` | 预告提醒模板 | 内置 |
| `completed_template` | 完成提醒模板 | 内置 |
| `important_template` | 新模型、降价、额度调整和补偿提醒模板 | 内置 |

## 工作原理

1. **抓取**：普通轮询读取 Codex Reset 公共 API；定期深度核查再通过 Firefox 同时检查主页和回复，并展开被截断的长帖
2. **过滤**：排除纯转推，只保留目标用户自己发的帖子（含回复）
3. **分类**：将帖子文本 + 最近上下文 + 当前生命周期状态发给 LLM，判断重置与重要产品公告
4. **记录**：将分类结果和事件持久化到 `data/monitor_state.json`
5. **提醒**：对未提醒的重置、新模型、降价、额度调整和补偿事件，向配置的 QQ 群发送普通消息

## 注意事项

- 首次启用时会建立基线；升级后的深度核查只补回基线之后漏掉的消息
- Firefox 登录失效不会阻断普通 API 轮询，但会在状态中报告深度补漏失败
- 轮询间隔建议不低于 10 分钟
- 插件数据存储在插件目录的 `data/` 文件夹下
- Firefox 深度回查会创建临时 Cookie 副本，正常退出后删除；不复制保存的密码，不写入原 Firefox profile。异常断电可能留下临时资料，请勿上传或分享。
- 上游公共 Feed、X 页面结构及登录限制都可能变化；深度回查失败会保留错误状态与待处理任务，不等同于没有新帖子。
- 不要提交 `data/`、Firefox profile、Cookie、日志或 AstrBot 私人配置。公开仓库不包含这些文件。

## 开发与测试

```bash
python -m pip install pytest pytest-asyncio
python -m pytest tests/ -q
node --test scripts/fetch_tweets.test.cjs
```

Python 单元测试使用最小 AstrBot 桩，不会连接群聊、浏览器或模型；抓取工具测试使用模拟 JSON，不需要 X 登录。真实深度回查仍需在安装 Firefox 且已登录 X 的环境中验证。

## 许可证

MIT License，见 [LICENSE](LICENSE)。这是第三方 AstrBot 插件，不是 OpenAI、X、Codex Reset 或 AstrBot 的官方产品。使用时请遵守相关平台条款。
