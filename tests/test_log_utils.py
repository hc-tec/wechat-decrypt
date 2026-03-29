import os
import tempfile
import unittest

from log_utils import RotatingFileWriter, get_log_dir


class LogUtilsTests(unittest.TestCase):
    def test_get_log_dir_creates_logs_next_to_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = os.path.join(td, "config.json")
            log_dir = get_log_dir(cfg_path)
            self.assertTrue(os.path.isdir(log_dir))
            self.assertEqual(os.path.abspath(log_dir), os.path.abspath(os.path.join(td, "logs")))

    def test_rotating_file_writer_rotates(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "service.log")
            w = RotatingFileWriter(p, max_bytes=200, backup_count=2)
            try:
                for _ in range(5):
                    w.write(("x" * 160) + "\n")
                self.assertTrue(os.path.exists(p))
                self.assertTrue(os.path.exists(p + ".1"))
            finally:
                w.close()


if __name__ == "__main__":
    unittest.main()

