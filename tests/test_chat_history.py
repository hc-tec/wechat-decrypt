import hashlib
import os
import sqlite3
import tempfile
import unittest

from chat_history import query_chat_history


def _msg_table_name(username: str) -> str:
    return f"Msg_{hashlib.md5(username.encode()).hexdigest()}"


def _create_message_db(
    path: str,
    chat_username: str,
    rows: list[tuple[int, int, int, int, str]],
    *,
    name2id_users: list[str] | None = None,
):
    # rows: (local_id, local_type, create_time, real_sender_id, content)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        users = name2id_users or ["wxid_me", chat_username]
        for user in users:
            conn.execute("INSERT INTO Name2Id(user_name) VALUES (?)", (user,))

        table_name = _msg_table_name(chat_username)
        conn.execute(
            f"""
            CREATE TABLE [{table_name}] (
                local_id INTEGER,
                local_type INTEGER,
                create_time INTEGER,
                real_sender_id INTEGER,
                message_content TEXT,
                WCDB_CT_message_content INTEGER
            )
            """
        )
        for local_id, local_type, create_time, real_sender_id, content in rows:
            conn.execute(
                f"""
                INSERT INTO [{table_name}] (
                    local_id, local_type, create_time, real_sender_id,
                    message_content, WCDB_CT_message_content
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (local_id, local_type, create_time, real_sender_id, content, 0),
            )
        conn.commit()
    finally:
        conn.close()


class ChatHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_query_history_returns_direction_and_supports_after_local_id(self):
        db_path = os.path.join(self.temp_dir.name, "message_0.db")
        chat = "wxid_alice"
        _create_message_db(
            db_path,
            chat,
            [
                (1, 1, 100, 2, "hi"),   # from alice
                (2, 1, 110, 1, "yo"),   # from me
                (3, 1, 120, 2, "ok"),   # from alice
            ],
        )

        payload = query_chat_history(
            [db_path],
            username=chat,
            contact_names={"wxid_me": "Me", chat: "Alice"},
            self_username="wxid_me",
            limit=10,
            offset=0,
            include_raw=True,
        )
        self.assertEqual(payload["username"], chat)
        self.assertEqual([it["local_id"] for it in payload["items"]], [1, 2, 3])
        self.assertEqual(payload["items"][0]["direction"], "in")
        self.assertEqual(payload["items"][1]["direction"], "out")
        self.assertEqual(payload["items"][2]["direction"], "in")
        self.assertEqual(payload["last_local_id"], 3)

        payload2 = query_chat_history(
            [db_path],
            username=chat,
            contact_names={},
            self_username="wxid_me",
            after_local_id=2,
            limit=10,
            offset=0,
        )
        self.assertEqual([it["local_id"] for it in payload2["items"]], [3])

    def test_group_history_strips_sender_prefix_and_uses_sender_fields(self):
        db_path = os.path.join(self.temp_dir.name, "message_0.db")
        chat = "123456@chatroom"
        _create_message_db(
            db_path,
            chat,
            [
                (1, 1, 100, 0, "wxid_alice:\nhello group"),
                (2, 3, 101, 0, "wxid_alice:\n<msg><img md5=\"abc\" /></msg>"),
            ],
            name2id_users=["wxid_me", chat],
        )

        payload = query_chat_history(
            [db_path],
            username=chat,
            contact_names={"wxid_alice": "Alice", chat: "Test Group"},
            self_username="wxid_me",
            limit=10,
            offset=0,
            include_raw=True,
        )

        self.assertEqual(payload["display_name"], "Test Group")
        self.assertEqual(payload["items"][0]["sender_username"], "wxid_alice")
        self.assertEqual(payload["items"][0]["sender_display_name"], "Alice")
        self.assertEqual(payload["items"][0]["text"], "hello group")
        self.assertEqual(payload["items"][0]["raw"], "hello group")
        self.assertEqual(payload["items"][1]["sender_username"], "wxid_alice")
        self.assertEqual(payload["items"][1]["sender_display_name"], "Alice")
        self.assertEqual(payload["items"][1]["raw"], "<msg><img md5=\"abc\" /></msg>")


if __name__ == "__main__":
    unittest.main()
