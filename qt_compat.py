try:
    from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore

    QT_LIB = "PyQt6"
except Exception:  # pragma: no cover
    from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore

    QT_LIB = "PySide6"

