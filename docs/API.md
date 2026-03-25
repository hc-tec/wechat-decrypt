# WeChat Data Service API（本地离线 / Windows）

默认监听地址：`http://127.0.0.1:5678`

说明：
- 本服务只读微信本地数据库并解密/解析，**不提供 AI**。
- 适配上层应用：自动回复、个性化对话取数、人物画像/记忆（后续可扩展写接口）。

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
{"time":1710000000,"last_seq":123,"contacts_loaded":true}
```

## 联系人与会话

### `GET /api/v1/contacts?query=&limit=50`

联系人检索（昵称/备注/微信号模糊匹配）。

响应示例：
```json
{"items":[{"username":"wxid_xxx","nick_name":"张三","remark":"同事","display_name":"同事","is_group":false}]}
```

### `GET /api/v1/sessions?limit=50`

最近会话列表（来自 `session.db`，含未读数和最新消息摘要）。

响应示例：
```json
{"items":[{"username":"wxid_xxx","display_name":"同事","unread":0,"last_timestamp":1710000000,"last_msg_type":1,"summary":"晚上吃啥"}]}
```

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

响应示例：
```json
{"username":"wxid_xxx","display_name":"同事","items":[{"local_id":1,"timestamp":1710000000,"base_type":1,"text":"晚上吃啥","raw":"晚上吃啥"}]}
```

