import os
import sys
import urllib.request

from autostart import get_run_command, set_autostart_enabled
from config import get_config_path, load_config_soft, read_config_file, write_config_file
from qt_compat import QT_LIB, QtCore, QtGui, QtWidgets


APP_TITLE = "WeChat Data Service"
AUTOSTART_VALUE_NAME = "WeChatDataService"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_exe_dir() -> str:
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _quote_cmd_arg(path: str) -> str:
    path = path or ""
    if not path:
        return ""
    if " " in path or "\t" in path:
        return f"\"{path}\""
    return path


def _find_service_exe() -> str:
    if not _is_frozen():
        return ""

    gui_dir = _app_exe_dir()
    parent = os.path.dirname(gui_dir)
    candidates = [
        os.path.join(gui_dir, "WeChatDataService.exe"),
        os.path.join(gui_dir, "Service", "WeChatDataService.exe"),
        os.path.join(parent, "WeChatDataService.exe"),
        os.path.join(parent, "Service", "WeChatDataService.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return ""


def _can_open_url(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WeChatDataServiceGUI"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, auto_start_service: bool = False):
        super().__init__()

        self.setWindowTitle(f"{APP_TITLE} ({QT_LIB})")
        self.resize(980, 680)

        self._really_quit = False
        self._auto_start_service = bool(auto_start_service)

        self._config_path = get_config_path()
        self._cfg = load_config_soft(self._config_path)

        self._proc = QtCore.QProcess(self)
        self._proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._on_proc_output)
        self._proc.stateChanged.connect(self._on_proc_state_changed)
        self._proc.finished.connect(self._on_proc_finished)

        self._tray = None
        self._setup_tray()

        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(1500)
        self._poll_timer.timeout.connect(self._poll_health)

        self._build_ui()
        self._apply_theme()
        self._refresh_ui_from_config()
        self._refresh_autostart_ui()

        if self._auto_start_service:
            QtCore.QTimer.singleShot(200, self.start_service)
            QtCore.QTimer.singleShot(300, self._hide_to_tray)

    # ---------------- UI ----------------

    def _build_ui(self):
        root = QtWidgets.QWidget(self)
        self.setCentralWidget(root)

        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        # Header
        header = QtWidgets.QHBoxLayout()
        self._title = QtWidgets.QLabel(APP_TITLE)
        self._title.setObjectName("title")
        header.addWidget(self._title)
        header.addStretch(1)

        self._status = QtWidgets.QLabel("● 未启动")
        self._status.setObjectName("status")
        header.addWidget(self._status)
        layout.addLayout(header)

        # Service controls
        svc_group = QtWidgets.QGroupBox("服务")
        svc_l = QtWidgets.QGridLayout(svc_group)
        svc_l.setHorizontalSpacing(10)
        svc_l.setVerticalSpacing(8)

        self._btn_start = QtWidgets.QPushButton("启动服务")
        self._btn_stop = QtWidgets.QPushButton("停止服务")
        self._btn_open = QtWidgets.QPushButton("打开 Web UI")
        self._btn_test = QtWidgets.QPushButton("探活 /api/v1/health")

        self._btn_start.clicked.connect(self.start_service)
        self._btn_stop.clicked.connect(self.stop_service)
        self._btn_open.clicked.connect(self.open_web_ui)
        self._btn_test.clicked.connect(self.test_health)

        self._url = QtWidgets.QLineEdit()
        self._url.setReadOnly(True)
        self._btn_copy = QtWidgets.QPushButton("复制")
        self._btn_copy.clicked.connect(self.copy_url)

        svc_l.addWidget(self._btn_start, 0, 0)
        svc_l.addWidget(self._btn_stop, 0, 1)
        svc_l.addWidget(self._btn_open, 0, 2)
        svc_l.addWidget(self._btn_test, 0, 3)

        svc_l.addWidget(QtWidgets.QLabel("地址"), 1, 0)
        svc_l.addWidget(self._url, 1, 1, 1, 2)
        svc_l.addWidget(self._btn_copy, 1, 3)

        self._hint = QtWidgets.QLabel(
            "提示：首次运行提取密钥可能需要管理员权限（右键“以管理员身份运行”）。"
        )
        self._hint.setWordWrap(True)
        self._hint.setObjectName("hint")
        svc_l.addWidget(self._hint, 2, 0, 1, 4)

        layout.addWidget(svc_group)

        # Config
        cfg_group = QtWidgets.QGroupBox("配置")
        cfg_l = QtWidgets.QGridLayout(cfg_group)
        cfg_l.setHorizontalSpacing(10)
        cfg_l.setVerticalSpacing(8)

        self._cfg_file = QtWidgets.QLineEdit(self._config_path)
        self._cfg_file.setReadOnly(True)
        self._btn_open_cfg = QtWidgets.QPushButton("打开配置文件夹")
        self._btn_open_cfg.clicked.connect(self.open_config_folder)

        self._db_dir = QtWidgets.QLineEdit()
        self._btn_pick_db = QtWidgets.QPushButton("选择…")
        self._btn_pick_db.clicked.connect(self.pick_db_dir)

        self._listen_host = QtWidgets.QLineEdit()
        self._listen_port = QtWidgets.QSpinBox()
        self._listen_port.setRange(1, 65535)

        self._open_browser = QtWidgets.QCheckBox("启动后自动打开浏览器")

        self._api_token = QtWidgets.QLineEdit()
        self._api_token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self._self_username = QtWidgets.QLineEdit()

        self._btn_save = QtWidgets.QPushButton("保存配置")
        self._btn_save.clicked.connect(self.save_config)

        self._chk_autostart = QtWidgets.QCheckBox("开机自启（登录后后台运行）")
        self._chk_autostart.toggled.connect(self.on_toggle_autostart)

        self._btn_admin = QtWidgets.QPushButton("以管理员身份重启")
        self._btn_admin.clicked.connect(self.restart_as_admin)

        row = 0
        cfg_l.addWidget(QtWidgets.QLabel("config.json"), row, 0)
        cfg_l.addWidget(self._cfg_file, row, 1, 1, 2)
        cfg_l.addWidget(self._btn_open_cfg, row, 3)

        row += 1
        cfg_l.addWidget(QtWidgets.QLabel("db_dir"), row, 0)
        cfg_l.addWidget(self._db_dir, row, 1, 1, 2)
        cfg_l.addWidget(self._btn_pick_db, row, 3)

        row += 1
        cfg_l.addWidget(QtWidgets.QLabel("listen_host"), row, 0)
        cfg_l.addWidget(self._listen_host, row, 1)
        cfg_l.addWidget(QtWidgets.QLabel("listen_port"), row, 2)
        cfg_l.addWidget(self._listen_port, row, 3)

        row += 1
        cfg_l.addWidget(self._open_browser, row, 1, 1, 2)
        cfg_l.addWidget(self._btn_save, row, 3)

        row += 1
        cfg_l.addWidget(QtWidgets.QLabel("api_token（可选）"), row, 0)
        cfg_l.addWidget(self._api_token, row, 1, 1, 3)

        row += 1
        cfg_l.addWidget(QtWidgets.QLabel("self_username（可选）"), row, 0)
        cfg_l.addWidget(self._self_username, row, 1, 1, 3)

        row += 1
        cfg_l.addWidget(self._chk_autostart, row, 1, 1, 2)
        cfg_l.addWidget(self._btn_admin, row, 3)

        layout.addWidget(cfg_group)

        # Logs
        log_group = QtWidgets.QGroupBox("日志")
        log_l = QtWidgets.QVBoxLayout(log_group)
        self._log = QtWidgets.QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        log_l.addWidget(self._log)
        layout.addWidget(log_group, 1)

    def _apply_theme(self):
        self.setStyleSheet(
            """
            QMainWindow { background: #0b0d12; }
            QLabel#title { font-size: 20px; font-weight: 700; color: #e8eaf0; }
            QLabel#status { font-size: 13px; color: #c7cbe0; }
            QLabel#hint { color: #9aa3c7; }
            QGroupBox {
                color: #e8eaf0;
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 10px;
                margin-top: 10px;
                padding: 12px;
                background: rgba(255,255,255,0.03);
            }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
            QLineEdit, QPlainTextEdit, QSpinBox {
                background: rgba(255,255,255,0.06);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 8px;
                padding: 7px;
                color: #e8eaf0;
            }
            QPushButton {
                background: rgba(79,195,247,0.14);
                border: 1px solid rgba(79,195,247,0.35);
                border-radius: 8px;
                padding: 8px 12px;
                color: #e8eaf0;
            }
            QPushButton:hover { background: rgba(79,195,247,0.20); }
            QPushButton:pressed { background: rgba(79,195,247,0.12); }
            QCheckBox { color: #e8eaf0; }
            """
        )

    # ---------------- Config ----------------

    def _refresh_ui_from_config(self):
        cfg = load_config_soft(self._config_path)
        self._cfg = cfg

        self._db_dir.setText(cfg.get("db_dir", "") or "")
        self._listen_host.setText(cfg.get("listen_host", "127.0.0.1") or "127.0.0.1")
        try:
            self._listen_port.setValue(int(cfg.get("listen_port", 5678) or 5678))
        except Exception:
            self._listen_port.setValue(5678)
        self._open_browser.setChecked(bool(cfg.get("open_browser", True)))
        self._api_token.setText(cfg.get("api_token", "") or "")
        self._self_username.setText(cfg.get("self_username", "") or "")

        host = (cfg.get("listen_host") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(cfg.get("listen_port") or 5678)
        url_host = "localhost" if host in ("0.0.0.0", "127.0.0.1", "::") else host
        self._url.setText(f"http://{url_host}:{port}")

        if cfg.get("_setup_required"):
            self._hint.setText("检测到未配置 db_dir：请先选择微信 db_storage 目录，然后保存配置。")
        else:
            self._hint.setText("提示：首次运行提取密钥可能需要管理员权限（右键“以管理员身份运行”）。")

    def save_config(self):
        cfg = read_config_file(self._config_path)
        if not isinstance(cfg, dict):
            cfg = {}

        cfg["db_dir"] = (self._db_dir.text() or "").strip()
        cfg["listen_host"] = (self._listen_host.text() or "").strip() or "127.0.0.1"
        cfg["listen_port"] = int(self._listen_port.value())
        cfg["open_browser"] = bool(self._open_browser.isChecked())
        cfg["api_token"] = (self._api_token.text() or "").strip()
        cfg["self_username"] = (self._self_username.text() or "").strip()

        try:
            write_config_file(cfg, self._config_path)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "保存失败", str(e))
            return

        self._append_log(f"[gui] 已保存配置: {self._config_path}")
        self._refresh_ui_from_config()

    def pick_db_dir(self):
        start = self._db_dir.text().strip() or os.path.expanduser("~")
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择 db_storage 目录", start)
        if d:
            self._db_dir.setText(d)

    def open_config_folder(self):
        folder = os.path.dirname(os.path.abspath(self._config_path))
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))

    # ---------------- Service ----------------

    def start_service(self):
        if self._proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            return

        self.save_config()
        cfg = load_config_soft(self._config_path)
        db_dir = (cfg.get("db_dir") or "").strip()
        if not db_dir or not os.path.isdir(db_dir):
            QtWidgets.QMessageBox.warning(self, "配置不完整", "db_dir 无效，请先选择微信 db_storage 目录。")
            return

        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        self._proc.setProcessEnvironment(env)

        if _is_frozen():
            service_exe = _find_service_exe()
            if not service_exe:
                QtWidgets.QMessageBox.critical(self, "启动失败", "未找到 WeChatDataService.exe（请确认已正确安装/解压）。")
                return
            self._proc.setProgram(service_exe)
            self._proc.setArguments([])
            self._proc.setWorkingDirectory(os.path.dirname(service_exe))
            self._append_log(f"[gui] start: {service_exe}")
        else:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            self._proc.setProgram(sys.executable)
            self._proc.setArguments(["-u", os.path.join(script_dir, "main.py")])
            self._proc.setWorkingDirectory(script_dir)
            self._append_log(f"[gui] start: {sys.executable} -u main.py")

        self._proc.start()
        self._poll_timer.start()
        self._update_status("● 启动中…", ok=False)

    def stop_service(self):
        if self._proc.state() == QtCore.QProcess.ProcessState.NotRunning:
            return
        self._append_log("[gui] stop requested")
        self._proc.terminate()
        QtCore.QTimer.singleShot(2200, self._kill_if_needed)

    def _kill_if_needed(self):
        if self._proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._append_log("[gui] kill")
            self._proc.kill()

    def open_web_ui(self):
        url = self._url.text().strip()
        if url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def test_health(self):
        url = self._url.text().strip().rstrip("/")
        ok = _can_open_url(url + "/api/v1/health")
        QtWidgets.QMessageBox.information(self, "探活结果", "OK" if ok else "失败（服务未启动或端口不可达）")

    def copy_url(self):
        QtWidgets.QApplication.clipboard().setText(self._url.text().strip())

    # ---------------- Autostart ----------------

    def _refresh_autostart_ui(self):
        cmd = get_run_command(AUTOSTART_VALUE_NAME)
        self._chk_autostart.blockSignals(True)
        self._chk_autostart.setChecked(bool(cmd))
        self._chk_autostart.blockSignals(False)

    def on_toggle_autostart(self, checked: bool):
        if not _is_frozen():
            QtWidgets.QMessageBox.information(self, "提示", "开发模式下不建议写入开机自启。请使用安装包版本。")
            self._refresh_autostart_ui()
            return

        gui_exe = os.path.abspath(sys.executable)
        command = f"{_quote_cmd_arg(gui_exe)} --autostart"
        try:
            set_autostart_enabled(AUTOSTART_VALUE_NAME, command, bool(checked))
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "设置失败", str(e))
        self._refresh_autostart_ui()

    # ---------------- Tray ----------------

    def _setup_tray(self):
        tray = QtWidgets.QSystemTrayIcon(self)
        tray.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon))
        tray.setToolTip(APP_TITLE)

        menu = QtWidgets.QMenu()
        act_show = menu.addAction("显示")
        act_show.triggered.connect(self.show_and_raise)
        menu.addSeparator()
        act_start = menu.addAction("启动服务")
        act_start.triggered.connect(self.start_service)
        act_stop = menu.addAction("停止服务")
        act_stop.triggered.connect(self.stop_service)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self.quit_app)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()

        self._tray = tray

    def _on_tray_activated(self, reason):
        try:
            trigger = QtWidgets.QSystemTrayIcon.ActivationReason.Trigger
            if reason == trigger:
                self.show_and_raise()
        except Exception:
            pass

    def show_and_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _hide_to_tray(self):
        self.hide()
        if self._tray:
            self._tray.showMessage(APP_TITLE, "已最小化到托盘（右键可退出）", QtWidgets.QSystemTrayIcon.MessageIcon.Information, 2500)

    def closeEvent(self, event):
        if self._really_quit:
            event.accept()
            return
        self._hide_to_tray()
        event.ignore()

    def quit_app(self):
        self._really_quit = True
        try:
            self.stop_service()
        except Exception:
            pass
        QtCore.QTimer.singleShot(600, QtWidgets.QApplication.quit)

    # ---------------- Admin restart ----------------

    def restart_as_admin(self):
        if not sys.platform.startswith("win"):
            return
        try:
            import ctypes

            exe = sys.executable if _is_frozen() else sys.executable
            args = "--autostart" if self._auto_start_service else ""
            params = args
            ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
            self.quit_app()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "失败", str(e))

    # ---------------- Process callbacks ----------------

    def _on_proc_output(self):
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        if data:
            self._append_log(data.rstrip("\n"))

    def _on_proc_state_changed(self, _state):
        st = self._proc.state()
        if st == QtCore.QProcess.ProcessState.Running:
            self._update_status("● 运行中", ok=True)
            self._btn_start.setEnabled(False)
            self._btn_stop.setEnabled(True)
        elif st == QtCore.QProcess.ProcessState.NotRunning:
            self._update_status("● 未启动", ok=False)
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)

    def _on_proc_finished(self, exit_code, exit_status):
        self._poll_timer.stop()
        self._update_status(f"● 已退出 (code={exit_code})", ok=False)
        self._append_log(f"[gui] exited: code={exit_code} status={exit_status}")

    def _poll_health(self):
        url = self._url.text().strip().rstrip("/") + "/api/v1/health"
        ok = _can_open_url(url)
        if ok and self._proc.state() == QtCore.QProcess.ProcessState.Running:
            self._update_status("● 运行中（可达）", ok=True)
        elif self._proc.state() == QtCore.QProcess.ProcessState.Running:
            self._update_status("● 运行中（不可达）", ok=False)

    # ---------------- helpers ----------------

    def _update_status(self, text: str, ok: bool):
        self._status.setText(text)
        color = "#81c784" if ok else "#ffb74d"
        self._status.setStyleSheet(f"color: {color};")

    def _append_log(self, line: str):
        self._log.appendPlainText(line)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    auto = "--autostart" in argv

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    win = MainWindow(auto_start_service=auto)
    win.show()
    return int(app.exec())
