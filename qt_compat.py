import os
import sys


_DLL_DIR_HANDLES: list[object] = []
_QT_BIN_DIRS: list[str] = []


def _qt_debug_enabled() -> bool:
    return (os.environ.get("WECHAT_DECRYPT_QT_DEBUG") or "").strip() not in ("", "0", "false", "False")


def _qt_debug(msg: str) -> None:
    if not _qt_debug_enabled():
        return
    try:
        print(f"[qt_compat] {msg}", file=sys.stderr)
    except Exception:
        pass


def _prefer_system_icu() -> None:
    # Some build environments (e.g. Anaconda) may cause PyInstaller to collect a full ICU build
    # (icuuc.dll/icudt*.dll) into the frozen app. Qt6Core.dll may fail to load against those ICU
    # DLLs; but Windows 10/11 ship their own ICU (or stubs) in System32 that works for Qt.
    # Pre-loading System32 ICU helps ensure later dependency resolution uses the system copy
    # instead of the bundled one (even if the bundled DLL exists).
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes  # pylint: disable=import-outside-toplevel
    except Exception:
        return

    sysroot = (os.environ.get("SystemRoot") or r"C:\Windows").strip() or r"C:\Windows"
    system32 = os.path.join(sysroot, "System32")
    icuuc = os.path.join(system32, "icuuc.dll")
    if os.path.isfile(icuuc):
        try:
            ctypes.WinDLL(icuuc)
            _qt_debug(f"preloaded system ICU: {icuuc}")
        except OSError as e:
            _qt_debug(f"failed to preload system ICU: {e}")


def _disable_bundled_icu() -> None:
    # Best-effort mitigation: if a full ICU DLL is bundled next to the frozen python runtime,
    # rename it so that Qt6Core.dll does not pick it up preferentially.
    if not sys.platform.startswith("win"):
        return

    base = getattr(sys, "_MEIPASS", "") or ""
    if not base or not os.path.isdir(base):
        return

    try:
        import glob  # pylint: disable=import-outside-toplevel
    except Exception:
        return

    # Only touch large "full ICU" DLLs; leave small system stubs alone.
    size_threshold = 200_000
    for p in glob.glob(os.path.join(base, "icu*.dll")):
        try:
            if not os.path.isfile(p):
                continue
            if os.path.isfile(p + ".disabled"):
                continue
            if os.path.getsize(p) < size_threshold:
                continue
            os.replace(p, p + ".disabled")
            _qt_debug(f"disabled bundled ICU: {os.path.basename(p)}")
        except OSError as e:
            _qt_debug(f"failed to disable bundled ICU {p}: {e}")
        except Exception:
            pass


def _prepare_qt_dll_search_path():
    # PyInstaller onedir + Python 3.8+ uses restricted DLL search paths when importing .pyd.
    # PyQt6 wheels ship Qt DLLs under `PyQt6/Qt6/bin`, which may not be discoverable when
    # PyQt6 package's __init__.py is loaded from base_library.zip.
    if not sys.platform.startswith("win"):
        return

    _prefer_system_icu()
    _disable_bundled_icu()

    roots: list[str] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        roots.append(meipass)
        roots.append(os.path.join(meipass, "_internal"))

    exe_dir = os.path.dirname(getattr(sys, "executable", "") or "")
    if exe_dir:
        roots.append(exe_dir)
        roots.append(os.path.join(exe_dir, "_internal"))

    seen: set[str] = set()
    existing_roots: list[str] = []
    for r in roots:
        if not r:
            continue
        r = os.path.normpath(r)
        if r in seen:
            continue
        seen.add(r)
        if os.path.isdir(r):
            existing_roots.append(r)

    candidates: list[str] = []
    plugin_candidates: list[str] = []
    for root in existing_roots:
        candidates.extend(
            [
                os.path.join(root, "PyQt6", "Qt6", "bin"),
                os.path.join(root, "PySide6", "Qt6", "bin"),
            ]
        )
        plugin_candidates.extend(
            [
                os.path.join(root, "PyQt6", "Qt6", "plugins"),
                os.path.join(root, "PySide6", "Qt6", "plugins"),
            ]
        )

    for p in candidates:
        if os.path.isdir(p):
            if p not in _QT_BIN_DIRS:
                _QT_BIN_DIRS.append(p)
            try:
                handle = os.add_dll_directory(p)
                _DLL_DIR_HANDLES.append(handle)
            except Exception:
                pass
            os.environ["PATH"] = p + ";" + (os.environ.get("PATH") or "")

    # Ensure Qt platform plugins can be found in frozen builds.
    # (Only set if not already set by user/environment.)
    for plugins_dir in plugin_candidates:
        if not os.path.isdir(plugins_dir):
            continue
        existing = os.environ.get("QT_PLUGIN_PATH") or ""
        if not existing:
            os.environ["QT_PLUGIN_PATH"] = plugins_dir
        elif plugins_dir not in existing.split(os.pathsep):
            os.environ["QT_PLUGIN_PATH"] = plugins_dir + os.pathsep + existing

        platforms = os.path.join(plugins_dir, "platforms")
        if os.path.isdir(platforms):
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", platforms)

    if _qt_debug_enabled():
        _qt_debug(f"sys._MEIPASS={getattr(sys, '_MEIPASS', '')!r}")
        _qt_debug(f"sys.executable={getattr(sys, 'executable', '')!r}")
        _qt_debug(f"qt_bin_dirs={_QT_BIN_DIRS!r}")
        _qt_debug(f"QT_PLUGIN_PATH={os.environ.get('QT_PLUGIN_PATH','')!r}")
        _qt_debug(f"QT_QPA_PLATFORM_PLUGIN_PATH={os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH','')!r}")


def _preload_qt6_dlls() -> None:
    # Best-effort: pre-load Qt6 DLLs via absolute paths so that importing PyQt6 .pyd
    # can resolve its dependencies even under restricted DLL search paths.
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes  # pylint: disable=import-outside-toplevel
    except Exception:
        return

    names = ["Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll"]
    for bin_dir in list(_QT_BIN_DIRS):
        for name in names:
            dll_path = os.path.join(bin_dir, name)
            if not os.path.isfile(dll_path):
                continue
            try:
                ctypes.WinDLL(dll_path)
                _qt_debug(f"preloaded {dll_path}")
            except OSError as e:
                _qt_debug(f"failed to preload {dll_path}: {e}")


_prepare_qt_dll_search_path()

try:
    from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore

    QT_LIB = "PyQt6"
except Exception as pyqt_exc:  # pragma: no cover
    _preload_qt6_dlls()
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore

        QT_LIB = "PyQt6"
    except Exception:
        pass
    try:
        from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore

        QT_LIB = "PySide6"
    except Exception:
        raise ImportError(f"Failed to import PyQt6 (and PySide6 is not available): {pyqt_exc}") from pyqt_exc
