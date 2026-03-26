"""
微信实时消息监听器 - Web UI (SSE推送 + mtime检测)

http://localhost:5678
- 30ms轮询WAL/DB文件的mtime变化（WAL是预分配固定大小，不能用size检测）
- 检测到变化后：全量解密DB + 全量WAL patch
- SSE 服务器推送
"""
import hashlib, struct, os, sys, json, time, sqlite3, io, threading, queue, traceback
import hmac as hmac_mod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from Crypto.Cipher import AES
import urllib.parse
import glob as glob_mod
import zstandard as zstd
from decode_image import extract_md5_from_packed_info, decrypt_dat_file, is_v2_format
from decode_voice import decode_voice_bytes_to_wav
from key_utils import get_key_info, strip_key_metadata
from persona_store import PersonaStore
from chat_history import query_chat_history

_zstd_dctx = zstd.ZstdDecompressor()

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HEADER_SZ = 32
WAL_FRAME_HEADER_SZ = 24

from config import load_config
_cfg = load_config()
DB_DIR = _cfg["db_dir"]
KEYS_FILE = _cfg["keys_file"]
CONTACT_CACHE = os.path.join(_cfg["decrypted_dir"], "contact", "contact.db")
DECRYPTED_SESSION = os.path.join(_cfg["decrypted_dir"], "session", "session.db")
DECODED_IMAGE_DIR = _cfg.get("decoded_image_dir", os.path.join(os.path.dirname(os.path.abspath(__file__)), "decoded_images"))
DECODED_VOICE_DIR = _cfg.get("decoded_voice_dir", os.path.join(os.path.dirname(os.path.abspath(__file__)), "decoded_voices"))
PERSONA_DB = _cfg.get("persona_db") or os.path.join(os.path.dirname(KEYS_FILE), "persona.db")
if not os.path.isabs(PERSONA_DB):
    PERSONA_DB = os.path.join(os.path.dirname(KEYS_FILE), PERSONA_DB)
MONITOR_CACHE_DIR = os.path.join(_cfg["decrypted_dir"], "_monitor_cache")
WECHAT_BASE_DIR = _cfg.get("wechat_base_dir", "")
IMAGE_AES_KEY = _cfg.get("image_aes_key")  # V2 格式 AES key (从微信内存提取)
IMAGE_XOR_KEY = _cfg.get("image_xor_key", 0x88)  # XOR key

POLL_MS = 30  # 高频轮询WAL/DB的mtime，30ms一次
LISTEN_HOST = _cfg.get("listen_host", "127.0.0.1") or "127.0.0.1"
try:
    PORT = int(_cfg.get("listen_port", 5678) or 5678)
except Exception:
    PORT = 5678
OPEN_BROWSER = bool(_cfg.get("open_browser", True))
API_TOKEN = str(_cfg.get("api_token") or "").strip()
CONFIG_SELF_USERNAME = str(_cfg.get("self_username") or "").strip()

sse_clients = []
sse_lock = threading.Lock()
messages_log = []
messages_lock = threading.Lock()
MAX_LOG = 500
MESSAGE_SEQ = 0
_img_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix='img')
_hidden_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hidden')
_detail_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='detail')

# ---- Emoji 缓存 (md5 → {cdn_url, aes_key, encrypt_url}) ----
_emoji_lookup = {}       # md5 → dict
_emoji_lookup_lock = threading.Lock()

_emoji_keys_dict = None  # 保存 keys 引用供刷新用
_emoji_last_refresh = 0

def _build_emoji_lookup(keys_dict):
    """从 emoticon.db 构建 emoji md5 → URL 映射（直接解密，不走 cache）"""
    global _emoji_lookup, _emoji_keys_dict, _emoji_last_refresh
    _emoji_keys_dict = keys_dict
    key_info = get_key_info(keys_dict, os.path.join("emoticon", "emoticon.db"))
    if not key_info:
        print("[emoji] 无 emoticon.db key，跳过", flush=True)
        return

    src = os.path.join(DB_DIR, "emoticon", "emoticon.db")
    if not os.path.exists(src):
        return

    import tempfile
    dst = os.path.join(tempfile.gettempdir(), "wechat_emoticon_dec.db")
    enc_key = bytes.fromhex(key_info["enc_key"])

    try:
        full_decrypt(src, dst, enc_key)
        wal = src + "-wal"
        if os.path.exists(wal):
            decrypt_wal_full(wal, dst, enc_key)
    except Exception as e:
        print(f"[emoji] emoticon.db 解密失败: {e}", flush=True)
        return

    try:
        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        new_lookup = {}

        # 1. NonStore 表情（有独立 cdn_url）
        rows = conn.execute(
            "SELECT md5, aes_key, cdn_url, encrypt_url, product_id FROM kNonStoreEmoticonTable"
        ).fetchall()
        # 收集每个 package 的 cdn_url 模板
        pkg_cdn_template = {}  # package_id → cdn_url (任意一个)
        for md5, aes_key, cdn_url, encrypt_url, product_id in rows:
            if md5:
                new_lookup[md5] = {
                    'cdn_url': cdn_url or '',
                    'aes_key': aes_key or '',
                    'encrypt_url': encrypt_url or '',
                }
            if product_id and cdn_url:
                pkg_cdn_template[product_id] = cdn_url

        non_store_count = len(new_lookup)

        # 2. Store 表情（尝试构造 cdn_url）
        store_rows = conn.execute(
            "SELECT package_id_, md5_ FROM kStoreEmoticonFilesTable"
        ).fetchall()
        store_added = 0
        for pkg_id, md5 in store_rows:
            if md5 and md5 not in new_lookup:
                # 尝试用同 package 的模板构造 URL
                template = pkg_cdn_template.get(pkg_id, '')
                if template and '&' in template:
                    # 替换 m= 参数为新 md5
                    import re
                    constructed = re.sub(r'm=[0-9a-f]+', f'm={md5}', template)
                    new_lookup[md5] = {
                        'cdn_url': constructed,
                        'aes_key': '',
                        'encrypt_url': '',
                    }
                    store_added += 1

        conn.close()
        with _emoji_lookup_lock:
            _emoji_lookup = new_lookup
        _emoji_last_refresh = time.time()
        print(f"[emoji] 已加载 {non_store_count} NonStore + {store_added} Store = {len(new_lookup)} 个表情映射", flush=True)
    except Exception as e:
        print(f"[emoji] 构建映射失败: {e}", flush=True)
    finally:
        try:
            os.unlink(dst)
        except OSError:
            pass

def _download_emoji(md5):
    """从 CDN 下载表情并缓存到 decoded_images/，返回文件名或 None"""
    with _emoji_lookup_lock:
        info = _emoji_lookup.get(md5)
    if not info:
        # Lookup miss: 刷新 emoticon.db（最多每60秒一次）
        if _emoji_keys_dict and time.time() - _emoji_last_refresh > 60:
            print(f"  [emoji] lookup miss, 刷新 emoticon.db...", flush=True)
            _build_emoji_lookup(_emoji_keys_dict)
            with _emoji_lookup_lock:
                info = _emoji_lookup.get(md5)
        if not info:
            return None

    # 先检查是否已缓存
    for ext in ('.gif', '.png', '.jpg', '.webp'):
        cached = os.path.join(DECODED_IMAGE_DIR, f"emoji_{md5}{ext}")
        if os.path.exists(cached):
            return f"emoji_{md5}{ext}"

    cdn_url = info.get('cdn_url', '')
    aes_key = info.get('aes_key', '')
    encrypt_url = info.get('encrypt_url', '')

    data = None
    # 方法1: 从 cdn_url 直接下载（未加密）
    if cdn_url:
        try:
            import urllib.request
            req = urllib.request.Request(cdn_url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=15)
            data = resp.read()
        except Exception as e:
            print(f"  [emoji] cdn下载失败 {md5[:12]}: {e}", flush=True)

    # 方法2: 从 encrypt_url 下载 + AES-CBC 解密
    if not data and encrypt_url and aes_key:
        try:
            import urllib.request
            req = urllib.request.Request(encrypt_url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=15)
            enc_data = resp.read()
            key_bytes = bytes.fromhex(aes_key)
            cipher = AES.new(key_bytes, AES.MODE_CBC, iv=key_bytes)
            data = cipher.decrypt(enc_data)
            # 去除 PKCS7 padding
            if data:
                pad = data[-1]
                if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
                    data = data[:-pad]
        except Exception as e:
            print(f"  [emoji] encrypt下载解密失败 {md5[:12]}: {e}", flush=True)

    if not data or len(data) < 4:
        return None

    # 检测格式
    if data[:3] == b'\xff\xd8\xff':
        ext = '.jpg'
    elif data[:4] == b'\x89PNG':
        ext = '.png'
    elif data[:3] == b'GIF':
        ext = '.gif'
    elif data[:4] == b'RIFF':
        ext = '.webp'
    elif data[:4] in (b'wxgf', b'wxam'):
        # WXGF/WXAM 需要转换
        ext = '.gif'
        tmp_path = os.path.join(DECODED_IMAGE_DIR, f"emoji_{md5}.wxgf")
        with open(tmp_path, 'wb') as f:
            f.write(data)
        jpg_path = _convert_hevc_to_jpeg(tmp_path, os.path.join(DECODED_IMAGE_DIR, f"emoji_{md5}.jpg"))
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        if jpg_path:
            return f"emoji_{md5}.jpg"
        return None
    else:
        ext = '.bin'

    out_name = f"emoji_{md5}{ext}"
    out_path = os.path.join(DECODED_IMAGE_DIR, out_name)
    with open(out_path, 'wb') as f:
        f.write(data)
    print(f"  [emoji] 下载缓存: {out_name} ({len(data)//1024}KB)", flush=True)
    return out_name


class MonitorDBCache:
    """轻量 DB 缓存，mtime 检测变化时重新解密（线程安全）"""

    def __init__(self, keys, tmp_dir):
        self.keys = keys
        self.tmp_dir = tmp_dir
        os.makedirs(tmp_dir, exist_ok=True)
        self._state = {}  # rel_key → (db_mtime, wal_mtime)
        self._locks = {}  # per-key 锁，防止并发解密同一 DB
        self._meta_lock = threading.Lock()

    def _get_lock(self, rel_key):
        with self._meta_lock:
            if rel_key not in self._locks:
                self._locks[rel_key] = threading.Lock()
            return self._locks[rel_key]

    def invalidate(self, rel_key):
        """强制清除缓存状态，下次 get() 会重新全量解密"""
        lock = self._get_lock(rel_key)
        with lock:
            self._state.pop(rel_key, None)

    def get(self, rel_key):
        """返回解密后的临时文件路径，mtime 变化时自动重新解密"""
        key_info = get_key_info(self.keys, rel_key)
        if not key_info:
            return None

        lock = self._get_lock(rel_key)
        with lock:
            enc_key = bytes.fromhex(key_info["enc_key"])
            rel_path = rel_key.replace('\\', '/').replace('/', os.sep)
            db_path = os.path.join(DB_DIR, rel_path)
            wal_path = db_path + "-wal"

            if not os.path.exists(db_path):
                return None

            try:
                db_mtime = os.path.getmtime(db_path)
                wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
            except OSError:
                return None

            out_name = rel_key.replace('\\', '_').replace('/', '_')
            out_path = os.path.join(self.tmp_dir, out_name)

            prev = self._state.get(rel_key)

            if prev is None or db_mtime != prev[0]:
                t0 = time.perf_counter()
                for _retry in range(3):
                    try:
                        full_decrypt(db_path, out_path, enc_key)
                        break
                    except PermissionError:
                        if _retry < 2:
                            time.sleep(1)
                        else:
                            raise
                if os.path.exists(wal_path):
                    decrypt_wal_full(wal_path, out_path, enc_key)
                ms = (time.perf_counter() - t0) * 1000
                print(f"  [cache] {rel_key} 全量解密 {ms:.0f}ms", flush=True)
                self._state[rel_key] = (db_mtime, wal_mtime)
            elif wal_mtime != prev[1]:
                t0 = time.perf_counter()
                decrypt_wal_full(wal_path, out_path, enc_key)
                ms = (time.perf_counter() - t0) * 1000
                print(f"  [cache] {rel_key} WAL patch {ms:.0f}ms", flush=True)
                self._state[rel_key] = (db_mtime, wal_mtime)

            return out_path


def build_username_db_map(db_cache, keys):
    """从 message_N.db 的 Name2Id 表构建 username → [db_keys] 映射

    同一个 username 可能存在于多个 message_N.db 中，按 DB 文件修改时间倒序排列（最新的排前面）。
    通过 db_cache 动态解密 message_N.db，不依赖已全量解密的 decrypted/message 目录。
    """
    import re

    message_dir = os.path.join(DB_DIR, "message")
    msg_rel_keys = []
    if os.path.isdir(message_dir):
        for p in glob_mod.glob(os.path.join(message_dir, "message_*.db")):
            base = os.path.basename(p)
            # 仅匹配 message_<num>.db，排除 message_resource.db / message_fts_*.db 等
            if re.fullmatch(r"message_\d+\.db", base):
                msg_rel_keys.append(os.path.join("message", base))

    msg_rel_keys = sorted(set(msg_rel_keys))

    # 获取每个 DB 的 mtime 用于排序
    db_mtimes = {}
    for rel_key in msg_rel_keys:
        db_path = os.path.join(DB_DIR, rel_key.replace("\\", os.sep).replace("/", os.sep))
        try:
            db_mtimes[rel_key] = os.path.getmtime(db_path)
        except OSError:
            db_mtimes[rel_key] = 0

    mapping = {}  # username → [db_keys], 最新的在前
    for rel_key in msg_rel_keys:
        if not get_key_info(keys, rel_key):
            continue
        db_path = db_cache.get(rel_key)
        if not db_path:
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            for (user_name,) in conn.execute("SELECT user_name FROM Name2Id").fetchall():
                if not user_name:
                    continue
                mapping.setdefault(user_name, []).append(rel_key)
            conn.close()
        except Exception as e:
            print(f"  [WARN] Name2Id {rel_key}: {e}", flush=True)

    # 对每个 username 的 db_keys 按 mtime 倒序（最新的优先）
    for username in mapping:
        mapping[username].sort(key=lambda k: db_mtimes.get(k, 0), reverse=True)

    return mapping


