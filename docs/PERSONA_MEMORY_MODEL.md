# 人物画像与记忆：数据模型建议（给上层应用）

本项目定位：**本地离线的数据服务**（解密微信 DB → 提供查询/流式接口），**不提供 AI**。  
上层应用（自动回复、画像/记忆生成、个性化服务）可以调用 AI，然后把结果写回本服务的 `persona_db`（默认 `persona.db`），用于展示与后续增量更新。

> 核心原则：**一切以 `username` 作为联系人主键**（wxid 或群 id）。昵称/备注会变，只用于搜索与展示。

---

## 1) 页面需要的数据从哪里来

面向你描述的前端页面（头像 + 基础信息 + 上次聊天 + 记忆/喜好/禁忌/性格等），建议按下列组合取数：

- **联系人基础信息（含头像）**：`GET /api/v1/people/{username}/profile`  
  返回 `contact`（含 `display_name`、`avatar_url`）+ `profile`（画像字段）。
- **上一次聊天摘要**：`GET /api/v1/sessions?limit=...`  
  `session.db` 提供“最新消息摘要/时间/未读”等，适合做列表与“最近聊了什么”。
- **历史对话明细**：`GET /api/v1/chats/{username}/history?...`  
  适合做“最近 N 条对话/近 7 天对话”，并支持 `after_local_id` 断点。
- **记忆条目（结构化）**：`GET /api/v1/people/{username}/memories?...`
- **AI 刷新/生成记录（时间线）**：`GET /api/v1/people/{username}/runs?...`

---

## 2) 画像（Profile）建议放什么

画像是“稳定 + 少量”的信息，适合放在 `profile`：

- `tags: string[]`：标签（例如：`["家人","客户","健身"]`）
- `notes: string`：自由文本备注（人类可读、可手改）
- `auto_reply_policy: object`：**自动回复策略**（由上层应用定义结构）

建议：把“容易变化/需要证据/可过期”的信息放在 memories，而不是 notes。

---

## 3) 记忆（Memory Item）建议怎么建模

后端存储字段（`memory_item`）支持：

- `id`：uuid
- `kind`：记忆类别（建议约定枚举，见下文）
- `key`：可选的二级键（便于前端分组）
- `value`：内容（字符串）
- `importance`：0–5（越高越重要）
- `confidence`：0–1（AI 推断建议 < 0.6）
- `status`：建议用 `active | pending | archived | invalidated`（后端允许任意字符串）
- `source`：建议写 `manual` 或 `app:<app_id>@<version>` 或 `ai:<app_id>@<version>`
- `evidence`：数组（建议 1–3 条），用于“可追溯到原消息”
- `expires_at`：Unix 秒（0=不过期）
- `created_at/updated_at`

### evidence（证据）建议结构

后端只要求是数组，上层可约定每条证据长这样：

```json
{
  "local_id": 123,
  "timestamp": 1710000000,
  "snippet": "他提到：周三晚上不方便"
}
```

前端可支持“点击证据 → 跳转到该联系人聊天记录并定位”。

---

## 4) 建议的 kind/key 约定（对应你的 UI 区块）

你提到的“喜好/禁忌/性格特点”等，可以直接映射到 `kind`：

- 喜好：`kind=like`，`key` 可选（例如 `food|hobby|topic|gift`）
- 禁忌：`kind=taboo`（或 `dislike`），`key` 可选（例如 `topic|behavior|time`）
- 性格特点/画像结论：`kind=trait`（建议配合 `confidence` + `pending`）
- 重要事实：`kind=fact`（例如生日、过敏、孩子信息等）
- 关系/身份：`kind=relationship`（例如“同事/客户/同学/家人”）
- 近期目标/计划：`kind=plan`（建议 `expires_at`）
- 沟通偏好：`kind=communication`（例如“喜欢直接/讨厌电话/只晚上回消息”）

示例：

```json
{"kind":"like","key":"food","value":"喜欢川菜","importance":3,"confidence":0.9,"status":"active","source":"manual"}
{"kind":"taboo","key":"topic","value":"不要聊收入","importance":4,"confidence":0.8,"status":"active","source":"ai:auto-reply@1.0"}
{"kind":"trait","key":"","value":"对时间安排很敏感","importance":2,"confidence":0.55,"status":"pending","source":"ai:persona-ui@0.1"}
```

---

## 5) AI 运行记录（Runs）：解决“上次调用 AI 是什么时候”

后端不调用 AI，但提供 Run 存取接口，建议上层按以下流程写入：

1. `POST /api/v1/people/{username}/runs` 创建一条：`status=running`
2. AI 产出后：
   - `PATCH /api/v1/people/{username}/profile` 更新画像（可选）
   - `POST /api/v1/people/{username}/memories` 追加记忆（可多条）
3. `PATCH /api/v1/runs/{run_id}` 写入 `status=success|failed`、`finished_at`、`tokens/cost/error`

前端即可展示：
- “上次刷新时间/用的模型/是否失败/失败原因”
- “本次用到了哪些消息范围”（`input_range`）

---

## 6) 备份/迁移（面向普通用户很重要）

建议上层产品提供“一键备份/恢复”：

- 直接备份 `persona.db`（以及 `config.json`、`all_keys.json`）
- 或导出为 JSON（便于跨机器迁移与云备份）

