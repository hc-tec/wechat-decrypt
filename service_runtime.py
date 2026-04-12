import errno
import json
import os
import socket
import sys
import tempfile
import urllib.request


SERVICE_EXIT_ALREADY_RUNNING = 20
SERVICE_EXIT_PORT_CONFLICT = 21


def _normalize_listen_host(listen_host: str | None) -> str:
    host = (listen_host or "").strip()
    return host or "127.0.0.1"


def build_service_base_url(listen_host: str | None, listen_port: int | str | None) -> str:
    host = _normalize_listen_host(listen_host)
    if host in ("0.0.0.0", "127.0.0.1", "::", "::1"):
        host = "localhost"
    return f"http://{host}:{int(listen_port or 5678)}"


def build_service_already_running_message(listen_host: str | None, listen_port: int | str | None) -> str:
    return f"检测到服务已在运行：{build_service_base_url(listen_host, listen_port)}"


def build_port_conflict_message(listen_host: str | None, listen_port: int | str | None) -> str:
    return (
        f"监听端口已被其他程序占用：{build_service_base_url(listen_host, listen_port)}\n"
        "请修改 listen_port，或先关闭占用该端口的程序。"
    )


def _can_open_health(url: str, timeout: float = 0.8) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WeChatDataService"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status) == 200
    except Exception:
        return False


def _probe_socket_host(listen_host: str | None) -> str:
    host = _normalize_listen_host(listen_host)
    if host in ("0.0.0.0", "::", "::1", "localhost"):
        return "127.0.0.1"
    return host


def probe_local_service_state(
    listen_host: str | None,
    listen_port: int | str | None,
    *,
    timeout: float = 0.8,
) -> str:
    base_url = build_service_base_url(listen_host, listen_port).rstrip("/")
    if _can_open_health(base_url + "/api/v1/health", timeout=timeout):
        return "service"

    try:
        with socket.create_connection((_probe_socket_host(listen_host), int(listen_port or 5678)), timeout=timeout):
            return "occupied"
    except OSError:
        return "free"


def is_address_in_use_error(exc: BaseException) -> bool:
    err_no = getattr(exc, "errno", None)
    if err_no in {errno.EADDRINUSE, 10048}:
        return True
    text = str(exc).lower()
    return ("address already in use" in text) or ("only one usage of each socket address" in text)


def _lock_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "WeChatDataService", "locks")
    os.makedirs(path, exist_ok=True)
    return path


def _service_lock_name(listen_host: str | None, listen_port: int | str | None) -> str:
    host = _normalize_listen_host(listen_host).replace(":", "_").replace(".", "_")
    return f"service_{host}_{int(listen_port or 5678)}.lock"


class ServiceInstanceGuard:
    def __init__(self, listen_host: str | None, listen_port: int | str | None, *, config_path: str = ""):
        self._listen_host = _normalize_listen_host(listen_host)
        self._listen_port = int(listen_port or 5678)
        self._config_path = os.path.abspath(config_path or "")
        self._path = os.path.join(_lock_dir(), _service_lock_name(self._listen_host, self._listen_port))
        self._fp = None

    def acquire(self) -> bool:
        if self._fp is not None:
            return True

        fp = open(self._path, "a+b")
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                fp.seek(0, os.SEEK_END)
                if fp.tell() == 0:
                    fp.write(b"0")
                    fp.flush()
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            fp.seek(0)
            fp.truncate()
            fp.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "listen_host": self._listen_host,
                        "listen_port": self._listen_port,
                        "config_path": self._config_path,
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            fp.flush()
            self._fp = fp
            return True
        except OSError:
            try:
                fp.close()
            except Exception:
                pass
            return False

    def release(self) -> None:
        fp = self._fp
        self._fp = None
        if fp is None:
            return
        try:
            if sys.platform.startswith("win"):
                import msvcrt

                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fp.close()
        except Exception:
            pass
