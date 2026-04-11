import os
import platform
import subprocess


def candidate_process_names(configured: str | None = None) -> list[str]:
    """Return likely WeChat process names, with the configured name first."""
    system = platform.system().lower()
    if system == "windows":
        defaults = ["Weixin.exe", "WeChat.exe"]
    elif system == "linux":
        defaults = ["wechat", "WeChatAppEx", "weixin"]
    elif system == "darwin":
        defaults = ["WeChat"]
    else:
        defaults = ["Weixin.exe", "WeChat.exe", "wechat", "WeChat"]

    names: list[str] = []
    seen: set[str] = set()

    def add_name(name: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        key = name.lower() if system == "windows" else name
        if key in seen:
            return
        seen.add(key)
        names.append(name)

    configured = os.path.basename((configured or "").strip().strip('"'))
    if configured:
        add_name(configured)
        if system == "windows" and not configured.lower().endswith(".exe"):
            add_name(configured + ".exe")

    for name in defaults:
        add_name(name)
    return names


def _windows_tasklist_contains(stdout: str, image_name: str) -> bool:
    target = (image_name or "").strip().lower()
    if not target:
        return False

    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("INFO:"):
            continue
        if line.startswith('"'):
            first = line.strip('"').split('","', 1)[0]
        else:
            first = line.split(None, 1)[0]
        if first.strip().lower() == target:
            return True
    return False


def _windows_process_running(image_name: str) -> bool:
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    try:
        proc = subprocess.run(
            ["tasklist.exe", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=creationflags,
        )
    except Exception:
        return False
    return _windows_tasklist_contains(proc.stdout, image_name)


def _pgrep_process_running(process_name: str) -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-x", process_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return proc.returncode == 0
    except Exception:
        return False


def is_wechat_running(configured_process: str | None = None) -> bool:
    """Best-effort process check used before starting the data service."""
    system = platform.system().lower()
    for name in candidate_process_names(configured_process):
        if system == "windows":
            if _windows_process_running(name):
                return True
        elif system in ("linux", "darwin"):
            if _pgrep_process_running(os.path.splitext(name)[0] if system != "windows" else name):
                return True
        else:
            if _pgrep_process_running(name):
                return True
    return False


def build_wechat_not_running_message(configured_process: str | None = None) -> str:
    configured = os.path.basename((configured_process or "").strip().strip('"'))
    display = configured or "Weixin.exe / WeChat.exe"
    msg = f"未检测到微信进程（{display}）。\n请先启动微信并登录，再启动服务。"
    if configured and configured.lower() not in {"weixin", "weixin.exe", "wechat", "wechat.exe"}:
        msg += "\n如果你的微信进程名配置不对，请在 config.json 的 wechat_process 中修正。"
    return msg
