import hashlib
import sqlite3

import zstandard as zstd


_zstd_dctx = zstd.ZstdDecompressor()


def format_msg_type(t: int) -> str:
    return {
        1: "文本",
        3: "图片",
        34: "语音",
        42: "名片",
        43: "视频",
        47: "表情",
        48: "位置",
        49: "链接/文件",
        50: "通话",
        10000: "系统",
        10002: "撤回",
    }.get(int(t or 0), f"type={int(t or 0)}")


def _is_safe_msg_table_name(table_name: str) -> bool:
    if not isinstance(table_name, str):
        return False
    if not table_name.startswith("Msg_") or len(table_name) != 36:
        return False
    tail = table_name[4:]
    return all(c in "0123456789abcdef" for c in tail)


def _decompress_message_content(content, ct_flag) -> str:
    if content is None:
        return ""
    if isinstance(content, bytes):
        if ct_flag == 4:
            try:
                return _zstd_dctx.decompress(content).decode("utf-8", errors="replace")
            except Exception:
                return ""
        return content.decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content
    try:
        return str(content)
    except Exception:
        return ""


def _load_name2id_map(conn: sqlite3.Connection) -> dict:
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return {}
    return {int(rid): (uname or "") for rid, uname in rows if uname}


def query_chat_history(
    decrypted_db_paths: list[str],
    username: str,
    contact_names: dict | None = None,
    self_username: str = "",
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
    after_local_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    include_raw: bool = True,
) -> dict:
    username = (username or "").strip()
    if not username:
        raise ValueError("username 不能为空")

    if start_ts is not None and end_ts is not None and int(start_ts) > int(end_ts):
        raise ValueError("start_ts > end_ts")

    try:
        limit = int(limit or 50)
    except Exception:
        limit = 50
    try:
        offset = int(offset or 0)
    except Exception:
        offset = 0
    limit = max(1, min(limit, 2000))
    offset = max(0, offset)

    if after_local_id is not None:
        try:
            after_local_id = int(after_local_id)
        except Exception:
            raise ValueError("invalid after_local_id")
        if after_local_id < 0:
            after_local_id = 0

    names = contact_names or {}
    self_username = (self_username or "").strip()

    is_group = "@chatroom" in username
    display_name = names.get(username, username)

    table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
    if not _is_safe_msg_table_name(table_name):
        raise ValueError("unsafe table name")

    candidate_limit = max(1, min(limit + offset, 10000))

    collected: list[tuple[int, int, dict]] = []
    warnings: list[str] = []

    for db_path in decrypted_db_paths or []:
        if not db_path:
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            id_map = _load_name2id_map(conn)

            clauses = []
            params: list[object] = []
            if start_ts is not None:
                clauses.append("create_time >= ?")
                params.append(int(start_ts))
            if end_ts is not None:
                clauses.append("create_time <= ?")
                params.append(int(end_ts))
            if after_local_id is not None:
                clauses.append("local_id > ?")
                params.append(int(after_local_id))

            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                f"""
                SELECT local_id, local_type, create_time, real_sender_id, message_content,
                       WCDB_CT_message_content
                FROM [{table_name}]
                {where_sql}
                ORDER BY create_time DESC
                LIMIT ?
                """,
                (*params, candidate_limit),
            ).fetchall()
            conn.close()

            for local_id, local_type, create_time, real_sender_id, content, ct in rows:
                raw = _decompress_message_content(content, ct)
                if local_type and local_type > 4294967296:
                    base_type = int(local_type % 4294967296)
                    sub_type = int(local_type >> 32)
                else:
                    base_type = int(local_type or 0)
                    sub_type = 0

                sender_username = id_map.get(int(real_sender_id or 0), "")
                sender_display = names.get(sender_username, sender_username) if sender_username else ""

                if base_type == 1:
                    text = raw
                else:
                    text = f"[{format_msg_type(base_type)}]"

                item = {
                    "local_id": local_id,
                    "timestamp": create_time,
                    "local_type": local_type,
                    "base_type": base_type,
                    "sub_type": sub_type,
                    "real_sender_id": real_sender_id or 0,
                    "sender_username": sender_username,
                    "sender_display_name": sender_display,
                    "is_group": is_group,
                    "text": text,
                }

                if self_username and sender_username:
                    is_send = sender_username == self_username
                    item["is_send"] = bool(is_send)
                    item["direction"] = "out" if is_send else "in"
                else:
                    item["is_send"] = None
                    item["direction"] = ""

                if include_raw:
                    item["raw"] = raw
                collected.append((int(create_time or 0), int(local_id or 0), item))
        except Exception as e:
            warnings.append(f"{db_path}: {e}")

    collected.sort(key=lambda x: (x[0], x[1]), reverse=True)
    paged = collected[offset : offset + limit]
    paged.sort(key=lambda x: (x[0], x[1]))
    items = [it for _, __, it in paged]

    last_local_id = 0
    for it in items:
        try:
            last_local_id = max(last_local_id, int(it.get("local_id") or 0))
        except Exception:
            pass

    payload = {
        "username": username,
        "display_name": display_name,
        "is_group": is_group,
        "offset": offset,
        "limit": limit,
        "after_local_id": int(after_local_id or 0),
        "last_local_id": last_local_id,
        "items": items,
    }
    if warnings:
        payload["warnings"] = warnings[:5]
    return payload

