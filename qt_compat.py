import os
import sys


def _prepare_qt_dll_search_path():
    # PyInstaller onedir + Python 3.8+ uses restricted DLL search paths when importing .pyd.
    # PyQt6 wheels ship Qt DLLs under `PyQt6/Qt6/bin`, which may not be discoverable when
    # PyQt6 package's __init__.py is loaded from base_library.zip.
    if not sys.platform.startswith("win"):
        return
    meipass = getattr(sys, "_MEIPASS", "")
    if not meipass:
        return

    candidates = [
        os.path.join(meipass, "PyQt6", "Qt6", "bin"),
        os.path.join(meipass, "PySide6", "Qt6", "bin"),
    ]
    for p in candidates:
        if os.path.isdir(p):
            try:
                os.add_dll_directory(p)
            except Exception:
                pass
            os.environ["PATH"] = p + ";" + (os.environ.get("PATH") or "")


_prepare_qt_dll_search_path()

try:
    from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore

    QT_LIB = "PyQt6"
except Exception as pyqt_exc:  # pragma: no cover
    try:
        from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore

        QT_LIB = "PySide6"
    except Exception:
        raise ImportError(f"Failed to import PyQt6 (and PySide6 is not available): {pyqt_exc}") from pyqt_exc
