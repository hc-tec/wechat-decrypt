from __future__ import annotations

import ctypes
import glob
import os
import re
import struct
import subprocess
import time
from ctypes import wintypes
from typing import Callable


class ImageKeyError(RuntimeError):
    def __init__(self, message: str, *, code: str = "unknown"):
        super().__init__(message)
        self.code = code


class ImageKeyCancelled(ImageKeyError):
    def __init__(self, message: str = "cancelled"):
        super().__init__(message, code="cancelled")


_V2_MAGIC = b"\x07\x08V2\x08\x07"

# 正则：精确 32/16 字符 alphanum（前后是非 alphanum 或边界）
_RE_KEY32 = re.compile(rb"(?<![a-zA-Z0-9])[a-zA-Z0-9]{32}(?![a-zA-Z0-9])")
_RE_KEY16 = re.compile(rb"(?<![a-zA-Z0-9])[a-zA-Z0-9]{16}(?![a-zA-Z0-9])")


def _safe_mtime(path: str) -> float:
    try:
        return float(os.path.getmtime(path))
    except OSError:
        return 0.0


def derive_attach_dir(db_dir: str) -> str | None:
    db_dir = os.path.abspath(os.path.expanduser((db_dir or "").strip()))
    if not db_dir or not os.path.isdir(db_dir):
        return None

    base_dir = os.path.dirname(db_dir) if os.path.basename(db_dir).lower() == "db_storage" else db_dir

    candidates = [
        os.path.join(base_dir, "msg", "attach"),
        os.path.join(base_dir, "Msg", "attach"),
        os.path.join(base_dir, "Msg", "Attach"),
    ]
    for p in candidates:
        if os.path.isdir(p):
            return p

    # 兜底：从 base_dir\msg 内部少量扫描，找到一个 V2 缩略图再反推 attach_dir。
    msg_root = os.path.join(base_dir, "msg")
    if not os.path.isdir(msg_root):
        msg_root = os.path.join(base_dir, "Msg")
    if not os.path.isdir(msg_root):
        return None

    root_depth = os.path.abspath(msg_root).count(os.sep)
    max_depth = 7
    try:
        for dirpath, dirnames, filenames in os.walk(msg_root):
            depth = os.path.abspath(dirpath).count(os.sep) - root_depth
            if depth > max_depth:
                dirnames[:] = []
                continue
            if os.path.basename(dirpath).lower() != "img":
                continue
            for fn in filenames:
                if not fn.endswith("_t.dat"):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "rb") as f:
                        head = f.read(6)
                    if head != _V2_MAGIC:
                        continue
                except OSError:
                    continue
                # ...\attach\<a>\<b>\Img\file
                parent = os.path.dirname(dirpath)  # <b>
                parent2 = os.path.dirname(parent)  # <a>
                attach = os.path.dirname(parent2)  # attach
                if os.path.isdir(attach):
                    return attach
    except Exception:
        return None

    return None


def find_v2_ciphertext(attach_dir: str) -> tuple[bytes | None, str | None]:
    """从多个 V2 .dat 文件中提取第一个 AES 密文块 (16 bytes)"""
    attach_dir = os.path.abspath(os.path.expanduser((attach_dir or "").strip()))
    if not attach_dir or not os.path.isdir(attach_dir):
        return None, None

    pattern = os.path.join(attach_dir, "*", "*", "Img", "*_t.dat")
    dat_files = sorted(glob.glob(pattern), key=_safe_mtime, reverse=True)
    for f in dat_files[:200]:
        try:
            with open(f, "rb") as fp:
                header = fp.read(31)
            if header[:6] == _V2_MAGIC and len(header) >= 31:
                return header[15:31], os.path.basename(f)
        except OSError:
            continue
        except Exception:
            continue
    return None, None


def find_xor_key(attach_dir: str) -> int | None:
    """从缩略图文件末尾推导 XOR key (JPEG 结尾 FF D9)"""
    attach_dir = os.path.abspath(os.path.expanduser((attach_dir or "").strip()))
    if not attach_dir or not os.path.isdir(attach_dir):
        return None

    pattern = os.path.join(attach_dir, "*", "*", "Img", "*_t.dat")
    dat_files = sorted(glob.glob(pattern), key=_safe_mtime, reverse=True)

    tail_counts: dict[tuple[int, int], int] = {}
    for f in dat_files[:48]:
        try:
            sz = os.path.getsize(f)
            if sz < 32:
                continue
            with open(f, "rb") as fp:
                head = fp.read(6)
                fp.seek(sz - 2)
                tail = fp.read(2)
            if head != _V2_MAGIC or len(tail) != 2:
                continue
            key = (tail[0], tail[1])
            tail_counts[key] = tail_counts.get(key, 0) + 1
        except OSError:
            continue
        except Exception:
            continue

    if not tail_counts:
        return None

    most_common = max(tail_counts, key=tail_counts.get)
    x, y = most_common
    xor_key = x ^ 0xFF
    check = y ^ 0xD9
    return xor_key if xor_key == check else xor_key


