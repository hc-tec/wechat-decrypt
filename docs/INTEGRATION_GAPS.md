# 集成缺口清单（给上层应用：自动回复 / AI 画像与记忆）

本项目定位：**本地离线的数据服务**（解密微信 DB → 提供查询/流式接口），**不提供 AI**。  
上层应用（自动回复、画像/记忆生成、个性化服务）会调用 AI，并把结果写回本服务的“画像/记忆库”。

下面是目前最容易“做着做着才发现缺”的能力清单，用于你规划上层应用与本服务的协议/接口。

---

## 已实现（本服务侧）

截至当前版本，本服务已落地了以下能力（其余仍建议在产品化阶段逐步补齐）：
- 历史消息断点：`GET /api/v1/chats/{username}/history?after_local_id=...`
- 消息方向字段：历史消息返回 `sender_username/is_send/direction`（用于避免对自己消息触发自动回复）
- 处理 ACK 存取：`GET/POST /api/v1/chats/{username}/message_status`
- AI 运行元数据存取：`GET/POST /api/v1/people/{username}/runs`，`PATCH /api/v1/runs/{run_id}`
- 可选写接口鉴权：`api_token`（写接口要求 `Authorization: Bearer ...`）

## 1. 事件与游标：避免漏消息/重复处理

### 1.1 稳定的“消息唯一键”
**现状**：实时流使用 `seq`（服务内递增）+ `timestamp` +（后续补全）`local_id`。  
**风险**：`seq` 不是跨重启稳定；同秒多条消息、组合消息（文字+图片）会出现“摘要覆盖/补全延迟”。

**建议补齐（接口/字段）**
- 在所有“新消息事件/拉取接口”里尽量返回稳定字段：
  - `local_id`（若可得）
  - `create_time`（timestamp）
  - `base_type` / `sub_type` / `local_type`
  - `username`（会话 id）
  - `sender_username`（群聊必需）
- 定义上层去重规则（推荐）：`(username, local_id)`；若拿不到 local_id，再退化为 `(username, timestamp, base_type, content_hash)`。

### 1.2 断点续跑（重启后继续）
**现状**：`/api/v1/messages?after_seq=` 依赖 `seq`，服务重启后会清零。  
**建议**：增加“按时间/按 local_id 的断点”：
- `GET /api/v1/messages?after_ts=...` 或 `after_local_id=...`（按会话维度更合理）
- 或提供 `GET /api/v1/chats/{username}/since?after_local_id=...`

### 1.3 处理确认（ACK）与重试策略
**现状**：服务不知道上层是否已处理/已回复。  
**风险**：上层崩溃重启后可能重复回复；或漏掉“需要回复”的消息。

**建议补齐（服务端仅存取，不做 AI）**
- 提供“外部处理状态”写接口（上层写入）：
  - `POST /api/v1/chats/{username}/message_status`
  - 字段示例：`message_key`、`status`（seen/processing/replied/ignored/error）、`app_id`、`updated_at`、`error`
- 上层自动回复以“幂等”为第一原则：同一 `message_key` 只允许一次“进入回复链路”。

---

## 2. 上层 AI 调用相关：你提到的“上次调用是什么时候”

AI 不在本服务做，但**本服务需要承载 AI 的结果与元数据**，否则无法解释“为什么现在的画像是这样”、也无法做增量更新。

### 2.1 AI 运行记录（Run / Job）
**缺口**：缺少“画像生成/更新”的运行记录与状态机。

**建议增加（上层写入，本服务保存）**
- `POST /api/v1/people/{username}/runs`
- `GET /api/v1/people/{username}/runs?limit=...`
- 字段建议：
  - `run_id`（uuid）
  - `app_id`（哪个上层应用/哪个版本）
  - `kind`（profile_refresh / memory_extract / reply_suggest / summarize…）
  - `started_at` / `finished_at`
  - `status`（success/failed/canceled）
  - `model`（如 gpt-4.1-mini 等）
  - `input_range`（本次使用了哪些消息：时间范围、local_id 范围、条数）
  - `prompt_hash`（不存 prompt 原文也行，至少存 hash 便于对齐版本）
  - `cost/tokens`（可选）
  - `error`（失败原因）

这样前端就能展示：**上一次 AI 刷新时间、用的模型、失败原因**。

### 2.2 AI 结果的可追溯性（Evidence）
**现状**：记忆条目支持 `evidence`，但上层若不填，后续很难纠错。

