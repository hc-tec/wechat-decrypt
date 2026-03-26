import os
import tempfile
import unittest
from unittest.mock import patch

import autostart
import config


class _FakeKey:
    def __init__(self, store):
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeWinreg:
    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self._values = {}

    def OpenKey(self, *_a, **_kw):
        return _FakeKey(self._values)

    def CreateKey(self, *_a, **_kw):
        return _FakeKey(self._values)

    def QueryValueEx(self, key, value_name):
        if value_name not in key._store:
            raise FileNotFoundError(value_name)
        return key._store[value_name], self.REG_SZ

    def SetValueEx(self, key, value_name, *_a):
        # signature: (key, value_name, reserved, type, value)
        value = _a[-1] if _a else ""
        key._store[value_name] = value

    def DeleteValue(self, key, value_name):
        if value_name not in key._store:
            raise FileNotFoundError(value_name)
        del key._store[value_name]


class ConfigAndAutostartTests(unittest.TestCase):
    def test_load_config_soft_creates_file_and_does_not_exit(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "config.json")
            with patch.object(config, "auto_detect_db_dir", return_value=None):
                cfg = config.load_config_soft(cfg_path)
            self.assertEqual(os.path.abspath(cfg_path), cfg["_config_file"])
            self.assertTrue(cfg["_setup_required"])
            self.assertTrue(os.path.exists(cfg_path))

    def test_load_config_soft_resolves_relative_paths(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "config.json")
            config.write_config_file(
                {
                    "db_dir": r"D:\xwechat_files\wxid_xxx\db_storage",
                    "keys_file": "all_keys.json",
                    "decrypted_dir": "decrypted",
                },
                cfg_path,
            )
            with patch.object(config, "auto_detect_db_dir", return_value=None):
                cfg = config.load_config_soft(cfg_path)
            self.assertTrue(os.path.isabs(cfg["keys_file"]))
            self.assertTrue(cfg["keys_file"].lower().endswith("all_keys.json"))

    def test_autostart_set_get_delete_with_fake_winreg(self):
        fake = _FakeWinreg()
        with patch.object(autostart, "_winreg", fake):
            autostart.set_run_command("App", "cmd --arg")
            self.assertEqual(autostart.get_run_command("App"), "cmd --arg")
            self.assertTrue(autostart.is_autostart_enabled("App"))
            autostart.delete_run_command("App")
            self.assertEqual(autostart.get_run_command("App"), "")


if __name__ == "__main__":
    unittest.main()