def get_wechat_pids(*, process_name: str = "Weixin.exe") -> list[int]:
    process_name = (process_name or "Weixin.exe").strip() or "Weixin.exe"
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return []

    pids: list[int] = []
    for line in (result.stdout or "").strip().split("\n"):
        if process_name.lower() not in line.lower():
            continue
        parts = line.strip().strip('"').split('","')
        if len(parts) >= 2:
            try:
                pids.append(int(parts[1]))
            except Exception:
                continue
    # de-dup preserve order
    seen: set[int] = set()
    out: list[int] = []
    for pid in pids:
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
    return out


def _try_key(key_bytes: bytes, ciphertext16: bytes) -> bool:
    try:
        from Crypto.Cipher import AES  # pylint: disable=import-outside-toplevel
    except Exception:
        raise ImageKeyError("缺少依赖：pycryptodome", code="dependency")

    try:
        cipher = AES.new(key_bytes[:16], AES.MODE_ECB)
        dec = cipher.decrypt(ciphertext16)
    except Exception:
        return False

    # 常见图片 magic
    if dec[:3] == b"\xFF\xD8\xFF":  # JPEG
        return True
    if dec[:4] == b"\x89PNG":
        return True
    if dec[:4] == b"RIFF":  # WEBP
        return True
    if dec[:4] == b"wxgf":  # HEVC (wxgf)
        return True
    if dec[:3] == b"GIF":
        return True
    return False


# Windows API constants
_PROCESS_VM_READ = 0x0010
_PROCESS_QUERY_INFORMATION = 0x0400
_MEM_COMMIT = 0x1000
_PAGE_NOACCESS = 0x01
_PAGE_GUARD = 0x100
_PAGE_READWRITE = 0x04
_PAGE_WRITECOPY = 0x08
_PAGE_EXECUTE_READWRITE = 0x40
_PAGE_EXECUTE_WRITECOPY = 0x80


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


_kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]


def _is_rw_protect(protect: int) -> bool:
    rw_flags = _PAGE_READWRITE | _PAGE_WRITECOPY | _PAGE_EXECUTE_READWRITE | _PAGE_EXECUTE_WRITECOPY
    return (int(protect) & int(rw_flags)) != 0


def _read_process_memory(h_process, addr: int, size: int) -> bytes | None:
    buf = ctypes.create_string_buffer(int(size))
    n = ctypes.c_size_t(0)
    ok = _kernel32.ReadProcessMemory(h_process, ctypes.c_void_p(int(addr)), buf, int(size), ctypes.byref(n))
    if not ok or int(n.value) <= 0:
        return None
    return buf.raw[: int(n.value)]


def _enum_regions(h_process) -> list[tuple[int, int, int]]:
    regs: list[tuple[int, int, int]] = []
    addr = 0
    mbi = _MEMORY_BASIC_INFORMATION()
    while addr < 0x7FFFFFFFFFFF:
        result = _kernel32.VirtualQueryEx(
            h_process, ctypes.c_void_p(int(addr)), ctypes.byref(mbi), ctypes.sizeof(mbi)
        )
        if result == 0:
            break
        try:
            base = int(mbi.BaseAddress)  # type: ignore[arg-type]
            size = int(mbi.RegionSize)
            state = int(mbi.State)
            protect = int(mbi.Protect)
        except Exception:
            break

        if state == _MEM_COMMIT and protect != _PAGE_NOACCESS and (protect & _PAGE_GUARD) == 0 and size > 0:
            regs.append((base, size, protect))

        nxt = base + size
        if nxt <= addr:
            break
        addr = nxt
    return regs


