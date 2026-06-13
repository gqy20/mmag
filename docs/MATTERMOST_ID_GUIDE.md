# Mattermost ID 体系 & 当前环境速查表

> 生成时间: 2026-06-10 | 环境: localhost:8065 (Docker)

---

## 一、ID 层级关系

```
Organization (组织/实例)
 └── Team (团队)          ← MM_TEAM_ID
      └── Channel (频道)   ← MM_CHANNEL_ID
           └── Post (消息)  ← 消息 ID
```

**关键区别：**
- **Team ID** = 团队容器（类似 Slack 的 Workspace）
- **Channel ID** = 具体聊天频道（类似 Slack 的 Channel）
- **两者不同！** 不能混用，之前 `.env` 里就犯了这个错

---

## 二、当前环境完整 ID 对照表

### Team（团队）

| 显示名 | Name | Team ID | 类型 |
|--------|------|---------|------|
| `test` | test | `mqtoperfh3y3z8boad7axaut4h` | Open |

> Bot 只加入了 1 个团队

### Channel（频道）

| 显示名 | Name | Channel ID | 所属 Team | 类型 | 消息数 |
|--------|------|------------|-----------|------|--------|
| **Town Square** | town-square | `tty5c46o6igjzfqkgm7irxo38o` | test | 公开(O) | 6 |
| **test2** ⭐ | test2 | `9d7qi63zsir7zjt41pecd3h5oy` | test | 公开(O) | 19 |
| Off-Topic | off-topic | `jpoyymkq1inmxpm9aixjxs4j6r` | test | 公开(O) | 0 |
| DM (你↔Bot) | — | `1puff6dc37fjje141p3inb3ftc` | — | 私聊(D) | 3 |

### 用户

| 用户名 | User ID | 说明 |
|--------|---------|------|
| gqy (你) | `fdfqdm8nwpri9q1e98kmksoh3r` | 管理员 |
| @test (Bot) | `ruzf8bk87t81zchff69o7y1omw` | AI Agent |

---

## 三、如何查找 ID

### 方法 1：curl 命令（推荐，最准确）

```bash
TOKEN="wqg8uwefeby3mjjp8p838uhigr"
BASE="http://localhost:8065/api/v4"

# 1. 列出所有 Team
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/users/me/teams"

# 2. 列出所有频道（含 DM）
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/users/me/channels"

# 3. 列出某个 Team 下的频道
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/teams/{TEAM_ID}/channels"

# 4. 获取自己的信息
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/users/me"
```

### 方法 2：浏览器开发者工具

1. 打开 Mattermost 网页版 (`http://localhost:8065`)
2. 按 **F12** → **Network（网络）** 标签
3. 在任意频道发一条消息
4. 在请求列表中找到 `posts` 开头的请求
5. 点击查看 **Response（响应）**：
   - `channel_id` → 频道 ID
   - `team_id` → 团队 ID（公开频道才有）

### 方法 3：浏览器地址栏

Mattermost URL 格式：
```
http://localhost:8065/{team_name}/channels/{channel_name}
```

例如：`http://localhost:8065/test/channels/test2`

⚠️ 地址栏只显示 **名称**，不显示 **ID**。需要用方法 1 或 2 获取 ID。

---

## 四、Agent 配置说明

### .env 关键配置项