**建议约定**
- 上层每条记忆尽量带 1–3 条 evidence（指向消息 local_id/timestamp + 摘录），前端才能做“点击定位到原对话”。
- 对“性格特点/画像结论”这类不确定内容，建议：
  - `kind=trait`
  - `confidence` 低默认
  - `expires_at` 或“需要人工确认”标签（见下一条）

### 2.3 需要人工确认的条目（Approval）
**缺口**：没有“候选/待确认”状态，AI 会把不确定推断写成事实。

**建议**
- `memory_item.status` 除 `active/invalidated/archived` 外，增加 `pending`（待确认）
- 前端：把 pending 放到“待确认”面板，让用户一键确认/驳回

---

## 3. 联系人识别与变更：昵称会变、备注会变

### 3.1 “唯一联系人键”必须是 `username`
**建议**：上层所有存取都使用 `username`（wxid 或群 id）作为主键。  
昵称/备注仅用于搜索与展示缓存。

### 3.2 多匹配/歧义处理
**缺口**：上层输入一个名字可能匹配多个联系人。  
**建议**：联系人搜索接口返回：
- `items[]`（多条候选）
- 每条包含 `username/display_name/avatar_url/is_group`
- 上层负责让用户确认选择

---

## 4. 头像与资料：不止头像

### 4.1 头像缓存与失效
**现状**：`/avatar/{username}` 直接读 `head_image.db`。  
**建议**：返回 `ETag`/缓存头（已做），上层可缓存减少重复拉取。

### 4.2 可能还需要的资料字段（若 contact.db 能拿到）
后续如果 contact.db 表结构允许，建议补：
- 性别/地区/签名/朋友圈权限提示等（仅展示层需要）
- 群聊：群名、群成员列表（成员头像、昵称映射）

---

## 5. 自动回复专用缺口（重要）

### 5.1 方向判断：这是“对方发的”还是“我发的”
**风险**：如果无法判断方向，上层可能对自己消息误触发回复。

**建议补齐**
- 在历史/新消息里增加字段：`is_send` 或 `direction=in|out`
- 或最少提供 `self_username`（我是谁），以便上层用 sender 比对

### 5.2 群聊回复：@谁、回复引用、免打扰
上层会需要：
- `is_group`、`sender_username`
- 是否 @我（需要解析富媒体/系统字段）
- 免打扰策略（建议放在 profile 的 `auto_reply_policy`）

---

## 6. 多应用并发：同一台机器多个上层应用同时用

### 6.1 “谁写的”与冲突合并
**缺口**：当两个应用都在写 memories，会互相覆盖/打架。

**建议**
- 所有写入都带 `source`（如 `app:auto-reply@1.2.3`）
- 关键写入接口支持 `idempotency_key`（上层重复提交不会重复创建）
- 重要字段更新采用“追加式”而不是整块覆盖（记忆条目天然适合追加）

### 6.2 审计日志（可选但很值）
记录：
- 谁（app_id）在什么时候新增/修改/作废了哪条记忆
- 前端可提供“撤销/回滚”

---

## 7. 安全与隐私（本地也需要）

即使只监听 `127.0.0.1`，也存在“本机其他进程”读取隐私数据的风险。

**建议（可配置）**
- `api_token`：所有请求要求 `Authorization: Bearer ...`
- 或最小化：只对“写接口”加 token（避免别的进程污染画像/记忆）
- 提供“数据最小化”参数：上层只取需要的字段，避免把全量 raw 对话暴露给所有组件

---

## 8. 数据生命周期：积累久了会膨胀

### 8.1 记忆过期与清理
建议支持：
- `expires_at`（已支持字段）
- `GET /api/v1/people/{username}/memories?status=active` 默认过滤过期（或由上层过滤）

### 8.2 导入/导出与备份
面向普通用户强烈建议提供：
- `Export persona.db`（直接拷贝文件或导出 JSON）
- `Import`（恢复/迁移到新电脑）

---

## 9. 缺失但可能很快会要的能力（按优先级）

P0（自动回复安全性相关）
- `direction/is_send`、`self_username`
- 更稳定的断点游标（按会话 after_local_id）

P1（画像/记忆可靠性相关）
- AI Run 记录（started/finished/status/model/input_range）
- pending（待确认）状态
- 证据约定（evidence）

P2（产品化）
- token 鉴权（可选）
- 导入/导出
- 后台常驻（托盘/开机自启/安装器）