def scan_memory_for_aes_key(
    pid: int,
    ciphertext16: bytes,
    *,
    stop_check: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
) -> str | None:
    if not ciphertext16 or len(ciphertext16) != 16:
        raise ImageKeyError("ciphertext 无效", code="ciphertext_invalid")

    access = _PROCESS_VM_READ | _PROCESS_QUERY_INFORMATION
    h_process = _kernel32.OpenProcess(int(access), False, int(pid))
    if not h_process:
        raise ImageKeyError("无法打开微信进程（可能需要管理员权限）", code="permission")

    try:
        regs = _enum_regions(h_process)
        rw_regs = [r for r in regs if _is_rw_protect(r[2])]
        other_regs = [r for r in regs if r not in rw_regs]

        def _scan(regions: list[tuple[int, int, int]]) -> str | None:
            chunk_size = 2 * 1024 * 1024
            overlap = 64
            tail = b""
            for idx, (base, size, _protect) in enumerate(regions):
                if stop_check and stop_check():
                    raise ImageKeyCancelled()
                if progress and idx % 40 == 0:
                    progress(f"扫描中… {idx}/{len(regions)}")

                # 逐块读取，避免一次性分配大块内存
                offset = 0
                while offset < size:
                    if stop_check and stop_check():
                        raise ImageKeyCancelled()
                    to_read = min(chunk_size, int(size - offset))
                    data = _read_process_memory(h_process, base + offset, to_read)
                    offset += to_read
                    if not data or len(data) < 32:
                        continue

                    buf = tail + data
                    # 32-char candidates
                    for m in _RE_KEY32.finditer(buf):
                        key_bytes = m.group()
                        if _try_key(key_bytes[:16], ciphertext16):
                            return key_bytes[:16].decode("ascii", errors="ignore")
                        if _try_key(key_bytes, ciphertext16):
                            return key_bytes.decode("ascii", errors="ignore")
                    # 16-char candidates
                    for m in _RE_KEY16.finditer(buf):
                        key_bytes = m.group()
                        if _try_key(key_bytes, ciphertext16):
                            return key_bytes.decode("ascii", errors="ignore")

                    tail = buf[-overlap:] if len(buf) > overlap else buf
            return None

        if progress:
            progress("Phase 1/2：扫描 RW 内存")
        found = _scan(rw_regs)
        if found:
            return found

        if progress:
            progress("Phase 2/2：扫描其它可读内存")
        return _scan(other_regs)
    finally:
        try:
            _kernel32.CloseHandle(h_process)
        except Exception:
            pass


def extract_image_keys(
    db_dir: str,
    *,
    process_name: str = "Weixin.exe",
    stop_check: Callable[[], bool] | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[str, int | None]:
    """提取图片 V2 的 AES key + XOR key（XOR 可能为 None）。"""
    db_dir = os.path.abspath(os.path.expanduser((db_dir or "").strip()))
    if not db_dir or not os.path.isdir(db_dir):
        raise ImageKeyError("db_dir 无效，请先选择微信 db_storage 目录。", code="db_dir_invalid")

    attach_dir = derive_attach_dir(db_dir)
    if not attach_dir:
        raise ImageKeyError("未找到附件目录（attach）。请确认已选择正确的 db_storage。", code="attach_dir_not_found")

    xor_key = find_xor_key(attach_dir)
    ciphertext, _ct_file = find_v2_ciphertext(attach_dir)
    if ciphertext is None:
        raise ImageKeyError("未找到 V2 图片缓存。请先在微信里点开 2-3 张图片（大图）后重试。", code="no_v2_dat")

    pids = get_wechat_pids(process_name=process_name)
    if not pids:
        raise ImageKeyError("未检测到微信进程，请先启动微信并登录。", code="wechat_not_running")

    perm_errors = 0
    last_err: Exception | None = None
    for pid in pids[:3]:
        if stop_check and stop_check():
            raise ImageKeyCancelled()
        try:
            if progress:
                progress(f"扫描 PID {pid}…")
            aes_key = scan_memory_for_aes_key(pid, ciphertext, stop_check=stop_check, progress=progress)
            if aes_key:
                return aes_key, xor_key
        except ImageKeyCancelled:
            raise
        except ImageKeyError as e:
            last_err = e
            if e.code == "permission":
                perm_errors += 1
            continue
        except Exception as e:
            last_err = e
            continue

    if perm_errors >= max(1, min(3, len(pids))):
        raise ImageKeyError("权限不足：无法读取微信进程内存。请在 GUI 中点击“管理员运行”后重试。", code="permission")

    if last_err:
        raise ImageKeyError(f"提取失败：{last_err}", code="failed")
    raise ImageKeyError("未找到 AES key。请先在微信点开几张图片（大图），然后立即重试。", code="not_found")