def decrypt_page(enc_key, page_data, pgno):
    """解密单个加密页面"""
    iv = page_data[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        encrypted = page_data[SALT_SZ: PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return bytearray(SQLITE_HDR + decrypted + b'\x00' * RESERVE_SZ)
    else:
        encrypted = page_data[:PAGE_SZ - RESERVE_SZ]
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted)
        return decrypted + b'\x00' * RESERVE_SZ


def full_decrypt(db_path, out_path, enc_key):
    """首次全量解密"""
    t0 = time.perf_counter()
    file_size = os.path.getsize(db_path)
    total_pages = file_size // PAGE_SZ

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(db_path, 'rb') as fin, open(out_path, 'wb') as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                if len(page) > 0:
                    page = page + b'\x00' * (PAGE_SZ - len(page))
                else:
                    break
            fout.write(decrypt_page(enc_key, page, pgno))

    ms = (time.perf_counter() - t0) * 1000
    return total_pages, ms


def decrypt_wal_full(wal_path, out_path, enc_key):
    """解密WAL当前有效frame，patch到已解密的DB副本

    WAL是预分配固定大小(4MB)，包含当前有效frame和上一轮遗留的旧frame。
    通过WAL header中的salt值区分：只有frame header的salt匹配WAL header的才是有效frame。

    返回: (patched_pages, elapsed_ms)
    """
    t0 = time.perf_counter()

    if not os.path.exists(wal_path):
        return 0, 0

    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        return 0, 0

    frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ  # 24 + 4096 = 4120
    patched = 0

    with open(wal_path, 'rb') as wf, open(out_path, 'r+b') as df:
        # 读WAL header，获取当前salt值
        wal_hdr = wf.read(WAL_HEADER_SZ)
        wal_salt1 = struct.unpack('>I', wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack('>I', wal_hdr[20:24])[0]

        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            pgno = struct.unpack('>I', fh[0:4])[0]
            frame_salt1 = struct.unpack('>I', fh[8:12])[0]
            frame_salt2 = struct.unpack('>I', fh[12:16])[0]

            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break

            # 校验: pgno有效 且 salt匹配当前WAL周期
            if pgno == 0 or pgno > 1000000:
                continue
            if frame_salt1 != wal_salt1 or frame_salt2 != wal_salt2:
                continue  # 旧周期遗留的frame，跳过

            dec = decrypt_page(enc_key, ep, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(dec)
            patched += 1

    ms = (time.perf_counter() - t0) * 1000
    return patched, ms


def load_contact_names():
    names = {}
    try:
        conn = sqlite3.connect(CONTACT_CACHE)
        for r in conn.execute("SELECT username, nick_name, remark FROM contact").fetchall():
            names[r[0]] = r[2] if r[2] else r[1] if r[1] else r[0]
        conn.close()
    except:
        pass
    return names


def load_contacts(db_cache=None, keys=None):
    """加载联系人（优先通过 db_cache 动态解密 contact/contact.db）。

    Returns:
        (names_map, full_list)
    """
    names = {}
    full = []

    db_path = None
    try:
        rel_key = os.path.join("contact", "contact.db")
        if db_cache and keys and get_key_info(keys, rel_key):
            db_path = db_cache.get(rel_key)
    except Exception:
        db_path = None

    # fallback：如果用户曾运行 decrypt_db.py，直接读已解密的副本
    if not db_path and os.path.exists(CONTACT_CACHE):
        db_path = CONTACT_CACHE

    if not db_path or not os.path.exists(db_path):
        return names, full

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute("SELECT username, nick_name, remark FROM contact").fetchall()
        conn.close()
        for username, nick_name, remark in rows:
            display = remark if remark else nick_name if nick_name else username
            names[username] = display
            full.append({
                "username": username,
                "nick_name": nick_name or "",
                "remark": remark or "",
                "display_name": display,
                "is_group": ("@chatroom" in (username or "")),
            })
    except Exception:
        return {}, []

    return names, full


def detect_self_username(cfg_value: str, wechat_base_dir: str, contact_names: dict, db_cache=None, keys=None) -> str:
    """尽力识别当前账号 username（用于判断消息方向）。

    优先级：
    1) config.json 的 self_username
    2) wechat_base_dir 目录名（或去掉末尾 _xxxx 后缀）
    3) message_0.db 的 Name2Id 中与目录名前缀匹配的用户名
    """
    cfg_value = (cfg_value or "").strip()
    if cfg_value:
        return cfg_value

    base = os.path.basename(wechat_base_dir or "").strip()
    candidates = []
    if base:
        candidates.append(base)
        if base.startswith("wxid_") and "_" in base:
            candidates.append(base.rsplit("_", 1)[0])

    names = contact_names or {}
    for c in candidates:
        if c and c in names:
            return c

    users = set()
    try:
        rel = os.path.join("message", "message_0.db")
        if db_cache and keys and get_key_info(keys, rel):
            p = db_cache.get(rel)
            if p and os.path.exists(p):
                conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                rows = conn.execute("SELECT user_name FROM Name2Id").fetchall()
                conn.close()
                users = {u for (u,) in rows if u}
    except Exception:
        users = set()

    for c in candidates:
        if c and c in users:
            return c

    if base and users:
        prefix = [u for u in users if u and "@chatroom" not in u and base.startswith(u)]
        if len(prefix) == 1:
            return prefix[0]

    return ""


def format_msg_type(t):
    return {
        1: '文本', 3: '图片', 34: '语音', 42: '名片',
        43: '视频', 47: '表情', 48: '位置', 49: '链接/文件',
        50: '通话', 10000: '系统', 10002: '撤回',
    }.get(t, f'type={t}')


def msg_type_icon(t):
    return {
        1: '💬', 3: '🖼️', 34: '🎤', 42: '👤',
        43: '🎬', 47: '😀', 48: '📍', 49: '🔗',
        50: '📞', 10000: '⚙️', 10002: '↩️',
    }.get(t, '📨')


def _is_safe_msg_table_name(table_name):
    if not isinstance(table_name, str):
        return False
    if not table_name.startswith("Msg_") or len(table_name) != 36:
        return False
    tail = table_name[4:]
    return all(c in "0123456789abcdef" for c in tail)


def _decompress_message_content(content, ct_flag):
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


def _load_name2id_map(conn):
    """message DB: Name2Id(rowid -> user_name)"""
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return {}
    return {int(rid): (uname or "") for rid, uname in rows if uname}


def broadcast_sse(msg_data):
    event_type = msg_data.get('event', '')
    data_line = f"data: {json.dumps(msg_data, ensure_ascii=False)}\n"
    if event_type:
        payload = f"event: {event_type}\n{data_line}\n"
    else:
        payload = f"{data_line}\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(payload)
            except:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


def append_message(msg_data):
    global messages_log, MESSAGE_SEQ
    with messages_lock:
        MESSAGE_SEQ += 1
        msg_data["seq"] = MESSAGE_SEQ
        messages_log.append(msg_data)
        if len(messages_log) > MAX_LOG:
            messages_log = messages_log[-MAX_LOG:]
        return MESSAGE_SEQ


def _convert_hevc_to_jpeg(hevc_path, jpeg_path):
    """将 wxgf/HEVC 文件转为 JPEG

    wxgf 是微信自有格式: wxgf header + ICC profile + HEVC NAL units
    通过扫描 HEVC VPS start code (00 00 00 01 40 01) 定位 Annex B 流，
    再用 PyAV (ffmpeg) 解码首帧为 JPEG。
    """
    try:
        import av

        with open(hevc_path, 'rb') as f:
            data = f.read()

        # 扫描 HEVC Annex B VPS start code: 00 00 00 01 40 01
        vps_sig = b'\x00\x00\x00\x01\x40\x01'
        hevc_start = data.find(vps_sig)
        if hevc_start < 0:
            # fallback: 找 SPS (00 00 00 01 42 01)
            hevc_start = data.find(b'\x00\x00\x00\x01\x42\x01')
        if hevc_start < 0:
            print(f"  [img] wxgf 中未找到 HEVC VPS/SPS", flush=True)
            return None

        # 提取 HEVC Annex B 流并用 PyAV 解码
        h265_path = hevc_path + '.h265'
        with open(h265_path, 'wb') as f:
            f.write(data[hevc_start:])

        try:
            container = av.open(h265_path, format='hevc')
            for frame in container.decode(video=0):
                img = frame.to_image()
                img.save(jpeg_path, "JPEG", quality=90)
                container.close()
                return jpeg_path
            container.close()
        finally:
            if os.path.exists(h265_path):
                os.unlink(h265_path)

    except ImportError:
        print(f"  [img] 需要 PyAV: pip install av", flush=True)
    except Exception as e:
        print(f"  [img] HEVC→JPEG 失败: {e}", flush=True)
    return None


# ============ 监听器 ============

class SessionMonitor:
    def __init__(self, enc_key, session_db, contact_names, db_cache=None, username_db_map=None):
        self.enc_key = enc_key
        self.session_db = session_db
        self.wal_path = session_db + "-wal"
        self.contact_names = contact_names
        self.db_cache = db_cache
        self.username_db_map = username_db_map or {}
        self.prev_state = {}
        self.decrypt_ms = 0
        self.patched_pages = 0
        # 已显示消息去重: {(username, timestamp, base_msg_type), ...}
        self._shown_keys = set()

    def resolve_image(self, username, timestamp):
        """解密图片: username+timestamp → 解密后的图片文件名，失败返回 None"""
        if not self.db_cache or not self.username_db_map:
            return None

        # 1. 找到 username 对应的所有 message_N.db（按 mtime 倒序）
        db_keys = self.username_db_map.get(username)
        if not db_keys:
            return None

        # 2. 遍历候选 DB，找到包含该 timestamp 消息的那个
        table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        local_id = None
        for db_key in db_keys:
            for _try in range(2):
                msg_db_path = self.db_cache.get(db_key)
                if not msg_db_path:
                    break
                try:
                    conn = sqlite3.connect(f"file:{msg_db_path}?mode=ro", uri=True)
                    # 微信4.0 图片的 local_type 可能是复合编码: (sub<<32)|3
                    row = conn.execute(f"""
                        SELECT local_id FROM [{table_name}]
                        WHERE (local_type = 3 OR (local_type > 4294967296 AND local_type % 4294967296 = 3))
                        AND create_time = ?
                    """, (timestamp,)).fetchone()
                    if not row:
                        row = conn.execute(f"""
                            SELECT local_id FROM [{table_name}]
                            WHERE (local_type = 3 OR (local_type > 4294967296 AND local_type % 4294967296 = 3))
                            AND ABS(create_time - ?) <= 3
                            ORDER BY ABS(create_time - ?) LIMIT 1
                        """, (timestamp, timestamp)).fetchone()
                    conn.close()
                    if row:
                        local_id = row[0]
                    break
                except Exception as e:
                    if 'malformed' in str(e) and _try == 0:
                        print(f"  [img] {db_key} malformed, 强制刷新...", flush=True)
                        self.db_cache.invalidate(db_key)
                        continue
                    if 'no such table' not in str(e):
                        print(f"  [img] 查询 {db_key}/{table_name} 失败: {e}", flush=True)
                    break
            if local_id:
                break

        if not local_id:
            print(f"  [img] 未找到 local_id: {username} t={timestamp}", flush=True)
            return None

        # 4. 查 message_resource.db 获取 MD5
        #    local_id 不全局唯一，需要同时匹配 create_time
        file_md5 = None
        for _try in range(2):
            res_path = self.db_cache.get(os.path.join("message", "message_resource.db"))
            if not res_path:
                return None
            try:
                conn = sqlite3.connect(f"file:{res_path}?mode=ro", uri=True)
                row = conn.execute(
                    "SELECT packed_info FROM MessageResourceInfo "
                    "WHERE message_local_id = ? AND message_create_time = ? "
                    "AND (message_local_type = 3 OR message_local_type % 4294967296 = 3)",
                    (local_id, timestamp)
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT packed_info FROM MessageResourceInfo "
                        "WHERE message_create_time = ? "
                        "AND (message_local_type = 3 OR message_local_type % 4294967296 = 3)",
                        (timestamp,)
                    ).fetchone()
                conn.close()
                if row and row[0]:
                    file_md5 = extract_md5_from_packed_info(row[0])
                break
            except Exception as e:
                if 'malformed' in str(e) and _try == 0:
                    print(f"  [img] resource DB malformed, 强制刷新...", flush=True)
                    self.db_cache.invalidate(os.path.join("message", "message_resource.db"))
                    continue
                print(f"  [img] 查询 message_resource 失败: {e}", flush=True)
                return None

        if not file_md5:
            print(f"  [img] 未找到 MD5: local_id={local_id} t={timestamp}", flush=True)
            return None

        # 5. 查找 .dat 文件
        attach_dir = os.path.join(WECHAT_BASE_DIR, "msg", "attach")
        username_hash = hashlib.md5(username.encode()).hexdigest()
        search_base = os.path.join(attach_dir, username_hash)

        if not os.path.isdir(search_base):
            print(f"  [img] attach 目录不存在: {search_base}", flush=True)
            return None

        pattern = os.path.join(search_base, "*", "Img", f"{file_md5}*.dat")
        dat_files = sorted(glob_mod.glob(pattern))
        if not dat_files:
            print(f"  [img] 未找到 .dat: MD5={file_md5}", flush=True)
            return None

        # 分类 .dat 文件
        # 优先级: 原图.dat(最大) > _h.dat > _W.dat > _t.dat(缩略图)
        ranked = []
        for f in dat_files:
            fname = os.path.basename(f).lower()
            sz = os.path.getsize(f)
            if '_t_' in fname:
                rank = 5  # _t_W.dat 缩略图变体
            elif '_t.' in fname:
                rank = 4  # _t.dat 缩略图
            elif '_w.' in fname:
                rank = 2  # _W.dat (V2 可转 JPEG)
            elif '_h.' in fname:
                rank = 1  # 高清
            elif fname == f"{file_md5}.dat".lower():
                rank = 0  # 原图 (最优先)
            else:
                rank = 0
            ranked.append((rank, sz, f))
        ranked.sort(key=lambda x: (x[0], -x[1]))

        # 6. 解密图片
        os.makedirs(DECODED_IMAGE_DIR, exist_ok=True)
        out_base = os.path.join(DECODED_IMAGE_DIR, file_md5)
        rank_names = {0: 'orig', 1: 'h', 2: 'W', 4: 't', 5: 't_W'}
        browser_formats = ('jpg', 'png', 'gif', 'webp')

        # 已有可用缓存则跳过
        for ext in browser_formats:
            candidate = f"{out_base}.{ext}"
            if os.path.exists(candidate):
                cached_sz = os.path.getsize(candidate)
                best_rank = ranked[0][0] if ranked else 99
                if cached_sz > 20480 or best_rank >= 4:
                    return os.path.basename(candidate)
                os.unlink(candidate)
                print(f"  [img] 缩略图升级: {cached_sz/1024:.0f}KB → 重解密", flush=True)
                break

        for rank, sz, selected in ranked:
            sel_type = rank_names.get(rank, '?')
            print(f"  [img] 尝试 {sel_type}({sz/1024:.0f}KB): {os.path.basename(selected)}", flush=True)

            if is_v2_format(selected) and not IMAGE_AES_KEY:
                print(f"  [img] V2 格式缺少 AES key, 跳过", flush=True)
                continue

            result_path, fmt = decrypt_dat_file(selected, f"{out_base}.tmp", IMAGE_AES_KEY, IMAGE_XOR_KEY)
            if not result_path:
                print(f"  [img] 解密失败, 跳过", flush=True)
                continue

            # HEVC/wxgf → 用 pillow-heif 转 JPEG
            if fmt in ('hevc', 'bin'):
                jpg_path = _convert_hevc_to_jpeg(result_path, f"{out_base}.jpg")
                os.unlink(result_path)
                if jpg_path:
                    size_kb = os.path.getsize(jpg_path) / 1024
                    print(f"  [img] HEVC→JPEG 成功: {os.path.basename(jpg_path)} ({size_kb:.0f}KB)", flush=True)
                    return os.path.basename(jpg_path)
                print(f"  [img] HEVC→JPEG 转换失败, 尝试下一个", flush=True)
                continue

            final = f"{out_base}.{fmt}"
            if os.path.exists(final):
                os.unlink(final)
            os.rename(result_path, final)
            size_kb = os.path.getsize(final) / 1024
            print(f"  [img] 解密成功: {os.path.basename(final)} ({size_kb:.0f}KB)", flush=True)
            return os.path.basename(final)

        print(f"  [img] 所有 .dat 均无法解密", flush=True)
        return '__v2_unsupported__'

    def resolve_voice(self, username, timestamp):
        """解码语音: username+timestamp → 解码后的 wav 文件名，失败返回 None"""
        if not self.db_cache or not self.username_db_map:
            return None

        db_keys = self.username_db_map.get(username)
        if not db_keys:
            return None

        table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        local_id = None
        msg_ctime = None
        for db_key in db_keys:
            for _try in range(2):
                msg_db_path = self.db_cache.get(db_key)
                if not msg_db_path:
                    break
                try:
                    conn = sqlite3.connect(f"file:{msg_db_path}?mode=ro", uri=True)
                    row = conn.execute(f"""
                        SELECT local_id, create_time FROM [{table_name}]
                        WHERE (local_type = 34 OR (local_type > 4294967296 AND local_type % 4294967296 = 34))
                        AND create_time = ?
                        ORDER BY local_id DESC LIMIT 1
                    """, (timestamp,)).fetchone()
                    if not row:
                        row = conn.execute(f"""
                            SELECT local_id, create_time FROM [{table_name}]
                            WHERE (local_type = 34 OR (local_type > 4294967296 AND local_type % 4294967296 = 34))
                            AND ABS(create_time - ?) <= 3
                            ORDER BY ABS(create_time - ?) LIMIT 1
                        """, (timestamp, timestamp)).fetchone()
                    conn.close()
                    if row:
                        local_id, msg_ctime = row
                    break
                except Exception as e:
                    if 'malformed' in str(e) and _try == 0:
                        print(f"  [voice] {db_key} malformed, 强制刷新...", flush=True)
                        self.db_cache.invalidate(db_key)
                        continue
                    if 'no such table' not in str(e):
                        print(f"  [voice] 查询 {db_key}/{table_name} 失败: {e}", flush=True)
                    break
            if local_id:
                break

        if not local_id:
            print(f"  [voice] 未找到 local_id: {username} t={timestamp}", flush=True)
            return None

        media_rel = os.path.join("message", "media_0.db")
        media_path = self.db_cache.get(media_rel)
        if not media_path:
            print(f"  [voice] media_0.db 解密失败/不存在", flush=True)
            return None

        try:
            conn = sqlite3.connect(f"file:{media_path}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT v.create_time, v.data_index, v.voice_data "
                "FROM VoiceInfo v "
                "JOIN Name2Id n ON n.rowid = v.chat_name_id "
                "WHERE n.user_name = ? AND v.local_id = ? AND ABS(v.create_time - ?) <= 3 "
                "ORDER BY v.create_time DESC",
                (username, local_id, msg_ctime or timestamp),
            ).fetchall()
            conn.close()
        except Exception as e:
            print(f"  [voice] 查询 media_0.db 失败: {e}", flush=True)
            return None

        if not rows:
            print(f"  [voice] 未找到语音数据: local_id={local_id} t={msg_ctime or timestamp}", flush=True)
            return None

        target_ctime = rows[0][0]
        parts = [(idx, blob) for ct, idx, blob in rows if ct == target_ctime and blob]
        if not parts:
            return None

        def _idx_key(v):
            try:
                return int(v)
            except Exception:
                return 0

        parts.sort(key=lambda x: _idx_key(x[0]))
        voice_blob = b"".join(p[1] for p in parts)
        if not voice_blob:
            return None

        os.makedirs(DECODED_VOICE_DIR, exist_ok=True)
        uname_hash = hashlib.md5(username.encode()).hexdigest()[:8]
        wav_name = f"voice_{uname_hash}_{target_ctime}_{local_id}.wav"
        wav_path = os.path.join(DECODED_VOICE_DIR, wav_name)

        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
            return wav_name

        tmp_path = wav_path + ".tmp"
        out, src_fmt = decode_voice_bytes_to_wav(voice_blob, tmp_path)
        if not out:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            print(f"  [voice] 解码失败: local_id={local_id} fmt={src_fmt}", flush=True)
            return None

        try:
            if os.path.exists(wav_path):
                os.unlink(wav_path)
            os.replace(out, wav_path)
        except OSError:
            pass

        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
            print(f"  [voice] 解码成功: {wav_name}", flush=True)
            return wav_name

        return None

    def _async_resolve_image(self, username, timestamp, msg_data):
        """后台线程: 解密图片并通过 SSE 推送更新"""
        delays = [0.3, 1.0, 2.0]
        for attempt in range(3):
            try:
                img_name = self.resolve_image(username, timestamp)
                if img_name == '__v2_unsupported__':
                    msg_data['content'] = '[图片 - 新加密格式暂不支持预览]'
                    broadcast_sse({
                        'event': 'image_update',
                        'timestamp': timestamp,
                        'username': username,
                        'v2_unsupported': True,
                    })
                    return
                elif img_name:
                    image_url = f'/img/{img_name}'
                    msg_data['image_url'] = image_url
                    broadcast_sse({
                        'event': 'image_update',
                        'timestamp': timestamp,
                        'username': username,
                        'image_url': image_url,
                    })
                    print(f"  [img] 异步解密成功: {img_name}", flush=True)
                    return
                elif attempt < 2:
                    time.sleep(delays[attempt])
            except Exception as e:
                print(f"  [img] 异步解密失败(attempt={attempt}): {e}", flush=True)
                if attempt < 2:
                    time.sleep(delays[attempt])

    def _fresh_decrypt_query(self, db_key, table_name, prev_ts, curr_ts):
        """独立解密 message DB 到临时文件并查询，避免共享缓存竞态"""
        key_info = get_key_info(self.db_cache.keys, db_key)
        if not key_info:
            return []
        enc_key = bytes.fromhex(key_info["enc_key"])
        rel_path = db_key.replace('\\', '/').replace('/', os.sep)
        db_path = os.path.join(DB_DIR, rel_path)
        wal_path = db_path + "-wal"
        if not os.path.exists(db_path):
            return []

        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            t0 = time.perf_counter()
            full_decrypt(db_path, tmp_path, enc_key)
            if os.path.exists(wal_path):
                decrypt_wal_full(wal_path, tmp_path, enc_key)
            ms = (time.perf_counter() - t0) * 1000
            print(f"  [hidden] {db_key} 独立解密 {ms:.0f}ms", flush=True)

            conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
            rows = conn.execute(f"""
                SELECT create_time, local_type, message_content, WCDB_CT_message_content
                FROM [{table_name}]
                WHERE create_time >= ? AND create_time <= ?
                ORDER BY create_time ASC
            """, (prev_ts, curr_ts)).fetchall()
            conn.close()
            return rows
        except Exception as e:
            print(f"  [hidden] {db_key} 独立解密失败: {e}", flush=True)
            return []
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _check_hidden_messages(self, username, prev_ts, curr_ts, curr_msg_type, display, is_group, sender):
        """检查时间窗口内是否有被 session 摘要覆盖的消息（文字、图片、表情等）

        先用共享缓存查询（快），失败或可疑时用独立解密（慢但可靠）。
        """
        if not self.username_db_map:
            return
        db_keys = self.username_db_map.get(username)
        if not db_keys:
            return

        table_name = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        print(f"  [hidden] 检查 {display[:15]} prev_ts={prev_ts} curr_ts={curr_ts} type={curr_msg_type}", flush=True)

        # 等待 message DB 写入完成
        time.sleep(1.0)

        # 快速路径: 用共享缓存查询（带重试）
        all_rows = []
        cache_failed = False
        for _try in range(3):
            all_rows.clear()
            if self.db_cache:
                for db_key in db_keys:
                    dec_path = self.db_cache.get(db_key)
                    if not dec_path:
                        continue
                    try:
                        conn = sqlite3.connect(f"file:{dec_path}?mode=ro", uri=True)
                        rows = conn.execute(f"""
                            SELECT create_time, local_type, message_content, WCDB_CT_message_content
                            FROM [{table_name}]
                            WHERE create_time >= ? AND create_time <= ?
                            ORDER BY create_time ASC
                        """, (prev_ts, curr_ts)).fetchall()
                        conn.close()
                        all_rows.extend(rows)
                    except Exception as e:
                        print(f"  [hidden] 缓存查询失败 {db_key}: {e}", flush=True)
                        cache_failed = True
                        break
            # 检查是否找到了 curr_ts 的消息（说明缓存是最新的）
            has_curr = any(r[0] == curr_ts for r in all_rows)
            if has_curr or cache_failed:
                break
            # 缓存可能还没更新到最新数据，短暂等待后重试
            if _try < 2:
                time.sleep(1.5)
                print(f"  [hidden] 缓存未包含最新消息，重试({_try+1})...", flush=True)

        # 仅在缓存查询出错时才用昂贵的独立解密
        if cache_failed:
            print(f"  [hidden] 缓存异常，启动独立解密...", flush=True)
            all_rows = []
            for db_key in db_keys:
                rows = self._fresh_decrypt_query(db_key, table_name, prev_ts, curr_ts)
                all_rows.extend(rows)
                if rows:
                    break
        else:
            print(f"  [hidden] 缓存查到 {len(all_rows)} 条", flush=True)

        # 过滤出隐藏消息
        hidden_msgs = []
        for ts, lt, mc, ct in all_rows:
            base = lt % 4294967296 if lt > 4294967296 else lt
            # 跳过已显示的消息（精确匹配 username+timestamp+type）
            if (username, ts, base) in self._shown_keys:
                continue
            # 解压 zstd
            if isinstance(mc, bytes) and ct == 4:
                try:
                    mc = _zstd_dctx.decompress(mc).decode('utf-8', errors='replace')
                except Exception:
                    mc = mc.decode('utf-8', errors='replace') if isinstance(mc, bytes) else ''
            elif isinstance(mc, bytes):
                mc = mc.decode('utf-8', errors='replace')
            hidden_msgs.append((ts, base, mc or ''))

        print(f"  [hidden] 找到 {len(hidden_msgs)} 条隐藏消息", flush=True)

        if not hidden_msgs:
            return

        global messages_log
        for ts, base, mc in hidden_msgs:
            self._shown_keys.add((username, ts, base))
            msg_data = {
                'time': datetime.fromtimestamp(ts).strftime('%H:%M:%S'),
                'timestamp': ts,
                'chat': display,
                'username': username,
                'is_group': is_group,
                'sender': sender,
            }
            if base == 3:
                # 隐藏的图片消息
                time.sleep(0.5)
                img_name = self.resolve_image(username, ts)
                if img_name and img_name != '__v2_unsupported__':
                    msg_data.update({
                        'type': '图片', 'type_icon': '\U0001f5bc\ufe0f',
                        'content': '', 'image_url': f'/img/{img_name}',
                    })
                    print(f"  [hidden] 补充图片: {img_name} t={ts}", flush=True)
                else:
                    continue
            elif base == 1:
                # 隐藏的文字消息
                msg_data.update({
                    'type': '文本', 'type_icon': '\U0001f4ac',
                    'content': mc,
                })
                print(f"  [hidden] 补充文字: {mc[:30]} t={ts}", flush=True)
            elif base == 47:
                # 隐藏的表情消息
                rich = self.resolve_rich_content(username, ts, 47)
                msg_data.update({
                    'type': '表情', 'type_icon': '\U0001f600',
                    'content': '[表情]',
                })
                if rich:
                    msg_data['rich_content'] = rich
                print(f"  [hidden] 补充表情 t={ts}", flush=True)
            elif base == 49:
                # 隐藏的富媒体消息
                rich = self.resolve_rich_content(username, ts, 49)
                msg_data.update({
                    'type': format_msg_type(base), 'type_icon': msg_type_icon(base),
                    'content': mc[:100] if mc else '',
                })
                if rich:
                    msg_data['rich_content'] = rich
                print(f"  [hidden] 补充富媒体 t={ts}", flush=True)
            else:
                # 其他类型
                msg_data.update({
                    'type': format_msg_type(base), 'type_icon': msg_type_icon(base),
                    'content': mc[:100] if mc else f'[{format_msg_type(base)}]',
                })
                print(f"  [hidden] 补充type={base} t={ts}", flush=True)

            msg_data["base_type"] = base
            append_message(msg_data)
            broadcast_sse(msg_data)

    def _query_msg_content(self, username, timestamp, base_type):
        """通用: 从 message_*.db 查找指定类型消息的 XML 内容

        base_type: 基础类型 (47, 49, 43, 34 等)
        微信4.0 的 local_type 是复合编码: (sub_type << 32) | base_type
        """
        db_keys = self.username_db_map.get(username, [])
        if not db_keys:
            return None

        tbl = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        for dk in db_keys:
            for _try in range(2):
                dec_path = self.db_cache.get(dk)
                if not dec_path:
                    break
                try:
                    conn = sqlite3.connect(f"file:{dec_path}?mode=ro", uri=True)
                    row = conn.execute(f'''
                        SELECT message_content, WCDB_CT_message_content, local_type
                        FROM "{tbl}"
                        WHERE (local_type = ? OR (local_type > 4294967296 AND local_type % 4294967296 = ?))
                        AND create_time BETWEEN ? AND ?
                        ORDER BY create_time DESC LIMIT 1
                    ''', (base_type, base_type, timestamp - 5, timestamp + 5)).fetchone()
                    conn.close()

                    if not row:
                        break  # 表存在但没找到匹配行，换下一个 DB
                    mc, ct_flag, full_type = row
                    if isinstance(mc, bytes) and ct_flag == 4:
                        mc = _zstd_dctx.decompress(mc).decode('utf-8', errors='replace')
                    elif isinstance(mc, bytes):
                        mc = mc.decode('utf-8', errors='replace')
                    if not mc:
                        break

                    xml_start = mc.find('<msg>')
                    if xml_start < 0:
                        xml_start = mc.find('<msg\n')
                    if xml_start < 0:
                        xml_start = mc.find('<?xml')
                    if xml_start > 0:
                        mc = mc[xml_start:]

                    return mc, full_type

                except Exception as e:
                    if 'malformed' in str(e) and _try == 0:
                        print(f"  [rich] {dk} malformed, 强制刷新...", flush=True)
                        self.db_cache.invalidate(dk)
                        continue
                    if 'no such table' not in str(e):
                        print(f"  [rich] 查询 {dk} 失败: {e}", flush=True)
                    break
        return None

    def _query_message_row(self, username, timestamp, base_type):
        """从 message_*.db 获取一条消息的原始内容与 local_id（用于补全文本详情）。"""
        db_keys = self.username_db_map.get(username, [])
        if not db_keys:
            return None

        tbl = f"Msg_{hashlib.md5(username.encode()).hexdigest()}"
        if not _is_safe_msg_table_name(tbl):
            return None

        for dk in db_keys:
            for _try in range(2):
                dec_path = self.db_cache.get(dk)
                if not dec_path:
                    break
                try:
                    conn = sqlite3.connect(f"file:{dec_path}?mode=ro", uri=True)
                    row = conn.execute(f"""
                        SELECT local_id, local_type, create_time, real_sender_id,
                               message_content, WCDB_CT_message_content
                        FROM [{tbl}]
                        WHERE (local_type = ? OR (local_type > 4294967296 AND local_type % 4294967296 = ?))
                        AND create_time BETWEEN ? AND ?
                        ORDER BY create_time DESC
                        LIMIT 1
                    """, (base_type, base_type, timestamp - 5, timestamp + 5)).fetchone()

                    if not row:
                        conn.close()
                        break

                    local_id, local_type, create_time, real_sender_id, content, ct = row
                    raw = _decompress_message_content(content, ct)

                    sender_username = ""
                    try:
                        if real_sender_id:
                            r = conn.execute("SELECT user_name FROM Name2Id WHERE rowid = ?", (real_sender_id,)).fetchone()
                            if r and r[0]:
                                sender_username = r[0]
                    except sqlite3.Error:
                        pass

                    conn.close()

                    sub_type = int(local_type >> 32) if local_type and local_type > 4294967296 else 0
                    return {
                        "local_id": local_id,
                        "local_type": local_type or 0,
                        "create_time": create_time or timestamp,
                        "real_sender_id": real_sender_id or 0,
                        "sender_username": sender_username or "",
                        "base_type": int(base_type or 0),
                        "sub_type": sub_type,
                        "raw": raw,
                    }
                except Exception as e:
                    if 'malformed' in str(e) and _try == 0:
                        print(f"  [detail] {dk} malformed, 强制刷新...", flush=True)
                        self.db_cache.invalidate(dk)
                        continue
                    if 'no such table' not in str(e):
                        print(f"  [detail] 查询 {dk} 失败: {e}", flush=True)
                    break
        return None

    def _async_enrich_text_detail(self, msg_data):
        """后台线程: 补全文本消息详情（local_id/raw/sender_username 等）并推送 SSE"""
        try:
            username = msg_data.get("username", "")
            ts = int(msg_data.get("timestamp", 0) or 0)
            base_type = int(msg_data.get("base_type", 0) or 0)
            if not username or not ts or base_type != 1:
                return

            detail = self._query_message_row(username, ts, base_type)
            if not detail:
                return

            raw = detail.get("raw", "")
            sender_username = detail.get("sender_username", "")
            sender_display = self.contact_names.get(sender_username, sender_username) if sender_username else ""

            with messages_lock:
                msg_data.update({
                    "detail_ready": True,
                    "local_id": detail.get("local_id", 0) or 0,
                    "local_type": detail.get("local_type", 0) or 0,
                    "sub_type": detail.get("sub_type", 0) or 0,
                    "real_sender_id": detail.get("real_sender_id", 0) or 0,
                    "sender_username": sender_username,
                    "sender_display_name": sender_display,
                    "raw": raw,
                })
                if raw:
                    msg_data["content"] = raw

            broadcast_sse({
                "event": "message_detail",
                "seq": msg_data.get("seq", 0) or 0,
                "username": username,
                "timestamp": ts,
                "detail": {
                    "local_id": msg_data.get("local_id", 0) or 0,
                    "sender_username": sender_username,
                    "sender_display_name": sender_display,
                    "text": raw,
                },
            })
        except Exception as e:
            print(f"  [detail] enrich failed: {e}", flush=True)

    def _parse_rich_content(self, username, timestamp, msg_type):
        """解析富媒体消息, 返回 dict 或 None"""
        import xml.etree.ElementTree as ET

        if msg_type == 47:
            # --- 表情 ---
            result = self._query_msg_content(username, timestamp, 47)
            if not result:
                print(f"  [emoji] 查询失败 user={username[:10]} ts={timestamp}", flush=True)
                return None
            mc, _ = result
            if '<emoji' not in mc:
                return None
            try:
                root = ET.fromstring(mc)
                emoji = root.find('.//emoji')
                if emoji is None:
                    return None
                md5 = emoji.get('md5', '')
                etype = emoji.get('type', '')
                # 优先用 XML 中的 URL
                url = emoji.get('thumburl') or emoji.get('externurl') or emoji.get('cdnurl') or ''
                url = url.replace('&amp;', '&')
                if url and url.startswith('http'):
                    print(f"  [emoji] XML有URL md5={md5[:12]} type={etype}", flush=True)
                    return {'type': 'emoji', 'emoji_url': url}
                # XML 无 URL → 从 emoticon.db 下载
                if md5:
                    with _emoji_lookup_lock:
                        in_lookup = md5 in _emoji_lookup
                        lookup_size = len(_emoji_lookup)
                    print(f"  [emoji] XML无URL md5={md5[:12]} type={etype} lookup={lookup_size} found={in_lookup}", flush=True)
                    img_name = _download_emoji(md5)
                    if img_name:
                        return {'type': 'emoji', 'emoji_url': f'/img/{img_name}'}
                    print(f"  [emoji] 下载失败 md5={md5[:12]}", flush=True)
                else:
                    print(f"  [emoji] 无md5 type={etype}", flush=True)
            except ET.ParseError:
                pass
            return None

        elif msg_type == 49:
            # --- 链接/文件/引用/公众号/小程序 ---
            result = self._query_msg_content(username, timestamp, 49)
            if not result:
                return None
            mc, full_type = result
            sub_type = full_type >> 32 if full_type > 4294967296 else 0
            if '<appmsg' not in mc:
                return None
            try:
                root = ET.fromstring(mc)
                appmsg = root.find('.//appmsg')
                if appmsg is None:
                    return None
                title = (appmsg.findtext('title') or '').strip()
                des = (appmsg.findtext('des') or '').strip()
                url = (appmsg.findtext('url') or '').strip().replace('&amp;', '&')
                app_type = int(appmsg.findtext('type') or sub_type or 0)

                if app_type == 57:
                    # 引用回复: title 是回复内容
                    ref = appmsg.find('.//refermsg')
                    ref_name = ref.findtext('displayname') if ref is not None else ''
                    ref_content = ref.findtext('content') if ref is not None else ''
                    if ref_content:
                        ref_content = ref_content.strip()[:100]
                    return {
                        'type': 'quote',
                        'title': title,
                        'ref_name': ref_name or '',
                        'ref_content': ref_content or '',
                    }
                elif app_type == 6:
                    # 文件
                    attach = appmsg.find('.//appattach')
                    size = int(attach.findtext('totallen') or 0) if attach is not None else 0
                    ext = (attach.findtext('fileext') or '') if attach is not None else ''
                    return {
                        'type': 'file',
                        'title': title,
                        'file_ext': ext,
                        'file_size': size,
                    }
                elif app_type == 5:
                    # 链接/文章 — 清理 tracking 参数
                    clean_url = url
                    if 'mp.weixin.qq.com' in url:
                        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
                        pu = urlparse(url)
                        params = parse_qs(pu.query, keep_blank_values=False)
                        # 只保留文章必要参数
                        keep = {k: v for k, v in params.items()
                                if k in ('__biz', 'mid', 'idx', 'sn', 'chksm')}
                        clean_url = urlunparse(pu._replace(
                            query=urlencode(keep, doseq=True), fragment=''))
                    source = (appmsg.findtext('sourcedisplayname') or '').strip()
                    return {
                        'type': 'link',
                        'title': title,
                        'des': des[:200] if des else '',
                        'url': clean_url,
                        'source': source,
                    }
                elif app_type == 33 or app_type == 36:
                    # 小程序
                    source = (appmsg.findtext('sourcedisplayname') or '').strip()
                    return {
                        'type': 'miniapp',
                        'title': title,
                        'source': source,
                        'url': url,
                    }
                elif app_type == 51:
                    # 视频号
                    return {
                        'type': 'channels',
                        'title': title or '视频号内容',
                    }
                elif app_type == 19:
                    # 聊天记录转发 — 解析 recorditem 获取消息列表
                    items = []
                    ri = appmsg.findtext('recorditem') or ''
                    if ri:
                        try:
                            ri_root = ET.fromstring(ri)
                            for di in ri_root.findall('.//dataitem'):
                                name = (di.findtext('sourcename') or '').strip()
                                desc = (di.findtext('datadesc') or '').strip()
                                if name and desc:
                                    items.append({'name': name, 'text': desc[:100]})
                                if len(items) >= 20:
                                    break
                        except ET.ParseError:
                            pass
                    return {
                        'type': 'chatlog',
                        'title': title,
                        'des': des[:200] if des else '',
                        'items': items,
                    }
                else:
                    # 其他子类型: 用 title 显示
                    if title:
                        return {
                            'type': 'link',
                            'title': title,
                            'des': des[:200] if des else '',
                            'url': url,
                        }
            except ET.ParseError:
                pass
            return None

        elif msg_type == 43:
            # --- 视频 ---
            result = self._query_msg_content(username, timestamp, 43)
            if not result:
                return None
            mc, _ = result
            try:
                root = ET.fromstring(mc)
                video = root.find('.//videomsg')
                if video is None:
                    return None
                length = int(video.get('playlength') or 0)
                return {
                    'type': 'video',
                    'duration': length,
                }
            except ET.ParseError:
                pass
            return None

        elif msg_type == 34:
            # --- 语音 ---
            result = self._query_msg_content(username, timestamp, 34)
            if not result:
                return None
            mc, _ = result
            try:
                root = ET.fromstring(mc)
                voice = root.find('.//voicemsg')
                if voice is None:
                    return None
                length_ms = int(voice.get('voicelength') or 0)
                info = {
                    'type': 'voice',
                    'duration': round(length_ms / 1000, 1),
                }
                try:
                    wav_name = self.resolve_voice(username, timestamp)
                    if wav_name:
                        info['voice_url'] = f'/voice/{wav_name}'
                except Exception as e:
                    print(f"  [voice] resolve failed: {e}", flush=True)
                return info
            except ET.ParseError:
                pass
            return None

        return None

    def _async_resolve_rich(self, username, timestamp, msg_type, msg_data):
        """后台线程: 解析富媒体内容并推送 SSE（带重试）"""
        delays = [0.5, 1.5, 3.0]
        for attempt in range(3):
            try:
                time.sleep(delays[attempt])
                info = self._parse_rich_content(username, timestamp, msg_type)
                if info:
                    msg_data['rich'] = info
                    broadcast_sse({
                        'event': 'rich_update',
                        'timestamp': timestamp,
                        'username': username,
                        'rich': info,
                    })
                    print(f"  [rich] {info['type']} 解析成功", flush=True)
                    return
            except Exception as e:
                print(f"  [rich] 解析失败: {e}", flush=True)
        print(f"  [rich] type={msg_type} 3次重试均失败: {username}", flush=True)

    def query_state(self):
        """查询已解密副本的session状态"""
        conn = sqlite3.connect(f"file:{DECRYPTED_SESSION}?mode=ro", uri=True)
        state = {}
        for r in conn.execute("""
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            FROM SessionTable WHERE last_timestamp > 0
        """).fetchall():
            state[r[0]] = {
                'unread': r[1], 'summary': r[2] or '', 'timestamp': r[3],
                'msg_type': r[4], 'sender': r[5] or '', 'sender_name': r[6] or '',
            }
        conn.close()
        return state

    def do_full_refresh(self):
        """全量解密DB + 全量WAL patch"""
        # 先解密主DB
        pages, ms = full_decrypt(self.session_db, DECRYPTED_SESSION, self.enc_key)
        total_ms = ms
        wal_patched = 0

        # 再patch所有WAL frames
        if os.path.exists(self.wal_path):
            wal_patched, ms2 = decrypt_wal_full(self.wal_path, DECRYPTED_SESSION, self.enc_key)
            total_ms += ms2

        self.decrypt_ms = total_ms
        self.patched_pages = pages + wal_patched
        return self.patched_pages

    def check_updates(self):
        global messages_log
        try:
            t0 = time.perf_counter()
            self.do_full_refresh()
            t1 = time.perf_counter()
            curr_state = self.query_state()
            t2 = time.perf_counter()
            print(f"  [perf] decrypt={self.patched_pages}页/{(t1-t0)*1000:.1f}ms, query={(t2-t1)*1000:.1f}ms", flush=True)
        except Exception as e:
            print(f"  [ERROR] check_updates: {e}", flush=True)
            return

        # 收集所有新消息，按时间排序后再推送
        new_msgs = []
        for username, curr in curr_state.items():
            prev = self.prev_state.get(username)
            # 检测: 时间戳变化 OR 同一秒内消息类型变化（文字+图片组合）
            is_new = prev and (curr['timestamp'] > prev['timestamp'] or
                               (curr['timestamp'] == prev['timestamp'] and curr['msg_type'] != prev.get('msg_type')))
            if is_new:
                display = self.contact_names.get(username, username)
                is_group = '@chatroom' in username
                sender = ''
                if is_group:
                    sender = self.contact_names.get(curr['sender'], curr['sender_name'] or curr['sender'])

                summary = curr['summary']
                if isinstance(summary, bytes):
                    try:
                        summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                    except Exception:
                        summary = '(压缩内容)'
                if summary and ':\n' in summary:
                    summary = summary.split(':\n', 1)[1]

                msg_data = {
                    'time': datetime.fromtimestamp(curr['timestamp']).strftime('%H:%M:%S'),
                    'timestamp': curr['timestamp'],
                    'chat': display,
                    'username': username,
                    'is_group': is_group,
                    'sender': sender,
                    'base_type': curr['msg_type'],
                    'type': format_msg_type(curr['msg_type']),
                    'type_icon': msg_type_icon(curr['msg_type']),
                    'content': summary,
                    'unread': curr['unread'],
                    'decrypt_ms': round(self.decrypt_ms, 1),
                    'pages': self.patched_pages,
                }

                new_msgs.append(msg_data)
                self._shown_keys.add((username, curr['timestamp'], curr['msg_type']))

                # 图片消息: 后台异步解密（不阻塞轮询）
                if curr['msg_type'] == 3:
                    _img_executor.submit(
                        self._async_resolve_image,
                        username, curr['timestamp'], msg_data
                    )

                # 富媒体消息: 后台解析内容
                if curr['msg_type'] in (47, 49, 43, 34):
                    _img_executor.submit(
                        self._async_resolve_rich,
                        username, curr['timestamp'], curr['msg_type'], msg_data
                    )

                # 检查时间窗口内是否有被 session 摘要覆盖的消息
                # (比如用户发了 图片+文字，session只记录最后一条)
                prev_ts = prev['timestamp'] if prev else curr['timestamp'] - 5
                _hidden_executor.submit(
                    self._check_hidden_messages,
                    username, prev_ts, curr['timestamp'], curr['msg_type'],
                    display, is_group, sender
                )

        # 按时间排序
        new_msgs.sort(key=lambda m: m['timestamp'])

        for msg in new_msgs:
            append_message(msg)
            broadcast_sse(msg)

            # 文本消息：补全 local_id/raw（供上层自动回复更精确取数）
            if msg.get("base_type") == 1:
                _detail_executor.submit(self._async_enrich_text_detail, msg)

            try:
                now = time.time()
                msg_age = now - msg['timestamp']
                tag = f"{self.patched_pages}pg/{self.decrypt_ms:.0f}ms"
                sender = msg['sender']
                now_str = datetime.fromtimestamp(now).strftime('%H:%M:%S')
                if sender:
                    print(f"[{msg['time']} 延迟={msg_age:.1f}s] [{msg['chat']}] {sender}: {msg['content']}  ({tag})", flush=True)
                else:
                    print(f"[{msg['time']} 延迟={msg_age:.1f}s] [{msg['chat']}] {msg['content']}  ({tag})", flush=True)
            except Exception:
                pass  # Windows CMD编码问题，不影响SSE推送

        self.prev_state = curr_state

        # 清理过期的去重 key（保留最近 5 分钟）
        cutoff = int(time.time()) - 300
        self._shown_keys = {k for k in self._shown_keys if k[1] > cutoff}

def monitor_thread(enc_key, session_db, contact_names, db_cache=None, username_db_map=None):
    mon = SessionMonitor(enc_key, session_db, contact_names, db_cache, username_db_map)
    wal_path = mon.wal_path

    # 初始全量解密
    pages, ms = full_decrypt(session_db, DECRYPTED_SESSION, enc_key)
    wal_patched = 0
    wal_ms = 0
    if os.path.exists(wal_path):
        wal_patched, wal_ms = decrypt_wal_full(wal_path, DECRYPTED_SESSION, enc_key)
        print(f"[init] DB {pages}页/{ms:.0f}ms + WAL {wal_patched}页/{wal_ms:.0f}ms", flush=True)
    else:
        print(f"[init] DB {pages}页/{ms:.0f}ms", flush=True)

    mon.prev_state = mon.query_state()
    print(f"[monitor] 跟踪 {len(mon.prev_state)} 个会话", flush=True)
    print(f"[monitor] mtime轮询模式 (每{POLL_MS}ms)", flush=True)

    # mtime-based 轮询: WAL是预分配固定大小，不能用size检测
    poll_interval = POLL_MS / 1000
    prev_wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
    prev_db_mtime = os.path.getmtime(session_db)

    while True:
        time.sleep(poll_interval)
        try:
            # 用mtime检测WAL和DB变化
            try:
                wal_mtime = os.path.getmtime(wal_path) if os.path.exists(wal_path) else 0
                db_mtime = os.path.getmtime(session_db)
            except OSError:
                continue

            if wal_mtime == prev_wal_mtime and db_mtime == prev_db_mtime:
                continue  # 无变化

            t_detect = time.perf_counter()
            wal_changed = wal_mtime != prev_wal_mtime
            db_changed = db_mtime != prev_db_mtime

            mon.check_updates()

            t_done = time.perf_counter()
            try:
                detect_str = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                print(f"  [{detect_str}] WAL={'变' if wal_changed else '-'} DB={'变' if db_changed else '-'} 总耗时={(t_done-t_detect)*1000:.1f}ms", flush=True)
            except Exception:
                pass

            prev_wal_mtime = wal_mtime
            prev_db_mtime = db_mtime

        except Exception as e:
            print(f"[poll] 错误: {e}", flush=True)
            time.sleep(1)


# ============ Web ============

HTML_PAGE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>微信消息监听</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0a0a0f;color:#e0e0e0;height:100vh;display:flex;flex-direction:column}
.header{background:linear-gradient(135deg,#1a1a2e,#16213e);padding:14px 24px;border-bottom:1px solid rgba(255,255,255,.08);display:flex;align-items:center;gap:12px;flex-shrink:0}
.header h1{font-size:18px;font-weight:600;background:linear-gradient(90deg,#4fc3f7,#81c784);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.status{font-size:12px;padding:4px 10px;border-radius:12px;transition:all .3s}
.status.ok{background:rgba(76,175,80,.15);color:#81c784;border:1px solid rgba(76,175,80,.3)}
.status.ok::before{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:#4caf50;margin-right:6px;animation:pulse 2s infinite}
.status.err{background:rgba(244,67,54,.15);color:#ef9a9a;border:1px solid rgba(244,67,54,.3)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.stats{margin-left:auto;font-size:12px;color:#666;display:flex;gap:16px}
.messages{flex:1;overflow-y:auto;padding:12px}
.msg{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:10px 14px;margin-bottom:5px;transition:transform .3s ease}
.msg:hover{background:rgba(255,255,255,.05)}
.msg.hl{border-left:3px solid #4fc3f7;background:rgba(79,195,247,.05);animation:slideIn .3s cubic-bezier(.22,1,.36,1)}
@keyframes slideIn{from{opacity:0;transform:translateY(-20px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
.msg-header{display:flex;align-items:center;gap:8px;margin-bottom:3px}
.msg-time{font-size:11px;color:#555;font-family:"SF Mono",Monaco,monospace;min-width:55px}
.msg-chat{font-weight:600;color:#4fc3f7;font-size:13px;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.msg-chat.grp{color:#ce93d8}
.msg-sender{font-size:12px;color:#999}
.msg-r{margin-left:auto;display:flex;gap:6px;align-items:center}
.msg-type{font-size:10px;padding:2px 5px;border-radius:3px;background:rgba(255,255,255,.06);color:#777}
.msg-unread{font-size:10px;padding:1px 6px;border-radius:8px;background:rgba(244,67,54,.2);color:#ef9a9a;font-weight:600}
.msg-perf{font-size:9px;color:#333}
.msg-content{font-size:13px;line-height:1.4;color:#bbb;word-break:break-all;padding-left:63px}
.msg-img{max-width:300px;max-height:200px;border-radius:8px;cursor:pointer;margin-top:4px;transition:transform .2s}
.msg-img:hover{transform:scale(1.02)}
.msg-emoji{max-width:120px;max-height:120px;border-radius:4px;margin-top:2px}
.msg-link{display:inline-block;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:8px 12px;margin-top:4px;max-width:400px;cursor:pointer;transition:background .2s}
.msg-link:hover{background:rgba(255,255,255,.1)}
.msg-link-title{font-size:13px;color:#4fc3f7;font-weight:500;line-height:1.3}
.msg-link-des{font-size:11px;color:#888;margin-top:3px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.msg-link-src{font-size:10px;color:#555;margin-top:4px}
.msg-quote{background:rgba(255,255,255,.04);border-left:2px solid #666;padding:4px 8px;margin-top:4px;border-radius:0 6px 6px 0}
.msg-quote-ref{font-size:11px;color:#777;margin-bottom:3px}
.msg-quote-ref b{color:#999;font-weight:500}
.msg-file{display:inline-flex;align-items:center;gap:8px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:8px 12px;margin-top:4px}
.msg-file-icon{font-size:24px}
.msg-file-name{font-size:13px;color:#ccc}
.msg-file-size{font-size:11px;color:#666}
.msg-voice{display:inline-flex;align-items:center;gap:8px;background:rgba(76,175,80,.1);border:1px solid rgba(76,175,80,.2);border-radius:16px;padding:6px 14px;margin-top:4px}
.msg-voice audio{height:26px;max-width:260px}
.msg-video{display:inline-flex;align-items:center;gap:6px;background:rgba(79,195,247,.08);border:1px solid rgba(79,195,247,.15);border-radius:8px;padding:6px 12px;margin-top:4px}
.msg-chatlog{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:8px 12px;margin-top:4px;max-width:450px}
.chatlog-body{margin-top:6px;border-top:1px solid rgba(255,255,255,.06);padding-top:6px}
.chatlog-item{font-size:12px;color:#999;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chatlog-item b{color:#bbb;font-weight:500}
.chatlog-more{font-size:11px;color:#555;margin-top:4px}
a.msg-link{text-decoration:none;color:inherit}
#lightbox{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.92);z-index:1000;cursor:zoom-out;justify-content:center;align-items:center}
#lightbox.show{display:flex}
#lightbox img{max-width:95vw;max-height:95vh;object-fit:contain;border-radius:4px;box-shadow:0 4px 30px rgba(0,0,0,.5)}
.empty{text-align:center;padding:80px 20px;color:#444}
.empty .icon{font-size:48px;margin-bottom:12px}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:2px}
/* 设置面板 */
.settings-btn{background:none;border:1px solid rgba(255,255,255,.15);color:#888;font-size:16px;cursor:pointer;padding:4px 8px;border-radius:6px;transition:all .2s}
.settings-btn:hover{color:#ccc;border-color:rgba(255,255,255,.3)}
.settings-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);z-index:900}
.settings-overlay.show{display:block}
.settings-panel{position:fixed;top:0;right:-420px;width:400px;height:100%;background:#12121a;border-left:1px solid rgba(255,255,255,.1);z-index:901;transition:right .3s ease;display:flex;flex-direction:column;overflow:hidden}
.settings-panel.show{right:0}
.sp-header{padding:16px 20px;border-bottom:1px solid rgba(255,255,255,.08);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.sp-header h2{font-size:16px;color:#e0e0e0;font-weight:600}
.sp-close{background:none;border:none;color:#666;font-size:20px;cursor:pointer;padding:4px 8px}
.sp-close:hover{color:#ccc}
.sp-body{flex:1;overflow-y:auto;padding:16px 20px}
.sp-section{margin-bottom:20px}
.sp-section h3{font-size:13px;color:#888;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px}
.sp-toggle{display:flex;align-items:center;justify-content:space-between;padding:8px 0}
.sp-toggle label{font-size:13px;color:#ccc}
.switch{position:relative;width:40px;height:22px;flex-shrink:0}
.switch input{display:none}
.switch .slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#333;border-radius:11px;transition:.3s}
.switch input:checked+.slider{background:#4caf50}
.switch .slider:before{content:'';position:absolute;height:16px;width:16px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
.switch input:checked+.slider:before{transform:translateX(18px)}
.rule-card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:12px;margin-bottom:10px}
.rule-card .rule-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.rule-card .rule-del{background:none;border:none;color:#666;cursor:pointer;font-size:14px;padding:2px 6px}
.rule-card .rule-del:hover{color:#ef5350}
.rule-card input[type=text]{width:100%;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:4px;padding:6px 8px;color:#ccc;font-size:12px;margin-bottom:6px;outline:none}
.rule-card input[type=text]:focus{border-color:rgba(79,195,247,.5)}
.rule-card input[type=text]::placeholder{color:#555}
.rule-opts{display:flex;gap:12px;margin-top:4px}
.rule-opts label{font-size:11px;color:#999;display:flex;align-items:center;gap:4px;cursor:pointer}
.rule-opts input[type=checkbox]{accent-color:#4caf50}
.add-rule-btn{width:100%;padding:8px;background:rgba(79,195,247,.1);border:1px dashed rgba(79,195,247,.3);border-radius:6px;color:#4fc3f7;font-size:12px;cursor:pointer;transition:all .2s}
.add-rule-btn:hover{background:rgba(79,195,247,.2)}
/* 通知高亮 */
.msg.notify-hl{border-left:3px solid #ffd54f;background:rgba(255,213,79,.08);box-shadow:0 0 12px rgba(255,213,79,.1)}
</style>
</head>
<body>
<div class="header">
<h1>WeChat Monitor</h1>
<div class="status ok" id="st">SSE 实时</div>
<div class="stats"><span id="cnt">0 消息</span><span id="perf"></span></div>
<button class="settings-btn" onclick="toggleSettings()" title="通知设置">⚙️</button>
</div>
<div class="settings-overlay" id="settingsOverlay" onclick="toggleSettings()"></div>
<div class="settings-panel" id="settingsPanel">
<div class="sp-header"><h2>通知设置</h2><button class="sp-close" onclick="toggleSettings()">&times;</button></div>
<div class="sp-body">
<div class="sp-section">
<h3>全局</h3>
<div class="sp-toggle"><label>启用通知过滤</label><label class="switch"><input type="checkbox" id="notifyEnabled" onchange="saveNotifySettings()"><span class="slider"></span></label></div>
<div class="sp-toggle"><label>声音提醒</label><label class="switch"><input type="checkbox" id="soundEnabled" onchange="saveNotifySettings()"><span class="slider"></span></label></div>
</div>
<div class="sp-section">
<h3>规则</h3>
<div id="rulesContainer"></div>
<button class="add-rule-btn" onclick="addRule()">+ 添加规则</button>
</div>
</div>
</div>
<div id="lightbox" onclick="this.classList.remove('show')"><img id="lb-img" /></div>
<div class="messages" id="msgs">
<div class="empty" id="empty"><div class="icon">📡</div><p>等待新消息...</p><p style="margin-top:6px;font-size:11px;color:#333">WAL增量解密 · SSE推送</p></div>
</div>
<script>
let n=0;
const M=document.getElementById('msgs'), S=document.getElementById('st');
const seen = new Set();  // 去重: timestamp+username
let sseReady = false;

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
const WX_EMOJI={'微笑':'😊','撇嘴':'😣','色':'😍','发呆':'😳','得意':'😎','流泪':'😢','害羞':'😳','闭嘴':'🤐','睡':'😴','大哭':'😭','尴尬':'😅','发怒':'😡','调皮':'😜','呲牙':'😁','惊讶':'😮','难过':'😞','酷':'😎','冷汗':'😰','抓狂':'😫','吐':'🤮','偷笑':'🤭','可爱':'🥰','白眼':'🙄','傲慢':'😤','饥饿':'🤤','困':'😪','惊恐':'😨','流汗':'😓','憨笑':'😄','大兵':'🫡','奋斗':'💪','咒骂':'🤬','疑问':'❓','嘘':'🤫','晕':'😵','折磨':'😩','衰':'😥','骷髅':'💀','敲打':'🔨','再见':'👋','擦汗':'😓','抠鼻':'🤏','鼓掌':'👏','糗大了':'😳','坏笑':'😏','左哼哼':'😤','右哼哼':'😤','哈欠':'🥱','鄙视':'😒','委屈':'🥺','快哭了':'🥺','阴险':'😈','亲亲':'😘','吓':'😱','可怜':'🥺','菜刀':'🔪','西瓜':'🍉','啤酒':'🍺','篮球':'🏀','乒乓':'🏓','咖啡':'☕','饭':'🍚','猪头':'🐷','玫瑰':'🌹','凋谢':'🥀','示爱':'💗','爱心':'❤️','心碎':'💔','蛋糕':'🎂','闪电':'⚡','炸弹':'💣','刀':'🔪','足球':'⚽','瓢虫':'🐞','便便':'💩','月亮':'🌙','太阳':'☀️','礼物':'🎁','拥抱':'🤗','强':'👍','弱':'👎','握手':'🤝','胜利':'✌️','抱拳':'🙏','勾引':'👆','拳头':'✊','差劲':'👎','爱你':'🤟','NO':'🙅','OK':'👌','爱情':'💑','飞吻':'😘','跳跳':'💃','发抖':'🥶','怄火':'😤','转圈':'💫','磕头':'🙇','回头':'🔙','跳绳':'🏃','挥手':'👋','激动':'🤩','街舞':'💃','献吻':'😘','左太极':'☯️','右太极':'☯️','嘿哈':'😆','捂脸':'🤦','奸笑':'😏','机智':'🤓','皱眉':'😟','耶':'✌️','红包':'🧧','鸡':'🐔','Emm':'🤔','加油':'💪','汗':'😓','天啊':'😱','社会社会':'🤙','旺柴':'🐕','好的':'👌','打脸':'🤦','哇':'😲','翻白眼':'🙄','666':'👍','让我看看':'👀','叹气':'😮‍💨','苦涩':'😣','裂开':'💔','嘴唇':'💋','爱心':'❤️','破涕为笑':'😂'};
function wxEmoji(text){
  return text.replace(/\\[([^\\]]{1,4})\\]/g, (m,k)=>WX_EMOJI[k]||m);
}
function linkify(text){
  return text.replace(/(https?:\\/\\/[^\\s<>"'\\]\\)]+)/g, '<a href="$1" target="_blank" rel="noopener" style="color:#4fc3f7;text-decoration:underline">$1</a>');
}
function fmtSize(b){
  if(b<1024) return b+'B';
  if(b<1048576) return (b/1024).toFixed(1)+'KB';
  return (b/1048576).toFixed(1)+'MB';
}
function renderRich(r){
  if(!r) return null;
  if(r.type==='emoji' && r.emoji_url) return `<img class="msg-emoji" src="${esc(r.emoji_url)}" onerror="this.outerHTML='<span style=\\'color:#999\\'>😀 [表情]</span>'" />`;
  if(r.type==='link') {
    let src = r.source ? '<div class="msg-link-src">'+esc(r.source)+'</div>' : '';
    return `<a href="${esc(r.url)}" target="_blank" rel="noopener" class="msg-link"><div class="msg-link-title">🔗 ${esc(r.title)}</div>${r.des?'<div class="msg-link-des">'+esc(r.des)+'</div>':''}${src}</a>`;
  }
  if(r.type==='file') return `<div class="msg-file"><span class="msg-file-icon">📄</span><div><div class="msg-file-name">${esc(r.title)}</div><div class="msg-file-size">${r.file_ext?r.file_ext.toUpperCase()+' · ':''}${fmtSize(r.file_size)}</div></div></div>`;
  if(r.type==='quote') return `<div class="msg-quote"><div class="msg-quote-ref">↩ <b>${esc(r.ref_name)}</b>: ${esc(r.ref_content)}</div><div>${esc(r.title)}</div></div>`;
  if(r.type==='miniapp') return `<div class="msg-link"><div class="msg-link-title">🟢 ${esc(r.title)}</div>${r.source?'<div class="msg-link-src">小程序 · '+esc(r.source)+'</div>':''}</div>`;
  if(r.type==='channels') return `<div class="msg-video"><span>📺</span> ${esc(r.title)} <span style="color:#666;font-size:11px">视频号</span></div>`;
  if(r.type==='chatlog') {
    let items = r.items||[];
    let body = '';
    if(items.length>0) {
      let preview = items.slice(0,4).map(it=>'<div class="chatlog-item"><b>'+esc(it.name)+'</b>: '+esc(it.text)+'</div>').join('');
      let more = items.length>4 ? '<div class="chatlog-more">... 共'+items.length+'条消息</div>' : '';
      body = '<div class="chatlog-body">'+preview+more+'</div>';
    } else if(r.des) {
      body = '<div class="msg-link-des">'+esc(r.des)+'</div>';
    }
    return `<div class="msg-chatlog"><div class="msg-link-title">📋 ${esc(r.title)}</div>${body}</div>`;
  }
  if(r.type==='voice'){
    const d=(r.duration!=null)?` ${r.duration}s`:``;
    if(r.voice_url) return `<div class="msg-voice"><audio controls preload="none" src="${r.voice_url}"></audio><span style="color:#9f9">${d}</span></div>`;
    return `<div class="msg-voice">🎤 语音${d}</div>`;
  }
  if(r.type==='video') return `<div class="msg-video">🎬 视频${r.duration?' '+r.duration+'s':''}</div>`;
  return null;
}
function showLightbox(url){
  const lb=document.getElementById('lightbox'), img=document.getElementById('lb-img');
  img.src=url;
  lb.classList.add('show');
}
function renderContent(m){
  if(m.image_url) return `<img class="msg-img" src="${m.image_url}" onclick="showLightbox('${m.image_url}')" onerror="this.style.display='none';this.nextElementSibling.style.display='inline'" /><span style="display:none">${esc(m.content||'')}</span>`;
  const richHtml = renderRich(m.rich);
  if(richHtml) return richHtml;
  const raw = esc(m.content||'');
  return linkify(wxEmoji(raw));
}

// ---- 通知过滤 ----
const DEFAULT_NOTIFY = {enabled:false, sound_enabled:true, rules:[]};
function loadNotifySettings(){
  try{ return JSON.parse(localStorage.getItem('wechat_notify'))||DEFAULT_NOTIFY; }catch(e){ return DEFAULT_NOTIFY; }
}
function saveNotifySettings(){
  const s = {
    enabled: document.getElementById('notifyEnabled').checked,
    sound_enabled: document.getElementById('soundEnabled').checked,
    rules: collectRules()
  };
  localStorage.setItem('wechat_notify', JSON.stringify(s));
}
function collectRules(){
  const rules=[];
  document.querySelectorAll('.rule-card').forEach(card=>{
    const inputs=card.querySelectorAll('input[type=text]');
    const checks=card.querySelectorAll('input[type=checkbox]');
    rules.push({
      group_name: inputs[0]?.value||'',
      sender_name: inputs[1]?.value||'',
      notify_on_any: checks[0]?.checked||false
    });
  });
  return rules;
}
function renderRules(){
  const s=loadNotifySettings();
  document.getElementById('notifyEnabled').checked=s.enabled;
  document.getElementById('soundEnabled').checked=s.sound_enabled;
  const c=document.getElementById('rulesContainer');
  c.innerHTML='';
  (s.rules||[]).forEach((_,i)=>addRuleCard(s.rules[i]));
}
function addRuleCard(r){
  r=r||{group_name:'',sender_name:'',notify_on_any:true};
  const c=document.getElementById('rulesContainer');
  const d=document.createElement('div');
  d.className='rule-card';
  d.innerHTML=`<div class="rule-header"><span style="font-size:12px;color:#888">规则 #${c.children.length+1}</span><button class="rule-del" onclick="this.closest('.rule-card').remove();saveNotifySettings()">&times;</button></div><input type="text" placeholder="群名（模糊匹配）" value="${esc(r.group_name)}" onchange="saveNotifySettings()"><input type="text" placeholder="发送人（可选，模糊匹配）" value="${esc(r.sender_name)}" onchange="saveNotifySettings()"><div class="rule-opts"><label><input type="checkbox" ${r.notify_on_any?'checked':''} onchange="saveNotifySettings()"> 匹配时通知</label></div>`;
  c.appendChild(d);
}
function addRule(){addRuleCard();saveNotifySettings();}
function toggleSettings(){
  const p=document.getElementById('settingsPanel'),o=document.getElementById('settingsOverlay');
  const show=!p.classList.contains('show');
  p.classList.toggle('show',show);
  o.classList.toggle('show',show);
  if(show) renderRules();
}
function beep(){
  try{
    const ctx=new(window.AudioContext||window.webkitAudioContext)();
    const osc=ctx.createOscillator();
    const gain=ctx.createGain();
    osc.connect(gain);gain.connect(ctx.destination);
    osc.frequency.value=880;gain.gain.value=0.3;
    osc.start();osc.stop(ctx.currentTime+0.15);
  }catch(e){}
}
function checkNotifyMatch(m){
  const s=loadNotifySettings();
  if(!s.enabled||!s.rules||!s.rules.length) return false;
  const chat=(m.chat||'').toLowerCase();
  const sender=(m.sender||'').toLowerCase();
  for(const r of s.rules){
    if(!r.group_name) continue;
    if(!chat.includes(r.group_name.toLowerCase())) continue;
    if(r.sender_name && !sender.includes(r.sender_name.toLowerCase())) continue;
    if(r.notify_on_any) return true;
  }
  return false;
}
function sendNotification(m){
  const title=m.chat+(m.sender?' - '+m.sender:'');
  const body=(m.content||'').slice(0,100);
  if(Notification.permission==='granted'){
    new Notification(title,{body,icon:'📡'});
  }else if(Notification.permission!=='denied'){
    Notification.requestPermission().then(p=>{if(p==='granted') new Notification(title,{body,icon:'📡'});});
  }
  const s=loadNotifySettings();
  if(s.sound_enabled) beep();
}

function addMsg(m, animate){
  // 去重（包含类型，避免同时间戳的文字+图片组合被误判重复）
  const key = m.timestamp + '|' + (m.username||m.chat) + '|' + (m.type||'');
  if(seen.has(key)) return;
  seen.add(key);

  const x=document.getElementById('empty');
  if(x) x.remove();

  n++;
  document.getElementById('cnt').textContent=n+' 消息';
  if(m.decrypt_ms!=null) document.getElementById('perf').textContent=m.pages+'页/'+m.decrypt_ms+'ms';

  const d=document.createElement('div');
  d.className = animate ? 'msg hl' : 'msg';

  const sn=m.sender?`<span class="msg-sender">${esc(m.sender)}</span>`:'';
  const ur=m.unread>0?`<span class="msg-unread">${m.unread}</span>`:'';
  const cc=m.is_group?'msg-chat grp':'msg-chat';

  let contentHtml = renderContent(m);

  const dk=m.timestamp+'|'+(m.username||m.chat);
  d.innerHTML=`<div class="msg-header"><span class="msg-time">${m.time}</span><span class="${cc}">${esc(m.chat)}</span>${sn}<div class="msg-r"><span class="msg-type">${m.type_icon} ${m.type}</span>${ur}</div></div><div class="msg-content" data-key="${dk}">${contentHtml}</div>`;

  // 通知匹配检查
  if(animate && checkNotifyMatch(m)){
    d.classList.add('notify-hl');
    sendNotification(m);
    setTimeout(()=>d.classList.remove('notify-hl'), 10000);
  }

  M.insertBefore(d, M.firstChild);

  if(animate){
    setTimeout(()=>d.classList.remove('hl'), 3000);
    document.title='('+n+') 微信监听';
  }

  // 限制最多200条
  while(M.children.length>200) M.removeChild(M.lastChild);
}

// 页面加载时请求通知权限
if('Notification' in window && Notification.permission==='default'){
  Notification.requestPermission();
}

function connectSSE(){
  const es=new EventSource('/stream');
  es.onopen=()=>{
    S.textContent='SSE 实时';
    S.className='status ok';
    sseReady=true;
  };
  es.onmessage=ev=>{
    addMsg(JSON.parse(ev.data), true);  // 新消息有动画
  };
  es.addEventListener('image_update', ev=>{
    const d=JSON.parse(ev.data);
    const key=d.timestamp+'|'+(d.username||'');
    const msgs=M.querySelectorAll('.msg');
    for(const el of msgs){
      const ct=el.querySelector('.msg-content');
      if(ct && ct.dataset.key===key){
        if(d.v2_unsupported){
          ct.innerHTML='<span style="color:#999;font-style:italic">[图片 - 新加密格式暂不支持预览]</span>';
        } else if(d.image_url){
          ct.innerHTML=`<img class="msg-img" src="${d.image_url}" onclick="showLightbox('${d.image_url}')" onerror="this.style.display='none'" />`;
        }
        break;
      }
    }
  });
  es.addEventListener('rich_update', ev=>{
    const d=JSON.parse(ev.data);
    const key=d.timestamp+'|'+(d.username||'');
    for(const el of M.querySelectorAll('.msg')){
      const ct=el.querySelector('.msg-content');
      if(ct && ct.dataset.key===key){
        const html=renderRich(d.rich);
        if(html) ct.innerHTML=html;
        break;
      }
    }
  });
  es.onerror=()=>{
    S.textContent='重连...';
    S.className='status err';
    sseReady=false;
    es.close();
    setTimeout(connectSSE, 2000);  // 重连不清页面
  };
}

// 启动: 加载历史(无动画) → 连接SSE(有动画)
fetch('/api/history').then(r=>r.json()).then(ms=>{
  ms.sort((a,b)=>a.timestamp-b.timestamp);
  ms.forEach(m=>addMsg(m, false));  // 历史消息无动画
  connectSSE();
});
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    contact_names = {}
    contact_full = []
    db_cache = None
    username_db_map = {}
    persona_store = None
    self_username = ""

    def _send_json(self, status_code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self, max_bytes=1024 * 1024):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except Exception:
            length = 0
        if length <= 0:
            return None
        if length > max_bytes:
            raise ValueError("body too large")
        raw = self.rfile.read(length)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("invalid json")

    def _check_write_auth(self) -> bool:
        """可选写接口鉴权：config.json 设置 api_token 后启用。"""
        if not API_TOKEN:
            return True

        token = ""
        auth = self.headers.get("Authorization") or ""
        if isinstance(auth, str) and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = (self.headers.get("X-Api-Token") or "").strip()

        if token != API_TOKEN:
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return False
        return True

    def log_message(self, *a): pass
    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass  # 浏览器关闭连接，正常

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query or "")

        if path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode('utf-8'))

        elif path == '/api/history':
            with messages_lock:
                data = sorted(messages_log, key=lambda m: m.get('timestamp', 0))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

        elif path == '/api/v1/health':
            with messages_lock:
                last_seq = messages_log[-1].get("seq", 0) if messages_log else 0
            payload = {
                "ok": True,
                "time": int(time.time()),
                "last_seq": last_seq,
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        elif path == '/api/v1/state':
            with messages_lock:
                last_seq = messages_log[-1].get("seq", 0) if messages_log else 0
            payload = {
                "time": int(time.time()),
                "last_seq": last_seq,
                "contacts_loaded": bool(getattr(self.__class__, "contact_full", None)),
                "self_username": getattr(self.__class__, "self_username", "") or "",
                "write_auth_enabled": bool(API_TOKEN),
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        elif path == '/api/v1/contacts':
            try:
                limit = int((qs.get("limit", ["50"])[0] or "50").strip())
            except Exception:
                limit = 50
            limit = max(1, min(limit, 500))
            query = (qs.get("query", [""])[0] or "").strip().lower()

            items = list(getattr(self.__class__, "contact_full", []) or [])
            if query:
                def _hit(c):
                    return (
                        query in (c.get("username", "").lower())
                        or query in (c.get("nick_name", "").lower())
                        or query in (c.get("remark", "").lower())
                        or query in (c.get("display_name", "").lower())
                    )
                items = [c for c in items if _hit(c)]

            def _with_avatar(c):
                c2 = dict(c)
                uname = c2.get("username", "") or ""
                c2["avatar_url"] = f"/avatar/{urllib.parse.quote(uname, safe='')}" if uname else ""
                return c2

            payload = {"items": [_with_avatar(c) for c in items[:limit]]}
            self._send_json(200, payload)

        elif path == '/api/v1/sessions':
            try:
                limit = int((qs.get("limit", ["50"])[0] or "50").strip())
            except Exception:
                limit = 50
            limit = max(1, min(limit, 500))

            if not os.path.exists(DECRYPTED_SESSION):
                self.send_error(503, "session cache not ready")
                return

            names = getattr(self.__class__, "contact_names", {}) or {}
            try:
                conn = sqlite3.connect(f"file:{DECRYPTED_SESSION}?mode=ro", uri=True)
                rows = conn.execute("""
                    SELECT username, unread_count, summary, last_timestamp,
                           last_msg_type, last_msg_sender, last_sender_display_name
                    FROM SessionTable
                    WHERE last_timestamp > 0
                    ORDER BY last_timestamp DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                conn.close()
            except Exception as e:
                self.send_error(500, f"query session failed: {e}")
                return

            items = []
            for username, unread, summary, ts, msg_type, sender, sender_name in rows:
                display = names.get(username, username)
                is_group = '@chatroom' in (username or "")
                sender_display = ""
                if is_group and sender:
                    sender_display = names.get(sender, sender_name or sender)

                if isinstance(summary, bytes):
                    try:
                        summary = _zstd_dctx.decompress(summary).decode('utf-8', errors='replace')
                    except Exception:
                        summary = '(压缩内容)'
                if isinstance(summary, str) and ':\n' in summary:
                    summary = summary.split(':\n', 1)[1]

                items.append({
                    "username": username,
                    "display_name": display,
                    "is_group": is_group,
                    "avatar_url": f"/avatar/{urllib.parse.quote(username, safe='')}" if username else "",
                    "unread": unread or 0,
                    "last_timestamp": ts or 0,
                    "last_msg_type": msg_type or 0,
                    "last_msg_type_label": format_msg_type(msg_type or 0),
                    "summary": summary or "",
                    "sender_username": sender or "",
                    "sender_display_name": sender_display or "",
                })

            self._send_json(200, {"items": items})

        elif path == "/api/v1/recent_contacts":
            try:
                limit = int((qs.get("limit", ["20"])[0] or "20").strip())
            except Exception:
                limit = 20
            limit = max(1, min(limit, 500))

            try:
                offset = int((qs.get("offset", ["0"])[0] or "0").strip())
            except Exception:
                offset = 0
            offset = max(0, offset)

            store = getattr(self.__class__, "persona_store", None)
            if not store:
                self._send_json(200, {"items": [], "limit": limit, "offset": offset})
                return

            try:
                rec = store.list_recent_contacts(limit=limit, offset=offset)
            except Exception as e:
                self.send_error(500, f"recent_contacts failed: {e}")
                return

            names = getattr(self.__class__, "contact_names", {}) or {}
            full = getattr(self.__class__, "contact_full", []) or []
            full_map = {c.get("username"): c for c in full if c.get("username")}

            items = []
            for r in rec.get("items", []) or []:
                uname = (r.get("username") or "").strip()
                base = full_map.get(uname)
                if base:
                    item = dict(base)
                else:
                    item = {
                        "username": uname,
                        "nick_name": "",
                        "remark": "",
                        "display_name": names.get(uname, uname),
                        "is_group": ("@chatroom" in uname),
                    }
                item.update({
                    "last_access_ts": int(r.get("last_access_ts") or 0),
                    "access_count": int(r.get("access_count") or 0),
                    "avatar_url": f"/avatar/{urllib.parse.quote(uname, safe='')}" if uname else "",
                })
                items.append(item)

            self._send_json(200, {"items": items, "limit": limit, "offset": offset})

        elif path.startswith("/api/v1/people/"):
            parts = path.strip("/").split("/")
            # /api/v1/people/{username}/profile
            if len(parts) == 5 and parts[0:3] == ["api", "v1", "people"] and parts[4] == "profile":
                username = urllib.parse.unquote(parts[3] or "").strip()
                if not username:
                    self.send_error(400, "missing username")
                    return
                store = getattr(self.__class__, "persona_store", None)
                if not store:
                    self.send_error(503, "persona store not ready")
                    return
                try:
                    profile = store.get_profile(username)
                    store.record_recent_contact(username)
                except Exception as e:
                    self.send_error(500, f"profile failed: {e}")
                    return

                names = getattr(self.__class__, "contact_names", {}) or {}
                full = getattr(self.__class__, "contact_full", []) or []
                full_map = {c.get("username"): c for c in full if c.get("username")}
                base = full_map.get(username) or {
                    "username": username,
                    "nick_name": "",
                    "remark": "",
                    "display_name": names.get(username, username),
                    "is_group": ("@chatroom" in username),
                }
                base = dict(base)
                base["avatar_url"] = f"/avatar/{urllib.parse.quote(username, safe='')}"
                self._send_json(200, {"contact": base, "profile": profile})
                return

            # /api/v1/people/{username}/memories
            if len(parts) == 5 and parts[0:3] == ["api", "v1", "people"] and parts[4] == "memories":
                username = urllib.parse.unquote(parts[3] or "").strip()
                if not username:
                    self.send_error(400, "missing username")
                    return
                store = getattr(self.__class__, "persona_store", None)
                if not store:
                    self.send_error(503, "persona store not ready")
                    return
                kind = (qs.get("kind", [""])[0] or "").strip()
                status = (qs.get("status", ["active"])[0] or "active").strip()
                q = (qs.get("q", [""])[0] or "").strip()
                try:
                    limit = int((qs.get("limit", ["50"])[0] or "50").strip())
                except Exception:
                    limit = 50
                try:
                    offset = int((qs.get("offset", ["0"])[0] or "0").strip())
                except Exception:
                    offset = 0

                try:
                    result = store.list_memories(
                        username=username,
                        kind=kind,
                        status=status,
                        q=q,
                        limit=limit,
                        offset=offset,
                    )
                    store.record_recent_contact(username)
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                except Exception as e:
                    self.send_error(500, f"memories failed: {e}")
                    return

                self._send_json(200, {"username": username, **result})
                return

            # /api/v1/people/{username}/runs
            if len(parts) == 5 and parts[0:3] == ["api", "v1", "people"] and parts[4] == "runs":
                username = urllib.parse.unquote(parts[3] or "").strip()
                if not username:
                    self.send_error(400, "missing username")
                    return
                store = getattr(self.__class__, "persona_store", None)
                if not store:
                    self.send_error(503, "persona store not ready")
                    return
                kind = (qs.get("kind", [""])[0] or "").strip()
                status = (qs.get("status", [""])[0] or "").strip()
                try:
                    limit = int((qs.get("limit", ["20"])[0] or "20").strip())
                except Exception:
                    limit = 20
                try:
                    offset = int((qs.get("offset", ["0"])[0] or "0").strip())
                except Exception:
                    offset = 0

                try:
                    result = store.list_runs(username=username, kind=kind, status=status, limit=limit, offset=offset)
                    store.record_recent_contact(username)
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                except Exception as e:
                    self.send_error(500, f"runs failed: {e}")
                    return

                self._send_json(200, {"username": username, **result})
                return

        elif path == '/api/v1/messages':
            try:
                limit = int((qs.get("limit", ["200"])[0] or "200").strip())
            except Exception:
                limit = 200
            limit = max(1, min(limit, 2000))

            try:
                after_seq = int((qs.get("after_seq", ["0"])[0] or "0").strip())
            except Exception:
                after_seq = 0
            after_seq = max(0, after_seq)

            with messages_lock:
                if messages_log:
                    first_seq = messages_log[0].get("seq", 0) or 0
                    last_seq = messages_log[-1].get("seq", 0) or 0
                else:
                    first_seq = 0
                    last_seq = 0

                items = [m for m in messages_log if (m.get("seq", 0) or 0) > after_seq]

            payload = {
                "last_seq": last_seq,
                "first_seq": first_seq,
                "gap": bool(after_seq and first_seq and after_seq < first_seq - 1),
                "items": items[:limit],
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        elif path.startswith('/api/v1/chats/') and path.endswith('/message_status'):
            parts = path.split('/')
            if len(parts) != 6 or parts[5] != 'message_status':
                self.send_error(404)
                return

            username = urllib.parse.unquote(parts[4] or "")
            if not username:
                self.send_error(400, "missing username")
                return

            store = getattr(self.__class__, "persona_store", None)
            if not store:
                self.send_error(503, "persona store not ready")
                return

            app_id = (qs.get("app_id", [""])[0] or "").strip()
            if not app_id:
                self.send_error(400, "missing app_id")
                return

            local_id_raw = (qs.get("local_id", [""])[0] or "").strip()
            if local_id_raw:
                try:
                    local_id = int(local_id_raw)
                except Exception:
                    self.send_error(400, "invalid local_id")
                    return
                item = store.get_message_status(username=username, app_id=app_id, local_id=local_id)
                store.record_recent_contact(username)
                self._send_json(200, {"item": item})
                return

            status = (qs.get("status", [""])[0] or "").strip()
            try:
                limit = int((qs.get("limit", ["50"])[0] or "50").strip())
            except Exception:
                limit = 50
            try:
                offset = int((qs.get("offset", ["0"])[0] or "0").strip())
            except Exception:
                offset = 0

            try:
                result = store.list_message_status(
                    username=username,
                    app_id=app_id,
                    status=status,
                    limit=limit,
                    offset=offset,
                )
                store.record_recent_contact(username)
            except ValueError as e:
                self.send_error(400, str(e))
                return
            except Exception as e:
                self.send_error(500, f"message_status failed: {e}")
                return

            self._send_json(200, {"username": username, **result})
            return

        elif path.startswith('/api/v1/chats/') and path.endswith('/history'):
            parts = path.split('/')
            if len(parts) != 6 or parts[5] != 'history':
                self.send_error(404)
                return

            username = urllib.parse.unquote(parts[4] or "")
            if not username:
                self.send_error(400, "missing username")
                return

            try:
                limit = int((qs.get("limit", ["50"])[0] or "50").strip())
            except Exception:
                limit = 50
            limit = max(1, min(limit, 2000))

            try:
                offset = int((qs.get("offset", ["0"])[0] or "0").strip())
            except Exception:
                offset = 0
            offset = max(0, offset)

            start_ts = qs.get("start_ts", [""])[0] or ""
            end_ts = qs.get("end_ts", [""])[0] or ""
            try:
                start_ts = int(start_ts) if str(start_ts).strip() else None
            except Exception:
                self.send_error(400, "invalid start_ts")
                return
            try:
                end_ts = int(end_ts) if str(end_ts).strip() else None
            except Exception:
                self.send_error(400, "invalid end_ts")
                return
            if start_ts is not None and end_ts is not None and start_ts > end_ts:
                self.send_error(400, "start_ts > end_ts")
                return

            after_local_id = qs.get("after_local_id", [""])[0] or ""
            try:
                after_local_id = int(after_local_id) if str(after_local_id).strip() else None
            except Exception:
                self.send_error(400, "invalid after_local_id")
                return

            include_raw = (qs.get("include_raw", ["1"])[0] or "1").strip() not in ("0", "false", "False")

            names = getattr(self.__class__, "contact_names", {}) or {}
            display_name = names.get(username, username)
            is_group = "@chatroom" in username

            db_cache = getattr(self.__class__, "db_cache", None)
            username_db_map = getattr(self.__class__, "username_db_map", {}) or {}
            db_keys = username_db_map.get(username, []) or []
            if not db_cache or not db_keys:
                payload = {"username": username, "display_name": display_name, "is_group": is_group, "items": []}
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            dec_paths = []
            for db_key in db_keys:
                dec_path = db_cache.get(db_key)
                if dec_path:
                    dec_paths.append(dec_path)

            self_username = getattr(self.__class__, "self_username", "") or ""
            payload = query_chat_history(
                dec_paths,
                username=username,
                contact_names=names,
                self_username=self_username,
                start_ts=start_ts,
                end_ts=end_ts,
                after_local_id=after_local_id,
                limit=limit,
                offset=offset,
                include_raw=include_raw,
            )
            # 兼容: display_name/is_group 仍按当前 handler 的名字映射
            payload["display_name"] = display_name
            payload["is_group"] = is_group

            # 记录最近查询联系人（供前端“最近联系人”列表使用）
            store = getattr(self.__class__, "persona_store", None)
            if store:
                try:
                    store.record_recent_contact(username)
                except Exception:
                    pass

            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        elif path.startswith("/avatar/"):
            username = urllib.parse.unquote(path[len("/avatar/"):]).strip()
            if not username:
                self.send_error(404)
                return
            # 安全: 防目录穿越
            if "/" in username or "\\" in username or ".." in username:
                self.send_error(403)
                return

            db_cache = getattr(self.__class__, "db_cache", None)
            if not db_cache:
                self.send_error(503, "db cache not ready")
                return

            rel = os.path.join("head_image", "head_image.db")
            db_path = db_cache.get(rel)
            if not db_path or not os.path.exists(db_path):
                self.send_error(503, "head_image cache not ready")
                return

            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                row = conn.execute(
                    "SELECT md5, image_buffer, update_time FROM head_image "
                    "WHERE username=? ORDER BY update_time DESC LIMIT 1",
                    (username,),
                ).fetchone()
                conn.close()
            except Exception as e:
                self.send_error(500, f"query head_image failed: {e}")
                return

            if not row or not row[1]:
                self.send_error(404)
                return

            md5_hex, buf, update_time = row
            if isinstance(buf, memoryview):
                buf = buf.tobytes()
            if not isinstance(buf, (bytes, bytearray)) or len(buf) < 4:
                self.send_error(404)
                return

            if not md5_hex:
                md5_hex = hashlib.md5(buf).hexdigest()

            etag = f"\"{md5_hex}\""
            inm = self.headers.get("If-None-Match", "")
            if inm and inm.strip() == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                return

            ct = "application/octet-stream"
            if buf[:3] == b"\xff\xd8\xff":
                ct = "image/jpeg"
            elif buf[:4] == b"\x89PNG":
                ct = "image/png"
            elif buf[:3] == b"GIF":
                ct = "image/gif"
            elif buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
                ct = "image/webp"

            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(buf)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(buf)

        elif path.startswith('/img/'):
            filename = urllib.parse.unquote(path[5:])
            # 安全: 防目录穿越
            if '/' in filename or '\\' in filename or '..' in filename:
                self.send_error(403)
                return
            filepath = os.path.join(DECODED_IMAGE_DIR, filename)
            if not os.path.isfile(filepath):
                self.send_error(404)
                return
            ext = os.path.splitext(filename)[1].lower()
            ct = {
                '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.png': 'image/png', '.gif': 'image/gif',
                '.webp': 'image/webp', '.bmp': 'image/bmp',
                '.tif': 'image/tiff',
            }.get(ext, 'application/octet-stream')
            with open(filepath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            self.wfile.write(data)

        elif path.startswith('/voice/'):
            filename = urllib.parse.unquote(path[7:])
            # 安全: 防目录穿越
            if '/' in filename or '\\' in filename or '..' in filename:
                self.send_error(403)
                return
            filepath = os.path.join(DECODED_VOICE_DIR, filename)
            if not os.path.isfile(filepath):
                self.send_error(404)
                return
            ext = os.path.splitext(filename)[1].lower()
            ct = {
                '.wav': 'audio/wav',
                '.mp3': 'audio/mpeg',
                '.amr': 'audio/amr',
                '.ogg': 'audio/ogg',
            }.get(ext, 'application/octet-stream')
            with open(filepath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            self.wfile.write(data)

        elif path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()

            q = queue.Queue()
            with sse_lock:
                sse_clients.append(q)
            try:
                while True:
                    try:
                        payload = q.get(timeout=15)
                        self.wfile.write(payload.encode('utf-8'))
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b': hb\n\n')
                        self.wfile.flush()
            except:
                pass
            finally:
                with sse_lock:
                    if q in sse_clients:
                        sse_clients.remove(q)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._check_write_auth():
            return

        if path == "/api/v1/recent_contacts":
            store = getattr(self.__class__, "persona_store", None)
            if not store:
                self.send_error(503, "persona store not ready")
                return
            try:
                payload = self._read_json_body()
            except ValueError as e:
                self.send_error(400, str(e))
                return
            if not isinstance(payload, dict):
                self.send_error(400, "invalid json")
                return
            username = (payload.get("username") or "").strip()
            ts = payload.get("ts")
            try:
                ts = int(ts) if ts is not None else None
            except Exception:
                ts = None
            store.record_recent_contact(username, ts=ts)
            self._send_json(200, {"ok": True})
            return

        if path.startswith('/api/v1/chats/') and path.endswith('/message_status'):
            parts = path.split('/')
            if len(parts) != 6 or parts[5] != 'message_status':
                self.send_error(404)
                return

            username = urllib.parse.unquote(parts[4] or "")
            if not username:
                self.send_error(400, "missing username")
                return

            store = getattr(self.__class__, "persona_store", None)
            if not store:
                self.send_error(503, "persona store not ready")
                return

            try:
                payload = self._read_json_body()
            except ValueError as e:
                self.send_error(400, str(e))
                return
            if not isinstance(payload, dict):
                self.send_error(400, "invalid json")
                return

            try:
                item = store.upsert_message_status(username=username, payload=payload)
                store.record_recent_contact(username)
            except ValueError as e:
                self.send_error(400, str(e))
                return
            except Exception as e:
                self.send_error(500, f"message_status failed: {e}")
                return

            self._send_json(200, {"ok": True, "item": item})
            return

        if path.startswith("/api/v1/people/") and path.endswith("/memories"):
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[0:3] == ["api", "v1", "people"] and parts[4] == "memories":
                username = urllib.parse.unquote(parts[3] or "").strip()
                if not username:
                    self.send_error(400, "missing username")
                    return
                store = getattr(self.__class__, "persona_store", None)
                if not store:
                    self.send_error(503, "persona store not ready")
                    return
                try:
                    payload = self._read_json_body()
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                try:
                    res = store.create_memory(username, payload if isinstance(payload, dict) else {})
                    store.record_recent_contact(username)
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                except Exception as e:
                    self.send_error(500, f"create memory failed: {e}")
                    return
                self._send_json(200, {"ok": True, **res})
                return

        if path.startswith("/api/v1/people/") and path.endswith("/runs"):
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[0:3] == ["api", "v1", "people"] and parts[4] == "runs":
                username = urllib.parse.unquote(parts[3] or "").strip()
                if not username:
                    self.send_error(400, "missing username")
                    return
                store = getattr(self.__class__, "persona_store", None)
                if not store:
                    self.send_error(503, "persona store not ready")
                    return
                try:
                    payload = self._read_json_body()
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                try:
                    res = store.create_run(username, payload if isinstance(payload, dict) else {})
                    store.record_recent_contact(username)
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                except Exception as e:
                    self.send_error(500, f"create run failed: {e}")
                    return
                self._send_json(200, {"ok": True, **res})
                return

        self.send_error(404)

    def do_PATCH(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._check_write_auth():
            return

        store = getattr(self.__class__, "persona_store", None)
        if not store:
            self.send_error(503, "persona store not ready")
            return

        if path.startswith("/api/v1/people/") and path.endswith("/profile"):
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[0:3] == ["api", "v1", "people"] and parts[4] == "profile":
                username = urllib.parse.unquote(parts[3] or "").strip()
                if not username:
                    self.send_error(400, "missing username")
                    return
                try:
                    patch = self._read_json_body() or {}
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                try:
                    profile = store.patch_profile(username, patch if isinstance(patch, dict) else {})
                    store.record_recent_contact(username)
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                except Exception as e:
                    self.send_error(500, f"patch profile failed: {e}")
                    return
                self._send_json(200, {"ok": True, "profile": profile})
                return

        if path.startswith("/api/v1/memories/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0:3] == ["api", "v1", "memories"]:
                mem_id = urllib.parse.unquote(parts[3] or "").strip()
                try:
                    patch = self._read_json_body() or {}
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                try:
                    store.patch_memory(mem_id, patch if isinstance(patch, dict) else {})
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                except Exception as e:
                    self.send_error(500, f"patch memory failed: {e}")
                    return
                self._send_json(200, {"ok": True})
                return

        if path.startswith("/api/v1/runs/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0:3] == ["api", "v1", "runs"]:
                run_id = urllib.parse.unquote(parts[3] or "").strip()
                if not run_id:
                    self.send_error(400, "missing run_id")
                    return
                try:
                    patch = self._read_json_body() or {}
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                try:
                    store.patch_run(run_id, patch if isinstance(patch, dict) else {})
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                except Exception as e:
                    self.send_error(500, f"patch run failed: {e}")
                    return
                self._send_json(200, {"ok": True})
                return

        self.send_error(404)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if not self._check_write_auth():
            return

        store = getattr(self.__class__, "persona_store", None)
        if not store:
            self.send_error(503, "persona store not ready")
            return

        if path.startswith("/api/v1/memories/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0:3] == ["api", "v1", "memories"]:
                mem_id = urllib.parse.unquote(parts[3] or "").strip()
                try:
                    store.patch_memory(mem_id, {"status": "invalidated"})
                except ValueError as e:
                    self.send_error(400, str(e))
                    return
                except Exception as e:
                    self.send_error(500, f"delete memory failed: {e}")
                    return
                self._send_json(200, {"ok": True})
                return

        self.send_error(404)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    print("=" * 60, flush=True)
    print("  微信实时监听 (WAL增量 + SSE推送)", flush=True)
    print("=" * 60, flush=True)

    with open(KEYS_FILE, encoding="utf-8") as f:
        keys = strip_key_metadata(json.load(f))

    session_key_info = get_key_info(keys, os.path.join("session", "session.db"))
    if not session_key_info:
        print("[ERROR] 找不到 session.db 的密钥", flush=True)
        sys.exit(1)
    enc_key = bytes.fromhex(session_key_info["enc_key"])
    session_db = os.path.join(DB_DIR, "session", "session.db")

    # 启动时清理可能损坏的缓存
    if os.path.isdir(MONITOR_CACHE_DIR):
        for f in os.listdir(MONITOR_CACHE_DIR):
            fp = os.path.join(MONITOR_CACHE_DIR, f)
            if f.endswith('.db'):
                try:
                    c = sqlite3.connect(fp)
                    c.execute("SELECT 1 FROM sqlite_master LIMIT 1")
                    c.close()
                except Exception:
                    try:
                        os.unlink(fp)
                        print(f"[cleanup] 删除损坏缓存: {f}", flush=True)
                    except PermissionError:
                        print(f"[cleanup] 缓存被占用跳过: {f}", flush=True)

    db_cache = MonitorDBCache(keys, MONITOR_CACHE_DIR)

    print("加载联系人...", flush=True)
    contact_names, contact_full = load_contacts(db_cache, keys)
    print(f"已加载 {len(contact_names)} 个联系人", flush=True)

    print("构建 username→DB 映射...", flush=True)
    username_db_map = build_username_db_map(db_cache, keys)
    print(f"已映射 {len(username_db_map)} 个用户名", flush=True)

    # 注入到 HTTP Handler（给 API 使用）
    Handler.contact_names = contact_names
    Handler.contact_full = contact_full
    Handler.db_cache = db_cache
    Handler.username_db_map = username_db_map
    try:
        Handler.self_username = detect_self_username(CONFIG_SELF_USERNAME, WECHAT_BASE_DIR, contact_names, db_cache, keys)
        if Handler.self_username:
            print(f"[self] username: {Handler.self_username}", flush=True)
    except Exception:
        Handler.self_username = CONFIG_SELF_USERNAME or ""
    try:
        Handler.persona_store = PersonaStore(PERSONA_DB)
        print(f"[store] persona.db: {PERSONA_DB}", flush=True)
    except Exception as e:
        Handler.persona_store = None
        print(f"[store] 初始化失败: {e}", flush=True)

    # 后台预热所有 message DB（图片/emoji 解密必需）
    def _warmup():
        try:
            t0 = time.perf_counter()
            warmup_keys = [os.path.join("message", "message_resource.db")]
            try:
                import re
                message_dir = os.path.join(DB_DIR, "message")
                for p in glob_mod.glob(os.path.join(message_dir, "message_*.db")):
                    base = os.path.basename(p)
                    if re.fullmatch(r"message_\d+\.db", base):
                        k = os.path.join("message", base)
                        if get_key_info(keys, k):
                            warmup_keys.append(k)
            except Exception:
                pass
            for k in warmup_keys:
                t1 = time.perf_counter()
                try:
                    db_cache.get(k)
                    print(f"[warmup] {k} {(time.perf_counter()-t1)*1000:.0f}ms", flush=True)
                except Exception as e:
                    print(f"[warmup] {k} 失败: {e}", flush=True)
        except Exception as e:
            print(f"[warmup] 异常: {e}", flush=True)
        # 构建 emoji 映射（独立解密，不走 cache）
        _build_emoji_lookup(keys)
        print(f"[warmup] 全部完成 {(time.perf_counter()-t0)*1000:.0f}ms", flush=True)
    threading.Thread(target=_warmup, daemon=True).start()

    t = threading.Thread(target=monitor_thread, args=(enc_key, session_db, contact_names, db_cache, username_db_map), daemon=True)
    t.start()

    server = ThreadedServer((LISTEN_HOST, PORT), Handler)
    url_host = "localhost" if LISTEN_HOST in ("0.0.0.0", "127.0.0.1", "::") else LISTEN_HOST
    print(f"\n=> http://{url_host}:{PORT}", flush=True)
    print("Ctrl+C 停止\n", flush=True)

    if OPEN_BROWSER:
        try:
            os.system(f'cmd.exe /c start http://{url_host}:{PORT}')
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == '__main__':
    main()
