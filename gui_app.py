import os
import sys
import json
import urllib.request
import re

from autostart import get_run_command, set_autostart_enabled
from config import get_config_path, load_config_soft, read_config_file, write_config_file
from log_utils import get_current_log_dir, get_current_log_path, init_app_logging, write_log_line
from qt_compat import QT_LIB, QtCore, QtGui, QtWidgets


APP_TITLE = "WeChat Data Service"
AUTOSTART_VALUE_NAME = "WeChatDataService"
_RE_ACCOUNT_DIR_WITH_SUFFIX = re.compile(r"(.+)_([0-9a-fA-F]{4,})$")


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
        # 开发构建输出（PyInstaller onedir）：dist\\WeChatDataServiceGUI + dist\\WeChatDataService
        os.path.join(parent, "WeChatDataService", "WeChatDataService.exe"),
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


def _get_json(url: str, *, timeout: float = 1.5) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WeChatDataServiceGUI"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = resp.read()
        obj = json.loads(body.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


_Signal = getattr(QtCore, "pyqtSignal", None)
if _Signal is None:
    _Signal = getattr(QtCore, "Signal", None)


class _HealthEmitter(QtCore.QObject):
    done = _Signal(bool, object) if _Signal else None  # type: ignore[misc]


class _HealthWorker(QtCore.QRunnable):
    def __init__(self, emitter: _HealthEmitter, base_url: str):
        super().__init__()
        self._emitter = emitter
        self._base_url = base_url

    def run(self):
        ok = False
        state = None
        try:
            ok = _can_open_url(self._base_url + "/api/v1/health")
            if ok:
                state = _get_json(self._base_url + "/api/v1/state", timeout=1.0)
        except Exception:
            ok = False
            state = None
        try:
            if getattr(self._emitter, "done", None):
                self._emitter.done.emit(bool(ok), state)
        except Exception:
            pass


def _suggest_self_username_from_db_dir(db_dir: str) -> str:
    db_dir = (db_dir or "").strip()
    if not db_dir:
        return ""

    base_dir = db_dir
    if os.path.basename(base_dir) == "db_storage":
        base_dir = os.path.dirname(base_dir)

    account_dir = os.path.basename(base_dir).strip()
    if not account_dir:
        return ""

    m = _RE_ACCOUNT_DIR_WITH_SUFFIX.fullmatch(account_dir)
    if m:
        return m.group(1)

    return account_dir


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, auto_start_service: bool = False):
        super().__init__()

        self.setWindowTitle(f"{APP_TITLE} ({QT_LIB})")

        self._really_quit = False
        self._auto_start_service = bool(auto_start_service)

        self._config_path = get_config_path()
        init_app_logging("gui", config_path=self._config_path)
        self._log_dir = get_current_log_dir()
        self._log_path = get_current_log_path()
        self._cfg = load_config_soft(self._config_path)

        self._proc = QtCore.QProcess(self)
        self._proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.SeparateChannels)
        try:
            self._proc.errorOccurred.connect(self._on_proc_error)  # type: ignore[attr-defined]
        except Exception:
            pass
        self._proc.stateChanged.connect(self._on_proc_state_changed)
        self._proc.finished.connect(self._on_proc_finished)

        self._tray = None
        self._setup_tray()

        self._state_self_username = ""
        self._stop_requested = False

        self._health_pending = False
        self._health_emitter = _HealthEmitter()
        try:
            self._health_emitter.done.connect(self._on_health_done)  # type: ignore[union-attr]
        except Exception:
            pass

        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(1500)
        self._poll_timer.timeout.connect(self._poll_health)

        self._build_ui()
        self._apply_theme()
        self._apply_initial_window_geometry()
        self._refresh_ui_from_config()
        self._refresh_autostart_ui()

        if self._auto_start_service:
            QtCore.QTimer.singleShot(200, self.start_service)
            QtCore.QTimer.singleShot(300, self._hide_to_tray)

    # ---------------- UI ----------------

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self._ensure_within_screen)

    def _ensure_within_screen(self):
        g = self._get_available_screen_geometry()
        if not g:
            return

        margin = 24
        max_w = max(320, int(g.width() - margin))
        max_h = max(240, int(g.height() - margin))

        w = min(int(self.width()), max_w)
        h = min(int(self.height()), max_h)
        if w != self.width() or h != self.height():
            self.resize(w, h)

        try:
            x = int(self.x())
            y = int(self.y())
            x = max(int(g.x()), min(x, int(g.x() + g.width() - w)))
            y = max(int(g.y()), min(y, int(g.y() + g.height() - h)))
            self.move(x, y)
        except Exception:
            pass

    def _get_available_screen_geometry(self):
        try:
            screen = None
            try:
                screen = self.screen()
            except Exception:
                screen = None
            if not screen:
                try:
                    screen = QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
                except Exception:
                    screen = None
            if not screen:
                screen = QtWidgets.QApplication.primaryScreen()
            return screen.availableGeometry() if screen else None
        except Exception:
            return None

    def _apply_initial_window_geometry(self):
        # 避免固定尺寸在高 DPI 缩放下“变成超大窗口”，导致内容超出屏幕。
        g = self._get_available_screen_geometry()
        if not g:
            self.resize(980, 680)
            return

        # 预留足够边距给窗口边框/任务栏/多屏场景，避免“窗口比屏幕还高”。
        margin = 96
        max_w = max(420, int(g.width() - margin))
        max_h = max(320, int(g.height() - margin))

        # 初始窗口尽量“不会遮住屏幕”，同时在大屏上保持紧凑。
        target_w = min(980, int(max_w * 0.98))
        target_h = min(560, int(max_h * 0.70))

        min_w = min(760, max_w)
        min_h = min(420, max_h)

        target_w = min(max_w, max(min_w, target_w))
        target_h = min(max_h, max(min_h, target_h))

        self.resize(target_w, target_h)
        try:
            self.move(
                int(g.x() + (g.width() - target_w) / 2),
                int(g.y() + (g.height() - target_h) / 2),
            )
        except Exception:
            pass

        try:
            avail_h = int(g.height())
            guide_min = max(90, int(avail_h * 0.14))
            guide_max = max(guide_min, int(avail_h * 0.20))
            guide_max = min(200, guide_max)
            self._guide.setMinimumHeight(guide_min)
            self._guide.setMaximumHeight(guide_max)
        except Exception:
            pass

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

        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs, 1)

        # ---------------- 控制页（可滚动） ----------------
        tab_control = QtWidgets.QWidget()
        tabs.addTab(tab_control, "控制")
        tab_control_l = QtWidgets.QVBoxLayout(tab_control)
        tab_control_l.setContentsMargins(0, 0, 0, 0)
        tab_control_l.setSpacing(0)

        control_scroll = QtWidgets.QScrollArea()
        control_scroll.setWidgetResizable(True)
        control_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        tab_control_l.addWidget(control_scroll)

        control_root = QtWidgets.QWidget()
        control_scroll.setWidget(control_root)

        control_l = QtWidgets.QVBoxLayout(control_root)
        control_l.setContentsMargins(0, 0, 0, 0)
        control_l.setSpacing(12)

        # Autostart (top priority for non-technical users)
        autostart_group = QtWidgets.QGroupBox("开机自启")
        autostart_l = QtWidgets.QHBoxLayout(autostart_group)
        autostart_l.setContentsMargins(12, 10, 12, 10)
        autostart_l.setSpacing(10)

        self._chk_autostart = QtWidgets.QCheckBox("开机自启（登录后自动后台运行）")
        self._chk_autostart.toggled.connect(self.on_toggle_autostart)
        autostart_l.addWidget(self._chk_autostart)
        autostart_l.addStretch(1)
        control_l.addWidget(autostart_group)

        # Guide
        guide_group = QtWidgets.QGroupBox("使用引导（常见问题与小技巧）")
        guide_l = QtWidgets.QVBoxLayout(guide_group)
        self._guide = QtWidgets.QTextBrowser()
        self._guide.setOpenExternalLinks(True)
        self._guide.setReadOnly(True)
        # 该区域需要“初次可见”；具体高度会在 `_apply_initial_window_geometry()` 里按屏幕大小自适应。
        try:
            self._guide.setSizeAdjustPolicy(
                QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustToContentsOnFirstShow
            )
        except Exception:
            pass
        guide_l.addWidget(self._guide)
        control_l.addWidget(guide_group)

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

        control_l.addWidget(svc_group)

        # Config
        cfg_group = QtWidgets.QGroupBox("配置")
        cfg_root = QtWidgets.QVBoxLayout(cfg_group)
        cfg_root.setSpacing(10)

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

        self._open_browser = QtWidgets.QCheckBox("服务启动后自动打开 Web UI（可选）")

        self._self_username_detected = QtWidgets.QLineEdit()
        self._self_username_detected.setReadOnly(True)
        self._self_username_detected.setPlaceholderText("启动服务后自动识别")

        self._self_username_override = QtWidgets.QLineEdit()
        self._btn_guess_self = QtWidgets.QPushButton("从 db_dir 推导")
        self._btn_guess_self.clicked.connect(self.guess_self_username)
        self._self_hint = QtWidgets.QLabel(
            "无需知道 wxid：服务会自动识别当前账号（用于判断“我发的消息”，避免自动回复自己）。识别不准时再手动覆盖。"
        )
        self._self_hint.setObjectName("hint")
        self._self_hint.setWordWrap(True)

        self._api_token = QtWidgets.QLineEdit()
        self._api_token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self._image_aes_key = QtWidgets.QLineEdit()
        self._image_xor_key = QtWidgets.QLineEdit()

        self._btn_save = QtWidgets.QPushButton("保存配置")
        self._btn_save.clicked.connect(self.save_config)

        self._btn_admin = QtWidgets.QPushButton("以管理员身份重启")
        self._btn_admin.clicked.connect(self.restart_as_admin)

        # Basic (always visible)
        basic = QtWidgets.QWidget()
        basic_l = QtWidgets.QGridLayout(basic)
        basic_l.setHorizontalSpacing(10)
        basic_l.setVerticalSpacing(8)

        row = 0
        basic_l.addWidget(QtWidgets.QLabel("db_dir"), row, 0)
        basic_l.addWidget(self._db_dir, row, 1, 1, 2)
        basic_l.addWidget(self._btn_pick_db, row, 3)

        row += 1
        basic_l.addWidget(QtWidgets.QLabel("当前账号（自动识别）"), row, 0)
        basic_l.addWidget(self._self_username_detected, row, 1, 1, 3)

        row += 1
        basic_l.addWidget(QtWidgets.QLabel("手动覆盖（可选）"), row, 0)
        basic_l.addWidget(self._self_username_override, row, 1, 1, 2)
        basic_l.addWidget(self._btn_guess_self, row, 3)

        row += 1
        basic_l.addWidget(self._self_hint, row, 1, 1, 3)

        row += 1
        basic_l.addWidget(self._btn_save, row, 2)
        basic_l.addWidget(self._btn_admin, row, 3)

        cfg_root.addWidget(basic)

        # Advanced (collapsed by default)
        self._advanced_group = QtWidgets.QGroupBox("高级设置（点开后可见）")
        self._advanced_group.setCheckable(True)
        self._advanced_group.setChecked(False)

        self._advanced_content = QtWidgets.QWidget()
        adv_l = QtWidgets.QGridLayout(self._advanced_content)
        adv_l.setHorizontalSpacing(10)
        adv_l.setVerticalSpacing(8)

        row = 0
        adv_l.addWidget(QtWidgets.QLabel("config.json"), row, 0)
        adv_l.addWidget(self._cfg_file, row, 1, 1, 2)
        adv_l.addWidget(self._btn_open_cfg, row, 3)

        row += 1
        adv_l.addWidget(QtWidgets.QLabel("listen_host"), row, 0)
        adv_l.addWidget(self._listen_host, row, 1)
        adv_l.addWidget(QtWidgets.QLabel("listen_port"), row, 2)
        adv_l.addWidget(self._listen_port, row, 3)

        row += 1
        adv_l.addWidget(QtWidgets.QLabel("open_browser"), row, 0)
        adv_l.addWidget(self._open_browser, row, 1, 1, 3)

        row += 1
        adv_l.addWidget(QtWidgets.QLabel("api_token（可选）"), row, 0)
        adv_l.addWidget(self._api_token, row, 1, 1, 3)

        row += 1
        adv_l.addWidget(QtWidgets.QLabel("image_aes_key（图片 V2）"), row, 0)
        adv_l.addWidget(self._image_aes_key, row, 1, 1, 3)

        row += 1
        adv_l.addWidget(QtWidgets.QLabel("image_xor_key（图片 V2）"), row, 0)
        adv_l.addWidget(self._image_xor_key, row, 1, 1, 3)

        row += 1
        adv_l.addWidget(QtWidgets.QLabel("日志目录"), row, 0)
        self._log_dir_view = QtWidgets.QLineEdit()
        self._log_dir_view.setReadOnly(True)
        self._log_dir_view.setText(self._log_dir or "")
        adv_l.addWidget(self._log_dir_view, row, 1, 1, 2)
        self._btn_open_logs = QtWidgets.QPushButton("打开")
        self._btn_open_logs.clicked.connect(self.open_log_folder)
        adv_l.addWidget(self._btn_open_logs, row, 3)

        row += 1
        adv_l.addWidget(QtWidgets.QLabel("诊断信息"), row, 0)
        self._btn_copy_diag = QtWidgets.QPushButton("复制到剪贴板")
        self._btn_copy_diag.clicked.connect(self.copy_diagnostics)
        adv_l.addWidget(self._btn_copy_diag, row, 1, 1, 3)

        adv_root = QtWidgets.QVBoxLayout(self._advanced_group)
        adv_root.setContentsMargins(12, 10, 12, 10)
        adv_root.addWidget(self._advanced_content)
        self._advanced_content.setVisible(False)
        self._advanced_group.toggled.connect(self._advanced_content.setVisible)

        cfg_root.addWidget(self._advanced_group)

        control_l.addWidget(cfg_group)
        control_l.addStretch(1)

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
            QTextBrowser {
                background: rgba(255,255,255,0.04);
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 8px;
                padding: 10px;
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
        self._self_username_override.setText(cfg.get("self_username", "") or "")

        self._image_aes_key.setText(cfg.get("image_aes_key", "") or "")
        xor_val = cfg.get("image_xor_key", "")
        if isinstance(xor_val, int):
            self._image_xor_key.setText(f"0x{xor_val:02X}")
        else:
            self._image_xor_key.setText(str(xor_val or "").strip())

        suggested_self = _suggest_self_username_from_db_dir(cfg.get("db_dir", "") or "")
        if suggested_self:
            self._self_username_override.setPlaceholderText(f"留空=自动识别（例如：{suggested_self}）")
        else:
            self._self_username_override.setPlaceholderText("留空=自动识别（例如：wxid_xxx）")

        detected = (self._state_self_username or "").strip()
        if detected:
            self._self_username_detected.setText(detected)
        else:
            self._self_username_detected.setText("")
            if suggested_self:
                self._self_username_detected.setPlaceholderText(f"可能是：{suggested_self}（来自 db_dir）")
            else:
                self._self_username_detected.setPlaceholderText("启动服务后自动识别")

        host = (cfg.get("listen_host") or "127.0.0.1").strip() or "127.0.0.1"
        port = int(cfg.get("listen_port") or 5678)
        url_host = "localhost" if host in ("0.0.0.0", "127.0.0.1", "::") else host
        self._url.setText(f"http://{url_host}:{port}")

        if cfg.get("_setup_required"):
            self._hint.setText("检测到未配置 db_dir：请先选择微信 db_storage 目录，然后保存配置。")
        else:
            self._hint.setText("提示：首次运行提取密钥可能需要管理员权限（右键“以管理员身份运行”）。")

        self._refresh_guide_text()

    def _refresh_guide_text(self):
        cfg = self._cfg or {}
        db_dir = (cfg.get("db_dir") or "").strip()
        suggested_self = _suggest_self_username_from_db_dir(db_dir)
        override_u = (cfg.get("self_username") or "").strip()
        detected_u = (self._state_self_username or "").strip()
        image_aes = (cfg.get("image_aes_key") or "").strip()

        if detected_u:
            status_self = f"已识别：<code>{detected_u}</code>"
        elif override_u:
            status_self = f"已设置覆盖：<code>{override_u}</code>（启动后生效）"
        else:
            status_self = "未识别（启动服务后自动识别）"
        status_img = "已配置" if image_aes else "未配置（微信 4.0 / 2025-08+ 可能需要）"

        html = f"""
        <div style="line-height:1.5">
          <b>快速开始</b>
          <ol style="margin-top:6px;margin-bottom:10px;">
            <li>先启动微信并保持登录。</li>
            <li>在“配置”里选择 <code>db_dir</code>（一般形如：<code>...\\xwechat_files\\&lt;wxid&gt;\\db_storage</code>）。</li>
            <li>点击“启动服务” → “打开 Web UI”。</li>
          </ol>
          <b>当前账号（self_username）</b>
          <ul style="margin-top:6px;margin-bottom:10px;">
            <li>无需知道 wxid：服务会自动识别当前账号（用于判断“我发的消息”，避免自动回复自己）。</li>
            <li>状态：{status_self}</li>
            <li>如果识别不准：在“手动覆盖（可选）”里填写（可点“从 db_dir 推导”）。</li>
          </ul>
          <b>小技巧</b>
          <ul style="margin-top:6px;margin-bottom:10px;">
            <li>密钥提取失败：尝试点击“以管理员身份重启”，并确保微信窗口已打开。</li>
            <li>多账号：请确保 <code>db_dir</code> 对应当前登录账号；必要时用“手动覆盖（可选）”纠正账号识别。</li>
          </ul>
          <b>图片解密/预览（V2）</b>
          <ul style="margin-top:6px;margin-bottom:0;">
            <li>状态：<code>image_aes_key</code> {status_img}</li>
            <li>步骤：先在微信里“点开查看 2-3 张图片（大图）”，再运行图片密钥提取（README 里有 <code>find_image_key_monitor.py</code> / <code>find_image_key.py</code>），成功后重启服务。</li>
          </ul>
          <div style="margin-top:8px;color:#9aa3c7;">提示：如果一直显示“未识别”，请先确认 <code>db_dir</code> 是否选对账号{('（可能是：<code>'+suggested_self+'</code>）') if suggested_self else ''}。</div>
        </div>
        """
        self._guide.setHtml(html)
        try:
            self._guide.document().adjustSize()
            self._guide.updateGeometry()
            if self.centralWidget() and self.centralWidget().layout():
                self.centralWidget().layout().activate()
        except Exception:
            pass

    def save_config(self):
        cfg = read_config_file(self._config_path)
        if not isinstance(cfg, dict):
            cfg = {}

        cfg["db_dir"] = (self._db_dir.text() or "").strip()
        cfg["listen_host"] = (self._listen_host.text() or "").strip() or "127.0.0.1"
        cfg["listen_port"] = int(self._listen_port.value())
        cfg["open_browser"] = bool(self._open_browser.isChecked())
        cfg["api_token"] = (self._api_token.text() or "").strip()
        cfg["self_username"] = (self._self_username_override.text() or "").strip()

        aes = (self._image_aes_key.text() or "").strip()
        if aes:
            cfg["image_aes_key"] = aes
        else:
            cfg.pop("image_aes_key", None)

        xor_text = (self._image_xor_key.text() or "").strip()
        if xor_text:
            try:
                xor_val = int(xor_text, 16) if xor_text.lower().startswith("0x") else int(xor_text, 10)
                if xor_val < 0 or xor_val > 255:
                    raise ValueError("out of range")
                cfg["image_xor_key"] = xor_val
            except Exception:
                QtWidgets.QMessageBox.warning(self, "配置错误", "image_xor_key 必须是 0-255 的数字（支持 0xA2 这种十六进制）。")
                return
        else:
            cfg.pop("image_xor_key", None)

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

    def guess_self_username(self):
        suggested = _suggest_self_username_from_db_dir(self._db_dir.text())
        if not suggested:
            QtWidgets.QMessageBox.information(
                self,
                "提示",
                "无法从当前 db_dir 推导账号（wxid）。\n"
                "请先选择正确的 db_dir，或直接启动服务后让程序自动识别当前账号。",
            )
            return

        current = (self._self_username_override.text() or "").strip()
        if current and current != suggested:
            r = QtWidgets.QMessageBox.question(
                self,
                "确认覆盖",
                f"当前覆盖值 = {current}\n建议值 = {suggested}\n\n是否用建议值覆盖？",
            )
            if r != QtWidgets.QMessageBox.StandardButton.Yes:
                return

        self._self_username_override.setText(suggested)

    def open_config_folder(self):
        folder = os.path.dirname(os.path.abspath(self._config_path))
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))

    def open_log_folder(self):
        folder = self._log_dir or os.path.join(os.path.dirname(os.path.abspath(self._config_path)), "logs")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(folder))

    def copy_diagnostics(self):
        cfg = read_config_file(self._config_path)
        if not isinstance(cfg, dict):
            cfg = {}

        host = (cfg.get("listen_host") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(cfg.get("listen_port") or 5678)
        except Exception:
            port = 5678
        url_host = "localhost" if host in ("0.0.0.0", "127.0.0.1", "::") else host
        base_url = f"http://{url_host}:{port}"

        db_dir = (cfg.get("db_dir") or "").strip()
        detected = (self._state_self_username or "").strip()
        override_u = (cfg.get("self_username") or "").strip()

        running = self._proc.state() == QtCore.QProcess.ProcessState.Running
        try:
            frozen = _is_frozen()
        except Exception:
            frozen = False

        service_exe = ""
        if frozen:
            service_exe = _find_service_exe()

        log_dir = self._log_dir or ""
        gui_log = self._log_path or ""
        service_console = (
            os.path.join(log_dir, "service-console.log") if log_dir else "service-console.log"
        )
        service_log = os.path.join(log_dir, "service.log") if log_dir else "service.log"

        lines = [
            f"app={APP_TITLE}",
            f"qt={QT_LIB}",
            f"pid={os.getpid()}",
            f"python={sys.version.split()[0]}",
            f"frozen={frozen}",
            f"config_path={os.path.abspath(self._config_path)}",
            f"log_dir={log_dir}",
            f"gui_log={gui_log}",
            f"service_log={service_log}",
            f"service_console_log={service_console}",
            f"service_exe={service_exe}",
            f"service_running={running}",
            f"listen={host}:{port}",
            f"base_url={base_url}",
            f"db_dir={db_dir}",
            f"db_dir_exists={bool(db_dir and os.path.isdir(db_dir))}",
            f"self_username_detected={detected}",
            f"self_username_override={override_u}",
        ]

        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        QtWidgets.QMessageBox.information(self, "已复制", "已复制诊断信息到剪贴板。")

    # ---------------- Service ----------------

    def start_service(self):
        if self._proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            return

        self._state_self_username = ""
        self._self_username_detected.setText("")
        self._auto_open_web_pending = False
        self._auto_open_web_done = False
        self._stop_requested = False

        self.save_config()
        cfg = load_config_soft(self._config_path)
        db_dir = (cfg.get("db_dir") or "").strip()
        if not db_dir or not os.path.isdir(db_dir):
            QtWidgets.QMessageBox.warning(self, "配置不完整", "db_dir 无效，请先选择微信 db_storage 目录。")
            return

        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("WECHAT_DECRYPT_CONFIG", os.path.abspath(self._config_path))
        # 服务端不应主动弹浏览器；GUI 按需控制
        env.insert("WECHAT_DECRYPT_NO_BROWSER", "1")
        self._proc.setProcessEnvironment(env)

        try:
            self._auto_open_web_pending = bool(cfg.get("open_browser", False)) and (not self._auto_start_service)
        except Exception:
            self._auto_open_web_pending = False

        # 不在 GUI 里展示服务的实时 stdout（会影响性能）；输出落盘供排障用。
        try:
            console_log = os.path.join(self._log_dir or os.path.dirname(os.path.abspath(self._config_path)), "service-console.log")
            self._proc.setStandardOutputFile(console_log, QtCore.QIODevice.OpenModeFlag.Append)
            self._proc.setStandardErrorFile(console_log, QtCore.QIODevice.OpenModeFlag.Append)
        except Exception:
            pass

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
        self._stop_requested = True
        self._append_log("[gui] stop requested")
        self._update_status("● 停止中…", ok=False)
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

    def _on_proc_error(self, err):
        try:
            name = getattr(err, "name", None) or str(err)
        except Exception:
            name = "unknown"
        self._append_log(f"[gui] process error: {name}")

        # 用户主动停止/退出时，Windows 上强杀会触发 Crashed；这不是“异常”，不弹窗打扰。
        try:
            if self._stop_requested and (
                err == QtCore.QProcess.ProcessError.Crashed or str(name).lower() == "crashed"
            ):
                return
        except Exception:
            pass

        msg = f"服务异常: {name}\n请在“高级设置 → 日志目录”查看日志。"
        try:
            if self.isVisible():
                QtWidgets.QMessageBox.warning(self, "服务异常", msg)
            elif self._tray:
                self._tray.showMessage(APP_TITLE, msg, QtWidgets.QSystemTrayIcon.MessageIcon.Warning, 5000)
        except Exception:
            pass

    def _on_proc_state_changed(self, _state):
        st = self._proc.state()
        if st == QtCore.QProcess.ProcessState.Running:
            self._update_status("● 运行中", ok=True)
            self._btn_start.setEnabled(False)
            self._btn_stop.setEnabled(True)
        elif st == QtCore.QProcess.ProcessState.NotRunning:
            self._state_self_username = ""
            self._btn_start.setEnabled(True)
            self._btn_stop.setEnabled(False)
            self._refresh_ui_from_config()

    def _on_proc_finished(self, exit_code, exit_status):
        self._poll_timer.stop()
        if self._stop_requested:
            self._update_status("● 已停止", ok=False)
        else:
            self._update_status(f"● 已退出 (code={exit_code})", ok=False)
        self._append_log(f"[gui] exited: code={exit_code} status={exit_status}")
        self._stop_requested = False

    def _poll_health(self):
        if self._proc.state() != QtCore.QProcess.ProcessState.Running:
            return
        if self._health_pending:
            return
        base = self._url.text().strip().rstrip("/")
        if not base:
            return
        self._health_pending = True
        try:
            QtCore.QThreadPool.globalInstance().start(_HealthWorker(self._health_emitter, base))
        except Exception:
            self._health_pending = False

    def _on_health_done(self, ok: bool, state):
        self._health_pending = False
        if self._proc.state() != QtCore.QProcess.ProcessState.Running:
            return
        if ok:
            self._update_status("● 运行中（可达）", ok=True)
            if self._auto_open_web_pending and (not getattr(self, "_auto_open_web_done", False)):
                self._auto_open_web_done = True
                try:
                    self.open_web_ui()
                except Exception:
                    pass
            if isinstance(state, dict):
                new_self = (state.get("self_username") or "").strip()
                if new_self != (self._state_self_username or ""):
                    self._state_self_username = new_self
                    self._self_username_detected.setText(new_self or "")
                    self._refresh_ui_from_config()
        else:
            self._update_status("● 运行中（不可达）", ok=False)

    # ---------------- helpers ----------------

    def _update_status(self, text: str, ok: bool):
        self._status.setText(text)
        color = "#81c784" if ok else "#ffb74d"
        self._status.setStyleSheet(f"color: {color};")

    def _append_log(self, line: str, *, tag: str = "gui"):
        if line is None:
            return
        text = str(line)
        if not text:
            return
        parts = text.splitlines() or [text]
        for p in parts:
            write_log_line(p, tag=tag)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    auto = "--autostart" in argv

    init_app_logging("gui", config_path=get_config_path())

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    win = MainWindow(auto_start_service=auto)
    win.show()
    return int(app.exec())
