# WeChat Data Service API（本地离线 / Windows）

默认监听地址：`http://127.0.0.1:5678`

说明：
- 本服务只读微信本地数据库并解密/解析，**不提供 AI**。
- 适配上层应用：自动回复、个性化对话取数、人物画像/记忆（后续可扩展写接口）。

鉴权（可选）：
- `config.json` 设置 `api_token` 后，**所有写接口**需要带 `Authorization: Bearer <token>`（或 `X-Api-Token: <token>`）。

## 基础接口

### `GET /api/v1/health`

返回服务健康状态与当前消息序号。

响应示例：
```json
{"ok":true,"time":1710000000,"last_seq":123}
```

### `GET /api/v1/state`

返回轻量状态（适合上层应用启动时探活/同步游标）。

响应示例：
```json
{"time":1710000000,"last_seq":123,"contacts_loaded":true,"self_username":"wxid_xxx","write_auth_enabled":false}
```

## 联系人与会话

### `GET /api/v1/contacts?query=&limit=50`

联系人检索（昵称/备注/微信号模糊匹配）。

响应示例：
```json
{"items":[{"username":"wxid_xxx","nick_name":"张三","remark":"同事","display_name":"同事","is_group":false,"avatar_url":"/avatar/wxid_xxx"}]}
```

### `GET /api/v1/recent_contacts?limit=20&offset=0`

最近查询过的联系人（服务端只负责保存；前端按需拉取数量/分页）。

记录方式：
- 调用 `GET /api/v1/chats/{username}/history` 会自动记录
- 或手动调用 `POST /api/v1/recent_contacts`

响应示例：
```json
{"items":[{"username":"wxid_xxx","display_name":"同事","last_access_ts":1710000000,"access_count":3,"avatar_url":"/avatar/wxid_xxx"}],"limit":20,"offset":0}
```

### `POST /api/v1/recent_contacts`

手动记录一次“最近查询联系人”。

请求示例：
```json
{"username":"wxid_xxx"}
```

### `GET /api/v1/sessions?limit=50`

最近会话列表（来自 `session.db`，含未读数和最新消息摘要）。

响应示例：
```json
{"items":[{"username":"wxid_xxx","display_name":"同事","avatar_url":"/avatar/wxid_xxx","unread":0,"last_timestamp":1710000000,"last_msg_type":1,"summary":"晚上吃啥"}]}
```

### `GET /avatar/{username}`

联系人头像（从 `head_image/head_image.db` 读取）。

## 实时消息（给自动回复程序用）

### `GET /api/v1/messages?after_seq=0&limit=200`

拉取自 `after_seq` 之后的新消息（按 `seq` 递增）。`seq` 为服务内递增编号，适合做游标避免漏消息。

响应示例：
```json
{"last_seq":123,"items":[{"seq":123,"timestamp":1710000000,"username":"wxid_xxx","chat":"同事","type":"文本","content":"晚上吃啥"}]}
```

### `GET /stream`

SSE 实时推送（浏览器 EventSource 兼容）。普通消息走默认 `message` 事件；解析补全会通过自定义事件推送（如 `rich_update` / `image_update` / `message_detail`）。

## 历史消息（给个性化/画像取数用）

### `GET /api/v1/chats/{username}/history?limit=50&offset=0&start_ts=&end_ts=`

获取指定联系人/群的历史消息（从 `message_*.db` 查询）。

额外参数：
- `after_local_id`：断点续拉（仅返回 `local_id > after_local_id` 的消息）

响应示例：
```json
{"username":"wxid_xxx","display_name":"同事","items":[{"local_id":1,"timestamp":1710000000,"base_type":1,"text":"晚上吃啥","raw":"晚上吃啥"}]}
```

说明：
- 返回的 `items[]` 会包含 `sender_username`/`is_send`/`direction`（可用来避免对自己消息触发自动回复）。
- 返回 `last_local_id` 便于上层保存游标。

## 消息处理状态（ACK / 幂等）

### `GET /api/v1/chats/{username}/message_status?app_id=xxx&local_id=123`

查询某条消息对某个上层应用的处理状态。

### `GET /api/v1/chats/{username}/message_status?app_id=xxx&status=&limit=50&offset=0`

列出该联系人下的处理状态记录（按 `updated_at` 倒序）。

### `POST /api/v1/chats/{username}/message_status`

写入/更新一条处理状态（Upsert）。

请求示例：
```json
{"app_id":"auto-reply","local_id":123,"status":"replied","info":{"reply_id":"..."}}
```

## 人物画像与记忆（不含 AI，仅存取）

存储文件：`persona_db`（默认 `persona.db`）。

### `GET /api/v1/people/{username}/profile`

获取画像（并返回联系人基础信息 `contact`）。

### `PATCH /api/v1/people/{username}/profile`

更新画像字段：`tags` / `notes` / `auto_reply_policy`。

### `GET /api/v1/people/{username}/memories?kind=&status=active&q=&limit=50&offset=0`

列出记忆条目（支持按 kind/status 搜索与分页）。

### `POST /api/v1/people/{username}/memories`

新增记忆条目（示例字段：`kind`/`key`/`value`/`importance`/`confidence`/`status`/`source`/`evidence`/`expires_at`）。

### `PATCH /api/v1/memories/{id}`

更新记忆条目（同上字段，按需传递）。

### `DELETE /api/v1/memories/{id}`

软删除：将条目标记为 `invalidated`。

## AI 运行记录（仅元数据存储，不执行 AI）

### `GET /api/v1/people/{username}/runs?kind=&status=&limit=20&offset=0`

列出画像/记忆/总结等 AI 运行记录（由上层应用写入）。

### `POST /api/v1/people/{username}/runs`

创建一条运行记录。

请求示例：
```json
{"app_id":"persona-ui","kind":"profile_refresh","status":"running","model":"gpt-4.1-mini","input_range":{"start_ts":1710000000,"end_ts":1710003600}}
```

### `PATCH /api/v1/runs/{run_id}`

更新运行记录（例如结束时写入 `status/finished_at/error/tokens`）。
