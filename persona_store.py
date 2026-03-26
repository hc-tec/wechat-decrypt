import json
import os
import sqlite3
import threading
import time
import uuid


def _now_ts() -> int:
    return int(time.time())


class PersonaStore:
    """本地人物画像/记忆/最近联系人存储（独立 SQLite，不修改微信原库）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS person_profile (
                    username TEXT PRIMARY KEY,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    auto_reply_policy_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_item (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL DEFAULT '',
                    value TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    source TEXT NOT NULL DEFAULT 'manual',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    expires_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_user_updated ON memory_item(username, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_user_kind_status ON memory_item(username, kind, status)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS recent_contact (
                    username TEXT PRIMARY KEY,
                    last_access_ts INTEGER NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 1
                )
                """
            )

    # ---------------- Profiles ----------------

    def get_profile(self, username: str) -> dict:
        username = (username or "").strip()
        if not username:
            raise ValueError("username 不能为空")

        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT username, tags_json, notes, auto_reply_policy_json, created_at, updated_at "
                "FROM person_profile WHERE username=?",
                (username,),
            ).fetchone()
            if not row:
                now = _now_ts()
                conn.execute(
                    "INSERT INTO person_profile(username, tags_json, notes, auto_reply_policy_json, created_at, updated_at) "
                    "VALUES(?, '[]', '', '{}', ?, ?)",
                    (username, now, now),
                )
                row = conn.execute(
                    "SELECT username, tags_json, notes, auto_reply_policy_json, created_at, updated_at "
                    "FROM person_profile WHERE username=?",
                    (username,),
                ).fetchone()

        tags = json.loads(row["tags_json"] or "[]")
        policy = json.loads(row["auto_reply_policy_json"] or "{}")
        return {
            "username": row["username"],
            "tags": tags if isinstance(tags, list) else [],
            "notes": row["notes"] or "",
            "auto_reply_policy": policy if isinstance(policy, dict) else {},
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
        }

    def patch_profile(self, username: str, patch: dict) -> dict:
        username = (username or "").strip()
        if not username:
            raise ValueError("username 不能为空")
        if not isinstance(patch, dict):
            raise ValueError("patch 必须是 JSON 对象")

        allowed = {"tags", "notes", "auto_reply_policy"}
        unknown = set(patch.keys()) - allowed
        if unknown:
            raise ValueError(f"未知字段: {', '.join(sorted(unknown))}")

        tags = patch.get("tags")
        notes = patch.get("notes")
        policy = patch.get("auto_reply_policy")

        if tags is not None and not isinstance(tags, list):
            raise ValueError("tags 必须是数组")
        if notes is not None and not isinstance(notes, str):
            raise ValueError("notes 必须是字符串")
        if policy is not None and not isinstance(policy, dict):
            raise ValueError("auto_reply_policy 必须是对象")

        now = _now_ts()
        with self._lock, self._connect() as conn:
            # ensure row exists
            conn.execute(
                "INSERT OR IGNORE INTO person_profile(username, created_at, updated_at) VALUES(?, ?, ?)",
                (username, now, now),
            )

            fields = []
            params = []
            if tags is not None:
                fields.append("tags_json=?")
                params.append(json.dumps(tags, ensure_ascii=False))
            if notes is not None:
                fields.append("notes=?")
                params.append(notes)
            if policy is not None:
                fields.append("auto_reply_policy_json=?")
                params.append(json.dumps(policy, ensure_ascii=False))
            fields.append("updated_at=?")
            params.append(now)

            params.append(username)
            conn.execute(
                "UPDATE person_profile SET " + ", ".join(fields) + " WHERE username=?",
                tuple(params),
            )

        return self.get_profile(username)

    # ---------------- Memories ----------------

    def list_memories(
        self,
        username: str,
        kind: str = "",
        status: str = "active",
        q: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        username = (username or "").strip()
        if not username:
            raise ValueError("username 不能为空")
        kind = (kind or "").strip()
        status = (status or "").strip() or "active"
        q = (q or "").strip()
        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))

        clauses = ["username=?"]
        params: list[object] = [username]
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if status:
            clauses.append("status=?")
            params.append(status)
        if q:
            clauses.append("(value LIKE ? OR key LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])

        where_sql = "WHERE " + " AND ".join(clauses)
        sql = (
            "SELECT id, username, kind, key, value, importance, confidence, status, source, evidence_json, "
            "expires_at, created_at, updated_at "
            "FROM memory_item "
            f"{where_sql} "
            "ORDER BY updated_at DESC "
            "LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        items = []
        for r in rows:
            try:
                evidence = json.loads(r["evidence_json"] or "[]")
            except Exception:
                evidence = []
            items.append(
                {
                    "id": r["id"],
                    "username": r["username"],
                    "kind": r["kind"],
                    "key": r["key"] or "",
                    "value": r["value"] or "",
                    "importance": int(r["importance"] or 0),
                    "confidence": float(r["confidence"] or 0),
                    "status": r["status"],
                    "source": r["source"],
                    "evidence": evidence if isinstance(evidence, list) else [],
                    "expires_at": int(r["expires_at"] or 0),
                    "created_at": int(r["created_at"] or 0),
                    "updated_at": int(r["updated_at"] or 0),
                }
            )

        return {"items": items, "limit": limit, "offset": offset}

    def create_memory(self, username: str, payload: dict) -> dict:
        username = (username or "").strip()
        if not username:
            raise ValueError("username 不能为空")
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON 对象")

        kind = (payload.get("kind") or "").strip()
        if not kind:
            raise ValueError("kind 不能为空")
        key = (payload.get("key") or "").strip()
        value = (payload.get("value") or "").strip()
        if not value:
            raise ValueError("value 不能为空")

        importance = int(payload.get("importance") or 0)
        importance = max(0, min(importance, 5))
        confidence = float(payload.get("confidence") or 0)
        confidence = max(0.0, min(confidence, 1.0))
        status = (payload.get("status") or "active").strip() or "active"
        source = (payload.get("source") or "manual").strip() or "manual"
        evidence = payload.get("evidence") or []
        if evidence is None:
            evidence = []
        if not isinstance(evidence, list):
            raise ValueError("evidence 必须是数组")
        expires_at = int(payload.get("expires_at") or 0)

        now = _now_ts()
        mem_id = str(uuid.uuid4())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_item(
                    id, username, kind, key, value, importance, confidence, status, source, evidence_json,
                    expires_at, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    mem_id,
                    username,
                    kind,
                    key,
                    value,
                    importance,
                    confidence,
                    status,
                    source,
                    json.dumps(evidence, ensure_ascii=False),
                    expires_at,
                    now,
                    now,
                ),
            )

        return {"id": mem_id}

    def patch_memory(self, mem_id: str, patch: dict) -> None:
        mem_id = (mem_id or "").strip()
        if not mem_id:
            raise ValueError("id 不能为空")
        if not isinstance(patch, dict):
            raise ValueError("patch 必须是 JSON 对象")

        allowed = {
            "kind",
            "key",
            "value",
            "importance",
            "confidence",
            "status",
            "source",
            "evidence",
            "expires_at",
        }
        unknown = set(patch.keys()) - allowed
        if unknown:
            raise ValueError(f"未知字段: {', '.join(sorted(unknown))}")

        fields = []
        params: list[object] = []

        if "kind" in patch:
            kind = (patch.get("kind") or "").strip()
            if not kind:
                raise ValueError("kind 不能为空")
            fields.append("kind=?")
            params.append(kind)
        if "key" in patch:
            fields.append("key=?")
            params.append((patch.get("key") or "").strip())
        if "value" in patch:
            value = (patch.get("value") or "").strip()
            if not value:
                raise ValueError("value 不能为空")
            fields.append("value=?")
            params.append(value)
        if "importance" in patch:
            importance = int(patch.get("importance") or 0)
            importance = max(0, min(importance, 5))
            fields.append("importance=?")
            params.append(importance)
        if "confidence" in patch:
            confidence = float(patch.get("confidence") or 0)
            confidence = max(0.0, min(confidence, 1.0))
            fields.append("confidence=?")
            params.append(confidence)
        if "status" in patch:
            fields.append("status=?")
            params.append((patch.get("status") or "active").strip() or "active")
        if "source" in patch:
            fields.append("source=?")
            params.append((patch.get("source") or "manual").strip() or "manual")
        if "evidence" in patch:
            evidence = patch.get("evidence") or []
            if evidence is None:
                evidence = []
            if not isinstance(evidence, list):
                raise ValueError("evidence 必须是数组")
            fields.append("evidence_json=?")
            params.append(json.dumps(evidence, ensure_ascii=False))
        if "expires_at" in patch:
            fields.append("expires_at=?")
            params.append(int(patch.get("expires_at") or 0))

        if not fields:
            return

        fields.append("updated_at=?")
        params.append(_now_ts())
        params.append(mem_id)

        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE memory_item SET " + ", ".join(fields) + " WHERE id=?",
                tuple(params),
            )
            if cur.rowcount <= 0:
                raise ValueError("memory 不存在")

    # ---------------- Recent contacts ----------------

    def record_recent_contact(self, username: str, ts: int | None = None) -> None:
        username = (username or "").strip()
        if not username:
            return
        ts = int(ts or _now_ts())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recent_contact(username, last_access_ts, access_count)
                VALUES(?, ?, 1)
                ON CONFLICT(username) DO UPDATE SET
                    last_access_ts=excluded.last_access_ts,
                    access_count=recent_contact.access_count+1
                """,
                (username, ts),
            )

    def list_recent_contacts(self, limit: int = 20, offset: int = 0) -> dict:
        limit = max(1, min(int(limit or 20), 500))
        offset = max(0, int(offset or 0))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT username, last_access_ts, access_count "
                "FROM recent_contact "
                "ORDER BY last_access_ts DESC "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        items = [
            {
                "username": r["username"],
                "last_access_ts": int(r["last_access_ts"] or 0),
                "access_count": int(r["access_count"] or 0),
            }
            for r in rows
        ]
        return {"items": items, "limit": limit, "offset": offset}

