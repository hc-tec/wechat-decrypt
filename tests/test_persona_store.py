import os
import tempfile
import unittest

from persona_store import PersonaStore


class PersonaStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "persona.db")
        self.store = PersonaStore(self.db_path)

    def test_profile_get_creates_row_and_patch_updates(self):
        profile = self.store.get_profile("wxid_alice")
        self.assertEqual(profile["username"], "wxid_alice")

        updated = self.store.patch_profile(
            "wxid_alice",
            {"tags": ["friend"], "notes": "met at work", "auto_reply_policy": {"enabled": True}},
        )
        self.assertEqual(updated["tags"], ["friend"])
        self.assertEqual(updated["notes"], "met at work")
        self.assertEqual(updated["auto_reply_policy"].get("enabled"), True)

    def test_memory_create_list_patch_and_soft_delete(self):
        res = self.store.create_memory(
            "wxid_bob",
            {
                "kind": "preference",
                "key": "food",
                "value": "likes noodles",
                "importance": 3,
                "confidence": 0.8,
                "status": "active",
                "source": "app:test",
                "evidence": [{"local_id": 1, "ts": 123, "quote": "I like noodles"}],
            },
        )
        mem_id = res["id"]

        listed = self.store.list_memories("wxid_bob", limit=10, offset=0)
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["items"][0]["id"], mem_id)

        self.store.patch_memory(mem_id, {"value": "likes rice noodles", "importance": 4})
        listed2 = self.store.list_memories("wxid_bob", limit=10, offset=0)
        self.assertEqual(listed2["items"][0]["value"], "likes rice noodles")
        self.assertEqual(listed2["items"][0]["importance"], 4)

        # soft delete => invalidated
        self.store.patch_memory(mem_id, {"status": "invalidated"})
        listed3 = self.store.list_memories("wxid_bob", status="invalidated", limit=10, offset=0)
        self.assertEqual(len(listed3["items"]), 1)

    def test_recent_contacts_record_and_list(self):
        self.store.record_recent_contact("wxid_carol", ts=111)
        self.store.record_recent_contact("wxid_carol", ts=222)
        self.store.record_recent_contact("wxid_dave", ts=150)

        rec = self.store.list_recent_contacts(limit=10, offset=0)
        self.assertEqual(rec["items"][0]["username"], "wxid_carol")
        self.assertEqual(rec["items"][0]["access_count"], 2)
        self.assertEqual(rec["items"][0]["last_access_ts"], 222)

    def test_message_status_upsert_get_and_list(self):
        item = self.store.upsert_message_status(
            "wxid_eve",
            {"app_id": "auto-reply", "local_id": 1, "status": "processing", "info": {"attempt": 1}},
        )
        self.assertEqual(item["app_id"], "auto-reply")
        self.assertEqual(item["local_id"], 1)
        self.assertEqual(item["status"], "processing")
        self.assertEqual(item["info"]["attempt"], 1)

        got = self.store.get_message_status("wxid_eve", "auto-reply", 1)
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "processing")

        # update same key
        item2 = self.store.upsert_message_status(
            "wxid_eve",
            {"app_id": "auto-reply", "local_id": 1, "status": "replied", "info": {"reply_id": "r1"}},
        )
        self.assertEqual(item2["status"], "replied")
        self.assertEqual(item2["info"]["reply_id"], "r1")

        listed = self.store.list_message_status("wxid_eve", "auto-reply", limit=10, offset=0)
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["items"][0]["status"], "replied")

    def test_runs_create_list_and_patch(self):
        res = self.store.create_run(
            "wxid_frank",
            {
                "app_id": "persona-ui",
                "kind": "profile_refresh",
                "status": "running",
                "model": "test-model",
                "input_range": {"start_ts": 1, "end_ts": 2},
            },
        )
        run_id = res["run_id"]

        runs = self.store.list_runs("wxid_frank", limit=10, offset=0)
        self.assertEqual(runs["items"][0]["run_id"], run_id)
        self.assertEqual(runs["items"][0]["status"], "running")

        self.store.patch_run(run_id, {"status": "success", "finished_at": 123, "tokens": {"total": 10}})
        runs2 = self.store.list_runs("wxid_frank", status="success", limit=10, offset=0)
        self.assertTrue(any(r["run_id"] == run_id for r in runs2["items"]))


if __name__ == "__main__":
    unittest.main()