```env
# ============================================
# Mattermost 连接
# ============================================

MM_URL=http://localhost:8065          # Mattermost 地址
MM_TOKEN=wqg8uwefeby3mjjp8p838uhigr # Bot Token (必填)

# 监听范围 (二选一或都填)
MM_TEAM_ID=mqtoperfh3y3z8boad7axaut4h     # Team ID (团队级别过滤)
MM_CHANNEL_ID=9d7qi63zsir7jzt41pecd3h5oy    # Channel ID (频道级别过滤, 更精确)

# 填写规则:
#   - 两个都填 → 只监听该 Team 下该频道的消息
#   - 只填 TEAM_ID → 监听该 Team 下所有频道
#   - 只填 CHANNEL_ID → 只监听该频道 (不检查 Team)
#   - 都不填 → 监听所有消息 (不推荐, 噪音大)

# ============================================
# LLM 配置
# ============================================

ANTHROPIC_API_KEY=你的密钥              # StepFun / Anthropic API Key
ANTHROPIC_BASE_URL=https://api.stepfun.com/step_plan  # API 地址
ANTHROPIC_MODEL=step-3.7-flash         # 模型名称

# ============================================
# Agent 行为配置
# ============================================

BOT_NAME=                                 # 已删除 — bot 身份完全由 MM_TOKEN 决定
LISTEN_PROBABILITY=0.15                  # 旁听概率 (0~1, 建议 0.1~0.2)
MAX_CONTEXT_MESSAGES=30                  # 上下文窗口大小
TYPING_DELAY_MIN=1                       # 模拟打字最短延迟(秒)
TYPING_DELAY_MAX=3                       # 模拟打字最长延迟(秒)
MEMORY_DB_PATH=./agent_memory.db        # SQLite 记忆存储路径
LOG_LEVEL=INFO                          # 日志级别 DEBUG/INFO/WARNING/ERROR
```

### 当前推荐配置

如果你想只在 **test2 频道** 监听：

```env
MM_TEAM_ID=mqtoperfh3y3z8boad7axaut4h
MM_CHANNEL_ID=9d7qi63zsir7jzt41pecd3h5oy
```

如果你想在 **整个 test 团队** 都监听：

```env
MM_TEAM_ID=mqtoperfh3y3z8boad7axaut4h
MM_CHANNEL_ID=                              # 留空
```

---

## 五、Channel 类型对照

| Type | 含义 | 说明 |
|------|------|------|
| **O** | Open (公开) | 团队成员可见，默认加入 |
| **P** | Private (私有) | 需被邀请才能加入 |
| **D** | Direct (私聊) | 两人对话，无 Team 归属 |

---

## 六、常用 API 快速参考

| 操作 | 方法 | 路径 |
|------|------|------|
| 获取自己信息 | GET | `/users/me` |
| 获取我的 Team | GET | `/users/me/teams` |
| 获取我的频道 | GET | `/users/me/channels` |
| Team 下频道列表 | GET | `/teams/{team_id}/channels` |
| 频道详情 | GET | `/channels/{channel_id}` |
| 频道消息 | GET | `/channels/{channel_id}/posts?per_page=N` |
| 单条消息 | GET | `/posts/{post_id}` |
| 发送消息 | POST | `/posts` |
| 发送 ephemeral | POST | `/posts/ephemeral` |
| 用户信息 | GET | `/users/{user_id}` |
| WebSocket 连接 | WS | `/api/v4/websocket` |

详细 API 文档见本项目 `/api/v4/source/*.yaml`

---

## 七、常见问题排查

### Q1: Bot 发了消息但我看不到？

按以下步骤排查：

```bash
TOKEN="wqg8uwefeby3mjjp8p838uhigr"
CHANNEL="9d7qi63zsir7zjt41pecd3h5oy"  # test2 频道 ID

# Step 1: 确认消息确实在数据库里
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8065/api/v4/channels/$CHANNEL/posts?per_page=5" | python3 -c "
import json, sys
data = json.load(sys.stdin)
posts = list(data.get('posts', {}).values())
posts.sort(key=lambda p: p['create_at'], reverse=True)
for p in posts[:5]:
    print(f'{p[\"id\"][:12]}... | {p.get(\"username\",\"?\"):8s} | {p[\"message\"][:60]}')
"

# Step 2: 确认 Bot 是频道成员
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8065/api/v4/channels/$CHANNEL/members/ruzf8bk87t81zchff69o7y1omw" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print('Bot 成员状态:', 'OK' if 'id' in data else 'NOT MEMBER')
"
```

