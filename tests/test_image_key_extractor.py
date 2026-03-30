import os
import tempfile
import unittest


class ImageKeyExtractorTests(unittest.TestCase):
    def _make_v2_dat(self, path: str, *, ciphertext16: bytes, xor_key: int) -> None:
        # Minimal V2 thumbnail .dat:
        # [6B magic][...header... up to >=31 bytes][...payload...][2B tail]
        magic = b"\x07\x08V2\x08\x07"
        header = bytearray(31)
        header[:6] = magic
        header[15:31] = ciphertext16

        body = bytes(header) + b"\x00" * 64
        tail = bytes([(0xFF ^ xor_key) & 0xFF, (0xD9 ^ xor_key) & 0xFF])
        data = body + tail

        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def test_derive_attach_dir_from_db_storage(self):
        import image_key_extractor as ike

        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, "wxid_xxx")
            db_dir = os.path.join(base, "db_storage")
            attach = os.path.join(base, "msg", "attach")
            os.makedirs(db_dir, exist_ok=True)
            os.makedirs(attach, exist_ok=True)

            got = ike.derive_attach_dir(db_dir)
            self.assertEqual(os.path.normcase(os.path.abspath(attach)), os.path.normcase(os.path.abspath(got)))

    def test_find_v2_ciphertext_and_xor_key(self):
        import image_key_extractor as ike

        with tempfile.TemporaryDirectory() as td:
            attach = os.path.join(td, "msg", "attach")
            dat_path = os.path.join(attach, "a", "b", "Img", "x_t.dat")

            ciphertext = b"\x01" * 16
            xor_key = 0xA2
            self._make_v2_dat(dat_path, ciphertext16=ciphertext, xor_key=xor_key)

            got_xor = ike.find_xor_key(attach)
            self.assertEqual(xor_key, got_xor)

            got_ct, got_name = ike.find_v2_ciphertext(attach)
            self.assertEqual(ciphertext, got_ct)
            self.assertEqual("x_t.dat", got_name)

    def test_try_key_accepts_jpeg_header(self):
        import image_key_extractor as ike

        from Crypto.Cipher import AES

        key = b"0123456789abcdef"
        plain = b"\xFF\xD8\xFF" + (b"\x00" * 13)
        cipher = AES.new(key, AES.MODE_ECB)
        ciphertext = cipher.encrypt(plain)

        self.assertTrue(ike._try_key(key, ciphertext))  # noqa: SLF001

    def test_find_aes_key_in_blob_supports_utf16(self):
        import image_key_extractor as ike

        from Crypto.Cipher import AES

        key_str = "0123456789abcdef"
        key = key_str.encode("ascii")
        plain = b"\xFF\xD8\xFF" + (b"\x00" * 13)
        ciphertext = AES.new(key, AES.MODE_ECB).encrypt(plain)

        blob_le = b"\x00\x11" + key_str.encode("utf-16le") + b"\x22\x00"
        self.assertEqual(key_str, ike.find_aes_key_in_blob(blob_le, ciphertext))

        blob_be = b"\x33\x00" + key_str.encode("utf-16be") + b"\x00\x44"
        self.assertEqual(key_str, ike.find_aes_key_in_blob(blob_be, ciphertext))

    def test_enum_regions_does_not_break_on_baseaddress_zero(self):
        import image_key_extractor as ike

        if os.name != "nt":
            self.skipTest("Windows only")

        pid = os.getpid()
        access = ike._PROCESS_VM_READ | ike._PROCESS_QUERY_INFORMATION  # noqa: SLF001
        h_process = ike._kernel32.OpenProcess(int(access), False, int(pid))  # noqa: SLF001
        if not h_process:
            self.skipTest("OpenProcess failed in test environment")

        try:
            regs = ike._enum_regions(h_process)  # noqa: SLF001
            self.assertGreater(len(regs), 0)
        finally:
            try:
                ike._kernel32.CloseHandle(h_process)  # noqa: SLF001
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
