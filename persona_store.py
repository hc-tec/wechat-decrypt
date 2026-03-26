import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


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

    @contextmanager
    def _conn(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_status (
                    app_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    local_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    info_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(app_id, username, local_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_msg_status_user_updated ON message_status(username, updated_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS person_run (
                    run_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    app_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    prompt_hash TEXT NOT NULL DEFAULT '',
                    input_range_json TEXT NOT NULL DEFAULT '{}',
                    tokens_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_user_updated ON person_run(username, updated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_user_kind_updated ON person_run(username, kind, updated_at DESC)"
            )

    # ---------------- Profiles ----------------

    def get_profile(self, username: str) -> dict:
        username = (username or "").strip()
        if not username:
            raise ValueError("username 不能为空")

        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
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

        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
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

        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
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
        with self._lock, self._conn() as conn:
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

    # ---------------- Message status (ACK) ----------------

    def upsert_message_status(self, username: str, payload: dict) -> dict:
        username = (username or "").strip()
        if not username:
            raise ValueError("username 不能为空")
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON 对象")

        app_id = (payload.get("app_id") or "").strip()
        if not app_id:
            raise ValueError("app_id 不能为空")

        try:
            local_id = int(payload.get("local_id") or 0)
        except Exception:
            local_id = 0
        if local_id <= 0:
            raise ValueError("local_id 必须为正整数")

        status = (payload.get("status") or "").strip()
        if not status:
            raise ValueError("status 不能为空")

        info = payload.get("info") or {}
        if info is None:
            info = {}
        if not isinstance(info, dict):
            raise ValueError("info 必须是对象")

        now = _now_ts()
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO message_status(app_id, username, local_id, status, info_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(app_id, username, local_id) DO UPDATE SET
                    status=excluded.status,
                    info_json=excluded.info_json,
                    updated_at=excluded.updated_at
                """,
                (
                    app_id,
                    username,
                    local_id,
                    status,
                    json.dumps(info, ensure_ascii=False),
                    now,
                    now,
                ),
            )

        item = self.get_message_status(username=username, app_id=app_id, local_id=local_id)
        return item or {
            "app_id": app_id,
            "username": username,
            "local_id": local_id,
            "status": status,
            "info": info,
            "created_at": now,
            "updated_at": now,
        }

    def get_message_status(self, username: str, app_id: str, local_id: int) -> dict | None:
        username = (username or "").strip()
        app_id = (app_id or "").strip()
        try:
            local_id = int(local_id or 0)
        except Exception:
            local_id = 0
        if not username or not app_id or local_id <= 0:
            return None

        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT app_id, username, local_id, status, info_json, created_at, updated_at "
                "FROM message_status WHERE app_id=? AND username=? AND local_id=?",
                (app_id, username, local_id),
            ).fetchone()
        if not row:
            return None

        try:
            info = json.loads(row["info_json"] or "{}")
        except Exception:
            info = {}
        return {
            "app_id": row["app_id"],
            "username": row["username"],
            "local_id": int(row["local_id"] or 0),
            "status": row["status"],
            "info": info if isinstance(info, dict) else {},
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
        }

    def list_message_status(
        self,
        username: str,
        app_id: str,
        status: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        username = (username or "").strip()
        app_id = (app_id or "").strip()
        status = (status or "").strip()
        if not username:
            raise ValueError("username 不能为空")
        if not app_id:
            raise ValueError("app_id 不能为空")

        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))

        clauses = ["username=?", "app_id=?"]
        params: list[object] = [username, app_id]
        if status:
            clauses.append("status=?")
            params.append(status)

        where_sql = "WHERE " + " AND ".join(clauses)
        sql = (
            "SELECT app_id, username, local_id, status, info_json, created_at, updated_at "
            "FROM message_status "
            f"{where_sql} "
            "ORDER BY updated_at DESC "
            "LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        items = []
        for r in rows:
            try:
                info = json.loads(r["info_json"] or "{}")
            except Exception:
                info = {}
            items.append(
                {
                    "app_id": r["app_id"],
                    "username": r["username"],
                    "local_id": int(r["local_id"] or 0),
                    "status": r["status"],
                    "info": info if isinstance(info, dict) else {},
                    "created_at": int(r["created_at"] or 0),
                    "updated_at": int(r["updated_at"] or 0),
                }
            )

        return {"items": items, "limit": limit, "offset": offset}

    # ---------------- Person runs (AI job metadata) ----------------

    def create_run(self, username: str, payload: dict) -> dict:
        username = (username or "").strip()
        if not username:
            raise ValueError("username 不能为空")
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON 对象")

        kind = (payload.get("kind") or "").strip()
        if not kind:
            raise ValueError("kind 不能为空")

        app_id = (payload.get("app_id") or "").strip()
        model = (payload.get("model") or "").strip()
        prompt_hash = (payload.get("prompt_hash") or "").strip()
        status = (payload.get("status") or "running").strip() or "running"
        error = (payload.get("error") or "").strip()

        input_range = payload.get("input_range") or {}
        if input_range is None:
            input_range = {}
        if not isinstance(input_range, dict):
            raise ValueError("input_range 必须是对象")

        tokens = payload.get("tokens") or {}
        if tokens is None:
            tokens = {}
        if not isinstance(tokens, dict):
            raise ValueError("tokens 必须是对象")

        started_at = payload.get("started_at")
        try:
            started_at = int(started_at) if started_at is not None else _now_ts()
        except Exception:
            started_at = _now_ts()

        finished_at = payload.get("finished_at")
        try:
            finished_at = int(finished_at) if finished_at is not None else 0
        except Exception:
            finished_at = 0

        now = _now_ts()
        run_id = str(uuid.uuid4())
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO person_run(
                    run_id, username, app_id, kind, status, model, prompt_hash,
                    input_range_json, tokens_json, error,
                    started_at, finished_at, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    username,
                    app_id,
                    kind,
                    status,
                    model,
                    prompt_hash,
                    json.dumps(input_range, ensure_ascii=False),
                    json.dumps(tokens, ensure_ascii=False),
                    error,
                    started_at,
                    finished_at,
                    now,
                    now,
                ),
            )

        return {"run_id": run_id}

    def list_runs(
        self,
        username: str,
        kind: str = "",
        status: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        username = (username or "").strip()
        if not username:
            raise ValueError("username 不能为空")
        kind = (kind or "").strip()
        status = (status or "").strip()
        limit = max(1, min(int(limit or 20), 200))
        offset = max(0, int(offset or 0))

        clauses = ["username=?"]
        params: list[object] = [username]
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if status:
            clauses.append("status=?")
            params.append(status)
        where_sql = "WHERE " + " AND ".join(clauses)
        sql = (
            "SELECT run_id, username, app_id, kind, status, model, prompt_hash, "
            "input_range_json, tokens_json, error, started_at, finished_at, created_at, updated_at "
            "FROM person_run "
            f"{where_sql} "
            "ORDER BY updated_at DESC "
            "LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        with self._lock, self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()

        items = []
        for r in rows:
            try:
                input_range = json.loads(r["input_range_json"] or "{}")
            except Exception:
                input_range = {}
            try:
                tokens = json.loads(r["tokens_json"] or "{}")
            except Exception:
                tokens = {}
            items.append(
                {
                    "run_id": r["run_id"],
                    "username": r["username"],
                    "app_id": r["app_id"] or "",
                    "kind": r["kind"],
                    "status": r["status"],
                    "model": r["model"] or "",
                    "prompt_hash": r["prompt_hash"] or "",
                    "input_range": input_range if isinstance(input_range, dict) else {},
                    "tokens": tokens if isinstance(tokens, dict) else {},
                    "error": r["error"] or "",
                    "started_at": int(r["started_at"] or 0),
                    "finished_at": int(r["finished_at"] or 0),
                    "created_at": int(r["created_at"] or 0),
                    "updated_at": int(r["updated_at"] or 0),
                }
            )

        return {"items": items, "limit": limit, "offset": offset}

    def patch_run(self, run_id: str, patch: dict) -> None:
        run_id = (run_id or "").strip()
        if not run_id:
            raise ValueError("run_id 不能为空")
        if not isinstance(patch, dict):
            raise ValueError("patch 必须是 JSON 对象")

        allowed = {
            "app_id",
            "kind",
            "status",
            "model",
            "prompt_hash",
            "input_range",
            "tokens",
            "error",
            "started_at",
            "finished_at",
        }
        unknown = set(patch.keys()) - allowed
        if unknown:
            raise ValueError(f"未知字段: {', '.join(sorted(unknown))}")

        fields = []
        params: list[object] = []

        if "app_id" in patch:
            fields.append("app_id=?")
            params.append((patch.get("app_id") or "").strip())
        if "kind" in patch:
            kind = (patch.get("kind") or "").strip()
            if not kind:
                raise ValueError("kind 不能为空")
            fields.append("kind=?")
            params.append(kind)
        if "status" in patch:
            status = (patch.get("status") or "").strip()
            if not status:
                raise ValueError("status 不能为空")
            fields.append("status=?")
            params.append(status)
        if "model" in patch:
            fields.append("model=?")
            params.append((patch.get("model") or "").strip())
        if "prompt_hash" in patch:
            fields.append("prompt_hash=?")
            params.append((patch.get("prompt_hash") or "").strip())
        if "input_range" in patch:
            input_range = patch.get("input_range") or {}
            if input_range is None:
                input_range = {}
            if not isinstance(input_range, dict):
                raise ValueError("input_range 必须是对象")
            fields.append("input_range_json=?")
            params.append(json.dumps(input_range, ensure_ascii=False))
        if "tokens" in patch:
            tokens = patch.get("tokens") or {}
            if tokens is None:
                tokens = {}
            if not isinstance(tokens, dict):
                raise ValueError("tokens 必须是对象")
            fields.append("tokens_json=?")
            params.append(json.dumps(tokens, ensure_ascii=False))
        if "error" in patch:
            fields.append("error=?")
            params.append((patch.get("error") or "").strip())
        if "started_at" in patch:
            try:
                started_at = int(patch.get("started_at") or 0)
            except Exception:
                started_at = 0
            if started_at <= 0:
                raise ValueError("started_at 必须为正整数")
            fields.append("started_at=?")
            params.append(started_at)
        if "finished_at" in patch:
            try:
                finished_at = int(patch.get("finished_at") or 0)
            except Exception:
                finished_at = 0
            fields.append("finished_at=?")
            params.append(finished_at)

        if not fields:
            return

        fields.append("updated_at=?")
        params.append(_now_ts())
        params.append(run_id)

        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE person_run SET " + ", ".join(fields) + " WHERE run_id=?",
                tuple(params),
            )
            if cur.rowcount <= 0:
                raise ValueError("run 不存在")