**最常见原因：**
| 原因 | 解决方法 |
|------|----------|
| 浏览器页面未刷新 | 按 `F5` 或 `Ctrl+R` 刷新 |
| WebSocket 断连 | 关闭浏览器标签页重新打开 |
| 看错频道 | 确认你在 `test → test2`，不是 Town Square |
| 消息被折叠（thread） | 点击 `查看线程` 展开回复 |
| 缓存问题 | 清除浏览器缓存后刷新 |

### Q2: 回复内容出现 `{bot_username}` 字面量？

这是 **YAML 模板变量未渲染** 的症状。

**原因：** `prompts.yml` 中使用了 `{{var}}`（Jinja2 格式），但代码用 `str.format()` 渲染。双花括号被 Python 当作字面量。

**修复：** 确保 `prompts.yml` 中所有变量用单花括号 `{var}`：
```yaml
# ❌ 错误 (输出字面量 {bot_username})
system_prompt: "你是 {{bot_username}}"

# ✅ 正确 (渲染为 agent2)
system_prompt: "你是 {bot_name}"
```

### Q3: Team ID 和 Channel ID 搞混了？

这是最容易犯的错误！记住：

```
MM_TEAM_ID    → 团队 (如 test)     → mqtoperfh3y3z8boad7axaut4h
MM_CHANNEL_ID → 频道 (如 test2)    → 9d7qi63zsir7zjt41pecd3h5oy
```

**判断方法：** 用 `/users/me/teams` 获取的是 Team ID，用 `/users/me/channels` 获取的是 Channel ID。

### Q4: 如何确认 Agent 正常运行？

检查日志中的关键标记：
```
[1/5] ✅ 配置加载完成        ← 配置 OK
[2/5] ✅ 提示词模板已加载      ← prompts.yml OK
[3/5] ✅ 记忆系统就绪          ← SQLite OK
       ✅ Bot: @test (ruzf...) ← 身份认证 OK
[4/5] ✅ WebSocket 已连接      ← 连接成功
       📨 Hello | id=xxx       ← 握手成功
       🔑 认证请求已发送        ← 认证中...
[5/5] 🎯 进入事件监听循环...   ← 就绪!
────── Agent 就绪，等待消息 ──────  ← 可以收发消息了
```

---

## 八、环境架构图

```
┌─────────────────────────────────────────────────┐
│              Mattermost Server                  │
│              localhost:8065                     │
│                                                  │
│  ┌──────── Team: test ───────────────────────┐  │
│  │  Team ID: mqtoperfh3y3z8boad7axaut4h      │  │
│  │                                           │  │
│  │  ┌─ Town Square ─┐  ┌─ test2 ⭐ ───────┐ │  │
│  │  │ tty5c46o6...   │  │ 9d7qi63zsir...   │ │  │
│  │  │ (6 条消息)     │  │ (19+ 条消息)     │ │  │
│  │  │                │  │ ← Bot 监听目标    │ │  │
│  │  └────────────────┘  └──────────────────┘ │  │
│  │  ┌─ Off-Topic ───┐                        │  │
│  │  │ jpoyymkq1...   │                        │  │
│  │  │ (0 条消息)     │                        │  │
│  │  └────────────────┘                        │  │
│  └───────────────────────────────────────────┘  │
│                                                  │
│  ┌─ DM: gqy ↔ @test ─────────────────────────┐  │
│  │  1puff6dc37fjje141p3inb3ftc               │  │
│  └───────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
         ↕ WebSocket (官方协议)
         ↕ REST API (/api/v4/posts)
┌─────────────────────────────────────────────────┐
│              AI Agent (agent.py)                │
│                                                  │
│  Config ← .env  |  Prompts ← prompts.yml        │
│  Memory ← SQLite  |  LLM ← StepFun API          │
│                                                  │
│  用户: gqy (fdfqdm8nwpri9q1e98kmksoh3r)         │
│  Bot:  @test (ruzf8bk87t81zchff69o7y1omw)       │
└─────────────────────────────────────────────────┘
```
