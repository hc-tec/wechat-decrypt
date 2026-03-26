import sys

try:
    if sys.platform == "win32":
        import winreg as _winreg  # type: ignore
    else:
        _winreg = None
except Exception:  # pragma: no cover
    _winreg = None


def _require_windows():
    if sys.platform != "win32":
        raise RuntimeError("autostart 仅支持 Windows")
    if _winreg is None:
        raise RuntimeError("winreg 不可用")


def get_run_command(value_name: str) -> str:
    """读取 HKCU Run 键中的启动命令，不存在返回空字符串。"""
    _require_windows()
    value_name = (value_name or "").strip()
    if not value_name:
        return ""

    try:
        with _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _typ = _winreg.QueryValueEx(key, value_name)
            return value or ""
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def set_run_command(value_name: str, command: str) -> None:
    _require_windows()
    value_name = (value_name or "").strip()
    command = (command or "").strip()
    if not value_name:
        raise ValueError("value_name 不能为空")
    if not command:
        raise ValueError("command 不能为空")

    with _winreg.CreateKey(_winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        _winreg.SetValueEx(key, value_name, 0, _winreg.REG_SZ, command)


def delete_run_command(value_name: str) -> None:
    _require_windows()
    value_name = (value_name or "").strip()
    if not value_name:
        return

    try:
        with _winreg.OpenKey(
            _winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            _winreg.KEY_SET_VALUE,
        ) as key:
            _winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        return
    except OSError:
        return


def set_autostart_enabled(value_name: str, command: str, enabled: bool) -> None:
    if enabled:
        set_run_command(value_name, command)
    else:
        delete_run_command(value_name)


def is_autostart_enabled(value_name: str) -> bool:
    return bool(get_run_command(value_name))
