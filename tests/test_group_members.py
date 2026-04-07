import os
import sqlite3
import tempfile
import unittest

from monitor_web import query_group_members


def _create_contact_db(path: str):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE chat_room (
                id INTEGER PRIMARY KEY,
                username TEXT,
                owner TEXT,
                ext_buffer BLOB
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chat_room_info_detail (
                room_id_ INTEGER PRIMARY KEY,
                username_ TEXT,
                announcement_ TEXT,
                announcement_editor_ TEXT,
                announcement_publish_time_ INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chatroom_member (
                room_id INTEGER,
                member_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE contact (
                id INTEGER PRIMARY KEY,
                username TEXT,
                alias TEXT,
                nick_name TEXT,
                remark TEXT,
                small_head_url TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE stranger (
                id INTEGER PRIMARY KEY,
                username TEXT,
                alias TEXT,
                nick_name TEXT,
                remark TEXT,
                small_head_url TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


class GroupMembersTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "contact.db")
        _create_contact_db(self.db_path)

    def test_query_group_members_returns_remark_and_group_name(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO chat_room(id, username, owner, ext_buffer) VALUES (1, ?, ?, ?)",
                ("123@chatroom", "wxid_alice", b"wxid_alice\x00wxid_bob"),
            )
            conn.execute(
                """
                INSERT INTO chat_room_info_detail(
                    room_id_, username_, announcement_, announcement_editor_, announcement_publish_time_
                ) VALUES (1, ?, ?, ?, ?)
                """,
                ("123@chatroom", "请先看群公告", "wxid_alice", 1710000000),
            )
            conn.execute(
                """
                INSERT INTO contact(id, username, alias, nick_name, remark, small_head_url)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (100, "123@chatroom", "", "测试群", "", "group_head"),
            )
            conn.execute(
                """
                INSERT INTO contact(id, username, alias, nick_name, remark, small_head_url)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (11, "wxid_alice", "alice_id", "AliceNick", "AliceRemark", "alice_head"),
            )
            conn.execute(
                """
                INSERT INTO contact(id, username, alias, nick_name, remark, small_head_url)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (12, "wxid_bob", "bob_id", "BobNick", "", "bob_head"),
            )
            conn.execute("INSERT INTO chatroom_member(room_id, member_id) VALUES (1, 11)")
            conn.execute("INSERT INTO chatroom_member(room_id, member_id) VALUES (1, 12)")
            conn.commit()
        finally:
            conn.close()

        payload = query_group_members(
            self.db_path,
            group_username="123@chatroom",
            contact_names={
                "123@chatroom": "测试群",
                "wxid_alice": "AliceRemark",
                "wxid_bob": "BobNick",
            },
            self_username="wxid_bob",
        )

        self.assertEqual(payload["display_name"], "测试群")
        self.assertEqual(payload["announcement"], "请先看群公告")
        self.assertEqual(payload["owner_username"], "wxid_alice")
        self.assertEqual(payload["owner_display_name"], "AliceRemark")
        self.assertEqual(payload["member_count"], 2)
        self.assertEqual(payload["resolved_member_count"], 2)
        self.assertEqual(payload["unresolved_member_count"], 0)
        self.assertEqual([item["username"] for item in payload["items"]], ["wxid_alice", "wxid_bob"])
        self.assertEqual(payload["items"][0]["remark"], "AliceRemark")
        self.assertEqual(payload["items"][0]["display_name"], "AliceRemark")
        self.assertTrue(payload["items"][0]["is_owner"])
        self.assertEqual(payload["items"][1]["nick_name"], "BobNick")
        self.assertEqual(payload["items"][1]["display_name"], "BobNick")
        self.assertTrue(payload["items"][1]["is_self"])

    def test_query_group_members_falls_back_to_ext_buffer_for_missing_member_contact(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT INTO chat_room(id, username, owner, ext_buffer) VALUES (1, ?, ?, ?)",
                ("456@chatroom", "wxid_owner", b"\x00wxid_missing_user123\x00"),
            )
            conn.execute(
                """
                INSERT INTO contact(id, username, alias, nick_name, remark, small_head_url)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (200, "456@chatroom", "", "另一个群", "", "group_head"),
            )
            conn.execute("INSERT INTO chatroom_member(room_id, member_id) VALUES (1, 999)")
            conn.commit()
        finally:
            conn.close()

        payload = query_group_members(
            self.db_path,
            group_username="456@chatroom",
            contact_names={"456@chatroom": "另一个群"},
        )

        self.assertEqual(payload["member_count"], 1)
        self.assertEqual(payload["resolved_member_count"], 1)
        self.assertEqual(payload["unresolved_member_count"], 0)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["username"], "wxid_missing_user123")
        self.assertEqual(payload["items"][0]["display_name"], "wxid_missing_user123")


if __name__ == "__main__":
    unittest.main()
