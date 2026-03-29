import datetime
import os
import sys
import tempfile
import threading
import traceback


def get_log_dir(config_path: str | None = None) -> str:
    """Return a writable logs directory.

    Default location is next to config.json (same app dir), e.g.:
      %APPDATA%\\WeChatDataService\\logs
    """
    base = ""
    if config_path:
        base = os.path.dirname(os.path.abspath(os.path.expanduser(config_path)))
    else:
        try:
            from config import get_config_path

            base = os.path.dirname(os.path.abspath(get_config_path()))
        except Exception:
            base = os.getcwd()

    log_dir = os.path.join(base, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        return log_dir
    except OSError:
        # Fallback to a guaranteed-writable temp dir
        tmp = os.path.join(tempfile.gettempdir(), "WeChatDataService", "logs")
        os.makedirs(tmp, exist_ok=True)
        return tmp


class RotatingFileWriter:
    """A tiny rotating writer for plain text logs (UTF-8).

    Keeps up to `backup_count` rotated files:
      service.log, service.log.1, service.log.2, ...
    """

    def __init__(self, path: str, *, max_bytes: int = 5 * 1024 * 1024, backup_count: int = 10):
        self.path = os.path.abspath(path)
        self.max_bytes = int(max_bytes) if max_bytes and max_bytes > 0 else 0
        self.backup_count = max(0, int(backup_count or 0))
        self._lock = threading.Lock()
        self._f = None
        self._open()

    def _open(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8", errors="replace", newline="\n")

    def _rotate(self):
        if self.backup_count <= 0:
            return
        try:
            if self._f:
                self._f.flush()
                self._f.close()
        except Exception:
            pass

        for i in range(self.backup_count - 1, 0, -1):
            src = f"{self.path}.{i}"
            dst = f"{self.path}.{i + 1}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError:
                    pass

        if os.path.exists(self.path):
            try:
                os.replace(self.path, f"{self.path}.1")
            except OSError:
                pass

        self._open()

    def write(self, s: str) -> None:
        if not s:
            return
        with self._lock:
            try:
                if not self._f:
                    self._open()
                self._f.write(s)
                self._f.flush()
                if self.max_bytes > 0:
                    try:
                        size = self._f.tell()
                    except Exception:
                        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
                    if size >= self.max_bytes:
                        self._rotate()
            except Exception:
                # Never crash the app because logging failed
                pass

    def close(self):
        with self._lock:
            try:
                if self._f:
                    self._f.flush()
                    self._f.close()
            except Exception:
                pass
            self._f = None


def _ts() -> str:
    # Example: 2026-03-29 23:10:12.345
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class _LinePrefixTee:
    def __init__(self, original, writer: RotatingFileWriter, stream_name: str):
        self._orig = original
        self._writer = writer
        self._stream_name = stream_name
        self._buf = ""
        self._lock = threading.Lock()
        self.encoding = "utf-8"

    def isatty(self):
        try:
            return bool(self._orig and getattr(self._orig, "isatty", lambda: False)())
        except Exception:
            return False

    def write(self, s):
        with self._lock:
            return self._write_locked(s)

    def _write_locked(self, s):
        try:
            text = s if isinstance(s, str) else str(s)
        except Exception:
            text = ""
        if not text:
            return 0

        try:
            if self._orig:
                self._orig.write(text)
        except Exception:
            pass

        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._writer.write(f"{_ts()} [{self._stream_name}] {line}\n")
        return len(text)

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        try:
            if self._orig:
                self._orig.flush()
        except Exception:
            pass
        if self._buf:
            self._writer.write(f"{_ts()} [{self._stream_name}] {self._buf}\n")
            self._buf = ""


_installed = False
_writer: RotatingFileWriter | None = None
_log_path = ""
_log_dir = ""
_orig_stdout = None
_orig_stderr = None
_orig_excepthook = None


def init_app_logging(
    component: str,
    *,
    config_path: str | None = None,
    redirect_stdio: bool = True,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 10,
) -> tuple[str, str]:
    """Initialize persistent logging. Returns (log_dir, log_path)."""
    global _installed, _writer, _log_dir, _log_path, _orig_stdout, _orig_stderr, _orig_excepthook

    if _installed and _log_dir and _log_path:
        return _log_dir, _log_path

    _log_dir = get_log_dir(config_path)
    safe = "".join(c for c in (component or "app") if c.isalnum() or c in ("-", "_")).strip("._-") or "app"
    _log_path = os.path.join(_log_dir, f"{safe}.log")
    _writer = RotatingFileWriter(_log_path, max_bytes=max_bytes, backup_count=backup_count)

    _orig_excepthook = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            _writer.write(f"{_ts()} [exception] Unhandled exception\n")
            _writer.write("".join(traceback.format_exception(exc_type, exc, tb)))
            _writer.write("\n")
        except Exception:
            pass
        try:
            if _orig_excepthook:
                _orig_excepthook(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _hook

    if redirect_stdio:
        _orig_stdout = getattr(sys, "stdout", None)
        _orig_stderr = getattr(sys, "stderr", None)
        sys.stdout = _LinePrefixTee(_orig_stdout, _writer, "stdout")
        sys.stderr = _LinePrefixTee(_orig_stderr, _writer, "stderr")

    _installed = True
    try:
        _writer.write(
            f"{_ts()} [startup] component={safe} pid={os.getpid()} "
            f"python={sys.version.split()[0]} frozen={bool(getattr(sys, 'frozen', False))}\n"
        )
        if config_path:
            _writer.write(f"{_ts()} [startup] config_path={os.path.abspath(os.path.expanduser(config_path))}\n")
    except Exception:
        pass
    return _log_dir, _log_path


def get_current_log_dir() -> str:
    return _log_dir


def get_current_log_path() -> str:
    return _log_path


def write_log_line(line: str, *, tag: str = "app") -> None:
    if not line:
        return
    w = _writer
    if not w:
        return
    safe = "".join(c for c in (tag or "app") if c.isalnum() or c in ("-", "_")).strip("._-") or "app"
    w.write(f"{_ts()} [{safe}] {line.rstrip()}\n")
