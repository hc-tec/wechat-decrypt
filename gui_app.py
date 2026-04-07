import os
import sys
import json
import urllib.request
import re
import threading

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


class _ImageKeyEmitter(QtCore.QObject):
    progress = _Signal(str) if _Signal else None  # type: ignore[misc]
    done = _Signal(bool, object) if _Signal else None  # type: ignore[misc]


class _ImageKeyWorker(QtCore.QRunnable):
    def __init__(
        self,
        emitter: _ImageKeyEmitter,
        *,
        db_dir: str,
        process_name: str,
        stop_event: threading.Event,
    ):
        super().__init__()
        self._emitter = emitter
        self._db_dir = db_dir
        self._process_name = process_name
        self._stop_event = stop_event

    def run(self):
        ok = False
        payload: object = None
        try:
            import image_key_extractor as ike  # lazy import

            def _progress(msg: str) -> None:
                try:
                    if getattr(self._emitter, "progress", None):
                        self._emitter.progress.emit(str(msg))
                except Exception:
                    pass

            aes, xor = ike.extract_image_keys(
                self._db_dir,
                process_name=self._process_name,
                stop_check=self._stop_event.is_set,
                progress=_progress,
                timeout_seconds=180,
            )
            ok = True
            payload = {"aes_key": aes, "xor_key": xor}
        except Exception as e:
            ok = False
            payload = str(e)
        try:
            if getattr(self._emitter, "done", None):
                self._emitter.done.emit(bool(ok), payload)
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


def _default_wechat_files_root() -> str:
    # Windows WeChat PC 常见目录：%USERPROFILE%\Documents\WeChat Files
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Documents", "WeChat Files"),
        os.path.join(home, "WeChat Files"),
    ]
    for p in candidates:
        if os.path.isdir(p):
            return p
    return ""


def _find_db_storage_candidates() -> list[str]:
    root = _default_wechat_files_root()
    if not root:
        return []

    out: list[str] = []
    try:
        for name in os.listdir(root):
            if not name or name.startswith("."):
                continue
            p = os.path.join(root, name, "Msg", "Multi", "db_storage")
            if os.path.isdir(p):
                out.append(p)
    except Exception:
        out = []

    # 兜底：少量递归扫描（避免全盘 walk）。
    if not out:
        try:
            max_depth = 6
            root_depth = os.path.abspath(root).count(os.sep)
            for dirpath, dirnames, _filenames in os.walk(root):
                depth = os.path.abspath(dirpath).count(os.sep) - root_depth
                if depth > max_depth:
                    dirnames[:] = []
                    continue
                if os.path.basename(dirpath).lower() == "db_storage":
                    out.append(dirpath)
                    dirnames[:] = []
        except Exception:
            pass

    # unique while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        ap = os.path.abspath(p)
        if ap in seen:
            continue
        seen.add(ap)
        uniq.append(ap)
    return uniq


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
        self._health_last_ok = False
        self._base_url = ""
        self._did_first_show = False

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
        if not self._did_first_show:
            self._did_first_show = True
            QtCore.QTimer.singleShot(0, self._post_show_adjust_geometry)
        else:
            QtCore.QTimer.singleShot(0, self._ensure_within_screen)

    def _post_show_adjust_geometry(self):
        # 某些环境下（尤其是冻结版/高 DPI），屏幕几何信息在首次 show 前可能不稳定，
        # 会导致初始窗口“异常偏小/偏大”。这里在首次 show 后再校正一次。
        g = self._get_available_screen_geometry()
        if not g:
            return

        margin = 24
        max_w = max(320, int(g.width() - margin))
        max_h = max(240, int(g.height() - margin))

        try:
            w = int(self.width())
            h = int(self.height())
        except Exception:
            w, h = 0, 0

        if w <= 0 or h <= 0:
            self._apply_initial_window_geometry()
            self._ensure_within_screen()
            return

        # 过小/过大：重新按屏幕比例设置一次，再 clamp 到屏幕内。
        too_small = w < min(700, max_w) or h < min(420, max_h)
        too_large = w > max_w or h > max_h
        if too_small or too_large:
            self._apply_initial_window_geometry()

        self._ensure_within_screen()

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

        # Quick actions (avoid long text guides for non-technical users)
        quick_group = QtWidgets.QGroupBox("开始")
        quick_l = QtWidgets.QGridLayout(quick_group)
        quick_l.setHorizontalSpacing(12)
        quick_l.setVerticalSpacing(12)

        def _mk_card(title: str):
            card = QtWidgets.QFrame()
            card.setObjectName("card")
            v = QtWidgets.QVBoxLayout(card)
            v.setContentsMargins(12, 10, 12, 10)
            v.setSpacing(8)

            header = QtWidgets.QHBoxLayout()
            t = QtWidgets.QLabel(title)
            t.setObjectName("cardTitle")
            header.addWidget(t)
            header.addStretch(1)
            badge = QtWidgets.QLabel("")
            badge.setObjectName("badge")
            header.addWidget(badge)
            v.addLayout(header)

            value = QtWidgets.QLabel("")
            value.setWordWrap(True)
            value.setObjectName("cardValue")
            v.addWidget(value, 1)

            actions = QtWidgets.QHBoxLayout()
            actions.addStretch(1)
            v.addLayout(actions)
            return card, badge, value, actions

        # ① WeChat folder
        c1, self._q_db_badge, self._q_db_value, a1 = _mk_card("① 微信数据目录")
        self._q_db_btn = QtWidgets.QPushButton("选择…")
        self._q_db_btn.clicked.connect(self.pick_db_dir)
        self._q_db_auto = QtWidgets.QPushButton("自动定位")
        self._q_db_auto.clicked.connect(self.auto_pick_db_dir)
        a1.addWidget(self._q_db_btn)
        a1.addWidget(self._q_db_auto)
        quick_l.addWidget(c1, 0, 0)

        # ② Service
        c2, self._q_svc_badge, self._q_svc_value, a2 = _mk_card("② 服务")
        self._q_svc_btn = QtWidgets.QPushButton("启动")
        self._q_svc_btn.clicked.connect(self._toggle_service_quick)
        a2.addWidget(self._q_svc_btn)
        quick_l.addWidget(c2, 0, 1)

        # ③ API URL
        c3, self._q_url_badge, self._q_url_value, a3 = _mk_card("③ 接口地址")
        self._q_url_copy = QtWidgets.QPushButton("复制")
        self._q_url_copy.clicked.connect(self.copy_url)
        a3.addWidget(self._q_url_copy)
        quick_l.addWidget(c3, 1, 0)

        # ④ Current account
        c4, self._q_self_badge, self._q_self_value, a4 = _mk_card("④ 当前账号")
        self._q_self_copy = QtWidgets.QPushButton("复制")
        self._q_self_copy.clicked.connect(self.copy_self_username)
        a4.addWidget(self._q_self_copy)
        quick_l.addWidget(c4, 1, 1)

        control_l.addWidget(quick_group)

        # Image keys (V2) wizard entry for non-technical users
        img_group = QtWidgets.QGroupBox("图片解密（可选）")
        img_l = QtWidgets.QHBoxLayout(img_group)
        img_l.setContentsMargins(12, 10, 12, 10)
        img_l.setSpacing(10)

        self._img_key_status = QtWidgets.QLabel("")
        self._img_key_status.setWordWrap(True)
        img_l.addWidget(self._img_key_status, 1)

        self._btn_img_keys = QtWidgets.QPushButton("图片密钥…")
        self._btn_img_keys.clicked.connect(self.open_image_key_wizard)
        self._btn_img_clear = QtWidgets.QPushButton("清除")
        self._btn_img_clear.clicked.connect(self.clear_image_keys)
        img_l.addWidget(self._btn_img_keys)
        img_l.addWidget(self._btn_img_clear)

        control_l.addWidget(img_group)

        # Quick tools (no long text; actions only)
        tools = QtWidgets.QWidget()
        tools_l = QtWidgets.QHBoxLayout(tools)
        tools_l.setContentsMargins(0, 0, 0, 0)
        tools_l.setSpacing(10)

        self._btn_open_logs_main = QtWidgets.QPushButton("打开日志")
        self._btn_open_logs_main.clicked.connect(self.open_log_folder)
        self._btn_open_cfg_main = QtWidgets.QPushButton("打开配置")
        self._btn_open_cfg_main.clicked.connect(self.open_config_folder)
        self._btn_admin_main = QtWidgets.QPushButton("管理员运行")
        self._btn_admin_main.clicked.connect(self.restart_as_admin)

        tools_l.addWidget(self._btn_open_logs_main)
        tools_l.addWidget(self._btn_open_cfg_main)
        tools_l.addStretch(1)
        tools_l.addWidget(self._btn_admin_main)
        control_l.addWidget(tools)

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

        self._open_browser = QtWidgets.QCheckBox("服务启动后自动打开调试页（可选）")

        self._self_username_detected = QtWidgets.QLineEdit()
        self._self_username_detected.setReadOnly(True)
        self._self_username_detected.setPlaceholderText("启动服务后自动识别")

        self._self_username_override = QtWidgets.QLineEdit()
        self._btn_guess_self = QtWidgets.QPushButton("从 db_dir 推导")
        self._btn_guess_self.clicked.connect(self.guess_self_username)
        self._self_username_detected.setToolTip("启动服务后自动识别（用于区分“我发的消息”，避免误处理自己）。")
        self._self_username_override.setToolTip("仅在自动识别不准时填写；留空=自动识别。")

        self._api_token = QtWidgets.QLineEdit()
        self._api_token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self._image_aes_key = QtWidgets.QLineEdit()
        self._image_xor_key = QtWidgets.QLineEdit()

        self._btn_save = QtWidgets.QPushButton("保存设置")
        self._btn_save.clicked.connect(self.save_config)

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
        basic_l.addWidget(QtWidgets.QLabel("我的账号（自动识别）"), row, 0)
        basic_l.addWidget(self._self_username_detected, row, 1, 1, 3)

        row += 1
        basic_l.addWidget(QtWidgets.QLabel("手动修正（可选）"), row, 0)
        basic_l.addWidget(self._self_username_override, row, 1, 1, 2)
        basic_l.addWidget(self._btn_guess_self, row, 3)

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
        adv_l.addWidget(QtWidgets.QLabel("调试"), row, 0)
        self._btn_open_web_adv = QtWidgets.QPushButton("打开调试页")
        self._btn_open_web_adv.clicked.connect(self.open_web_ui)
        self._btn_test_health_adv = QtWidgets.QPushButton("探活")
        self._btn_test_health_adv.clicked.connect(self.test_health)
        adv_l.addWidget(self._btn_open_web_adv, row, 1)
        adv_l.addWidget(self._btn_test_health_adv, row, 2)

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

        row += 1
        adv_l.addWidget(QtWidgets.QLabel("保存"), row, 0)
        adv_l.addWidget(self._btn_save, row, 1, 1, 3)

        adv_root = QtWidgets.QVBoxLayout(self._advanced_group)
        adv_root.setContentsMargins(12, 10, 12, 10)
        adv_root.addWidget(self._advanced_content)
        self._advanced_content.setVisible(False)
        self._advanced_group.toggled.connect(self._advanced_content.setVisible)
        self._advanced_group.toggled.connect(lambda _v: QtCore.QTimer.singleShot(0, self._ensure_within_screen))

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
            QFrame#card {
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 12px;
                background: rgba(255,255,255,0.03);
            }
            QLabel#cardTitle { font-size: 13px; font-weight: 700; color: #e8eaf0; }
            QLabel#cardValue { color: #c7cbe0; }
            QLabel#badge { font-size: 11px; }
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
        self._open_browser.setChecked(bool(cfg.get("open_browser", False)))
        self._api_token.setText(cfg.get("api_token", "") or "")
        self._self_username_override.setText(cfg.get("self_username", "") or "")

        self._image_aes_key.setText(cfg.get("image_aes_key", "") or "")
        xor_val = cfg.get("image_xor_key", "")
        if isinstance(xor_val, int):
            self._image_xor_key.setText(f"0x{xor_val:02X}")
        else:
            self._image_xor_key.setText(str(xor_val or "").strip())

        self._refresh_image_key_status(cfg)

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
        self._base_url = f"http://{url_host}:{port}"

        self._refresh_quick_cards()

    def _refresh_image_key_status(self, cfg: dict | None = None) -> None:
        cfg = cfg or self._cfg or {}
        aes = (cfg.get("image_aes_key") or "").strip()
        xor_val = cfg.get("image_xor_key", "")

        xor_text = ""
        if isinstance(xor_val, int):
            xor_text = f"0x{xor_val:02X}"
        elif isinstance(xor_val, str) and xor_val.strip():
            xor_text = xor_val.strip()

        if aes:
            masked = aes if len(aes) <= 8 else (aes[:4] + "…" + aes[-4:])
            if xor_text:
                self._img_key_status.setText(f"已配置（AES={masked}, XOR={xor_text}）")
            else:
                self._img_key_status.setText(f"已配置（AES={masked}）")
        else:
            self._img_key_status.setText("未配置：若需要在调试页/接口中解析图片，请点“图片密钥…”")

    def set_image_keys(self, aes_key: str, xor_key: int | None) -> None:
        aes_key = (aes_key or "").strip()
        if not aes_key:
            return

        cfg = read_config_file(self._config_path)
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["image_aes_key"] = aes_key
        if xor_key is not None:
            try:
                xv = int(xor_key)
                if 0 <= xv <= 255:
                    cfg["image_xor_key"] = xv
            except Exception:
                pass
        try:
            write_config_file(cfg, self._config_path)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "保存失败", str(e))
            return
        self._append_log("[gui] 已保存图片密钥")
        self._refresh_ui_from_config()

    def clear_image_keys(self) -> None:
        cfg = read_config_file(self._config_path)
        if not isinstance(cfg, dict):
            cfg = {}
        if "image_aes_key" not in cfg and "image_xor_key" not in cfg:
            return
        cfg.pop("image_aes_key", None)
        cfg.pop("image_xor_key", None)
        try:
            write_config_file(cfg, self._config_path)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "保存失败", str(e))
            return
        self._append_log("[gui] 已清除图片密钥")
        self._refresh_ui_from_config()

    def open_image_key_wizard(self) -> None:
        dlg = ImageKeyWizard(self)
        dlg.exec()

    def _set_badge(self, label: QtWidgets.QLabel, *, state: str, text: str) -> None:
        label.setText(text or "")
        if state == "ok":
            label.setStyleSheet(
                "QLabel{color:#81c784;background:rgba(129,199,132,0.10);"
                "border:1px solid rgba(129,199,132,0.35);border-radius:999px;padding:2px 10px;}"
            )
        elif state == "err":
            label.setStyleSheet(
                "QLabel{color:#ef9a9a;background:rgba(244,67,54,0.10);"
                "border:1px solid rgba(244,67,54,0.35);border-radius:999px;padding:2px 10px;}"
            )
        else:
            label.setStyleSheet(
                "QLabel{color:#ffb74d;background:rgba(255,183,77,0.10);"
                "border:1px solid rgba(255,183,77,0.35);border-radius:999px;padding:2px 10px;}"
            )

    def _short_path(self, p: str, max_len: int = 64) -> str:
        p = (p or "").strip()
        if not p:
            return ""
        if len(p) <= max_len:
            return p
        return "…" + p[-(max_len - 1) :]

    def _refresh_quick_cards(self) -> None:
        cfg = self._cfg or {}

        db_dir = (cfg.get("db_dir") or "").strip()
        db_ok = bool(db_dir and os.path.isdir(db_dir))
        db_hint = self._short_path(db_dir) if db_dir else "未选择"
        if db_dir and os.path.basename(db_dir).lower() != "db_storage":
            # best-effort: still allow, but warn.
            db_hint = f"{self._short_path(db_dir)}（可能不是 db_storage）"
        self._q_db_value.setText(db_hint)
        self._set_badge(self._q_db_badge, state=("ok" if db_ok else "warn"), text=("已选择" if db_ok else "未选择"))

        running = self._proc.state() == QtCore.QProcess.ProcessState.Running
        reachable = bool(self._health_last_ok) if running else False
        base_url = (self._base_url or "").strip()
        self._q_svc_value.setText("运行中" if reachable else ("启动中…" if running else "未启动"))
        if reachable:
            self._set_badge(self._q_svc_badge, state="ok", text="运行中")
        elif running:
            self._set_badge(self._q_svc_badge, state="warn", text="不可达")
        else:
            self._set_badge(self._q_svc_badge, state="warn", text="未启动")

        try:
            self._q_svc_btn.setText("停止" if running else "启动")
        except Exception:
            pass

        self._q_url_value.setText(base_url or "-")
        self._set_badge(self._q_url_badge, state=("ok" if reachable else "warn"), text=("可达" if reachable else "待启动"))

        detected_u = (self._state_self_username or "").strip()
        override_u = (cfg.get("self_username") or "").strip()
        suggested = _suggest_self_username_from_db_dir(db_dir)
        if detected_u:
            self._q_self_value.setText(detected_u)
            self._set_badge(self._q_self_badge, state="ok", text="已识别")
        elif override_u:
            self._q_self_value.setText(override_u)
            self._set_badge(self._q_self_badge, state="ok", text="已覆盖")
        elif suggested:
            self._q_self_value.setText(f"可能是：{suggested}")
            self._set_badge(self._q_self_badge, state="warn", text="未识别")
        else:
            self._q_self_value.setText("未识别（启动后自动识别）")
            self._set_badge(self._q_self_badge, state="warn", text="未识别")

    def _toggle_service_quick(self):
        if self._proc.state() == QtCore.QProcess.ProcessState.Running:
            self.stop_service()
        else:
            self.start_service()

    def copy_self_username(self):
        cfg = read_config_file(self._config_path)
        if not isinstance(cfg, dict):
            cfg = {}
        detected = (self._state_self_username or "").strip()
        override_u = (cfg.get("self_username") or "").strip()
        value = detected or override_u
        if not value:
            QtWidgets.QMessageBox.information(self, "提示", "当前账号尚未识别。\n请先启动服务，稍等 1-2 秒再试。")
            return
        QtWidgets.QApplication.clipboard().setText(value)

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

    def auto_pick_db_dir(self):
        detected = ""
        try:
            from config import auto_detect_db_dir  # lazy import

            detected = auto_detect_db_dir() or ""
        except Exception:
            detected = ""

        candidates = [detected] if detected and os.path.isdir(detected) else _find_db_storage_candidates()
        if not candidates:
            root = _default_wechat_files_root()
            if root:
                try:
                    QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(root))
                except Exception:
                    pass
            QtWidgets.QMessageBox.information(
                self,
                "未找到",
                "未在默认微信目录找到 db_storage。\n请点击“选择…”手动选择微信 db_storage 目录。",
            )
            return

        chosen = ""
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            options: list[str] = []
            mapping: dict[str, str] = {}
            for p in candidates:
                acc = _suggest_self_username_from_db_dir(p) or "未知账号"
                label = f"{acc}  —  {p}"
                options.append(label)
                mapping[label] = p

            item, ok = QtWidgets.QInputDialog.getItem(self, "选择账号", "检测到多个账号，请选择：", options, 0, False)
            if not ok:
                return
            chosen = (mapping.get(item or "") or "").strip()

        if not chosen:
            return

        self._db_dir.setText(chosen)
        self.save_config()

    def pick_db_dir(self):
        start = self._db_dir.text().strip() or os.path.expanduser("~")
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择 db_storage 目录", start)
        if d:
            self._db_dir.setText(d)
            self.save_config()

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
        self.save_config()

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
        self._health_last_ok = False

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
        try:
            self._refresh_quick_cards()
        except Exception:
            pass

    def stop_service(self):
        if self._proc.state() == QtCore.QProcess.ProcessState.NotRunning:
            return
        self._stop_requested = True
        self._append_log("[gui] stop requested")
        self._update_status("● 停止中…", ok=False)
        try:
            self._refresh_quick_cards()
        except Exception:
            pass
        self._proc.terminate()
        QtCore.QTimer.singleShot(2200, self._kill_if_needed)

    def _kill_if_needed(self):
        if self._proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._append_log("[gui] kill")
            self._proc.kill()

    def open_web_ui(self):
        url = (self._base_url or "").strip()
        if url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def test_health(self):
        url = (self._base_url or "").strip().rstrip("/")
        ok = _can_open_url(url + "/api/v1/health")
        QtWidgets.QMessageBox.information(self, "探活结果", "OK" if ok else "失败（服务未启动或端口不可达）")

    def copy_url(self):
        QtWidgets.QApplication.clipboard().setText((self._base_url or "").strip())

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
        elif st == QtCore.QProcess.ProcessState.NotRunning:
            self._state_self_username = ""
            self._refresh_ui_from_config()
        try:
            self._refresh_quick_cards()
        except Exception:
            pass

    def _on_proc_finished(self, exit_code, exit_status):
        self._poll_timer.stop()
        self._health_last_ok = False
        if self._stop_requested:
            self._update_status("● 已停止", ok=False)
        else:
            self._update_status(f"● 已退出 (code={exit_code})", ok=False)
        self._append_log(f"[gui] exited: code={exit_code} status={exit_status}")
        self._stop_requested = False
        try:
            self._refresh_quick_cards()
        except Exception:
            pass

    def _poll_health(self):
        if self._proc.state() != QtCore.QProcess.ProcessState.Running:
            return
        if self._health_pending:
            return
        base = (self._base_url or "").strip().rstrip("/")
        if not base:
            return
        self._health_pending = True
        try:
            QtCore.QThreadPool.globalInstance().start(_HealthWorker(self._health_emitter, base))
        except Exception:
            self._health_pending = False

    def _on_health_done(self, ok: bool, state):
        self._health_pending = False
        self._health_last_ok = bool(ok)
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
        try:
            self._refresh_quick_cards()
        except Exception:
            pass

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


class ImageKeyWizard(QtWidgets.QDialog):
    def __init__(self, parent: MainWindow):
        super().__init__(parent)
        self._mw = parent
        self.setWindowTitle("图片密钥（V2）")
        self.setModal(True)
        self.resize(720, 420)

        self._stop_event = threading.Event()
        self._running = False

        self._emitter = _ImageKeyEmitter()
        try:
            self._emitter.progress.connect(self._on_progress)  # type: ignore[union-attr]
            self._emitter.done.connect(self._on_done)  # type: ignore[union-attr]
        except Exception:
            pass

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        tabs = QtWidgets.QTabWidget()
        root.addWidget(tabs, 1)

        # ---- Tab: Auto extract ----
        tab_auto = QtWidgets.QWidget()
        tabs.addTab(tab_auto, "自动提取（推荐）")
        auto_l = QtWidgets.QVBoxLayout(tab_auto)
        auto_l.setSpacing(10)

        hint = QtWidgets.QLabel(
            "点“开始扫描”后，你可以去微信打开 2-3 张图片（大图）并保持打开；"
            "本程序会持续扫描约 3 分钟，找到后自动保存。"
        )
        hint.setWordWrap(True)
        auto_l.addWidget(hint)

        self._chk_ready = QtWidgets.QCheckBox("如果已打开图片（大图），会更快（可选）")
        auto_l.addWidget(self._chk_ready)

        btn_row = QtWidgets.QHBoxLayout()
        self._btn_start = QtWidgets.QPushButton("开始扫描")
        self._btn_start.clicked.connect(self._start_scan)
        self._btn_cancel = QtWidgets.QPushButton("取消")
        self._btn_cancel.clicked.connect(self._cancel)
        btn_row.addWidget(self._btn_start)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_cancel)
        auto_l.addLayout(btn_row)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        auto_l.addWidget(self._status)

        self._bar = QtWidgets.QProgressBar()
        self._bar.setRange(0, 0)
        self._bar.setVisible(False)
        auto_l.addWidget(self._bar)
        auto_l.addStretch(1)

        # ---- Tab: Manual input ----
        tab_manual = QtWidgets.QWidget()
        tabs.addTab(tab_manual, "手动填写")
        man_l = QtWidgets.QGridLayout(tab_manual)
        man_l.setHorizontalSpacing(10)
        man_l.setVerticalSpacing(10)

        self._aes_in = QtWidgets.QLineEdit()
        self._aes_in.setPlaceholderText("例如：0123456789abcdef")
        self._xor_in = QtWidgets.QLineEdit()
        self._xor_in.setPlaceholderText("例如：0x88")

        cfg = load_config_soft(self._mw._config_path)
        self._aes_in.setText((cfg.get("image_aes_key") or "").strip())
        xv = cfg.get("image_xor_key", "")
        if isinstance(xv, int):
            self._xor_in.setText(f"0x{xv:02X}")
        else:
            self._xor_in.setText(str(xv or "").strip())

        self._btn_save_manual = QtWidgets.QPushButton("保存")
        self._btn_save_manual.clicked.connect(self._save_manual)

        row = 0
        man_l.addWidget(QtWidgets.QLabel("AES key"), row, 0)
        man_l.addWidget(self._aes_in, row, 1, 1, 2)
        row += 1
        man_l.addWidget(QtWidgets.QLabel("XOR key"), row, 0)
        man_l.addWidget(self._xor_in, row, 1)
        man_l.addWidget(self._btn_save_manual, row, 2)

    def closeEvent(self, event):  # noqa: N802
        try:
            self._stop_event.set()
        except Exception:
            pass
        super().closeEvent(event)

    def _set_running(self, running: bool) -> None:
        self._running = bool(running)
        self._btn_start.setEnabled(not self._running)
        self._chk_ready.setEnabled(not self._running)
        self._btn_cancel.setText("取消" if self._running else "关闭")
        self._bar.setVisible(self._running)

    def _start_scan(self):
        if self._running:
            return

        cfg = load_config_soft(self._mw._config_path)
        db_dir = (cfg.get("db_dir") or "").strip()
        if not db_dir or not os.path.isdir(db_dir):
            QtWidgets.QMessageBox.warning(self, "配置不完整", "db_dir 无效，请先在主界面选择微信 db_storage 目录。")
            return

        proc = (cfg.get("wechat_process") or "Weixin.exe").strip() or "Weixin.exe"

        self._stop_event.clear()
        self._status.setText("准备扫描…")
        try:
            self._mw._append_log(f"[imgkey] start scan: process={proc} db_dir={db_dir}", tag="gui")
        except Exception:
            pass
        self._set_running(True)
        try:
            QtCore.QThreadPool.globalInstance().start(
                _ImageKeyWorker(self._emitter, db_dir=db_dir, process_name=proc, stop_event=self._stop_event)
            )
        except Exception as e:
            self._set_running(False)
            QtWidgets.QMessageBox.critical(self, "失败", str(e))

    def _cancel(self):
        if self._running:
            try:
                self._stop_event.set()
            except Exception:
                pass
            self._status.setText("正在取消…")
            return
        self.reject()

    def _on_progress(self, msg: str):
        self._status.setText(str(msg))
        try:
            self._mw._append_log(f"[imgkey] {msg}", tag="gui")
        except Exception:
            pass

    def _on_done(self, ok: bool, payload):
        self._set_running(False)
        if not ok:
            msg = str(payload or "失败")
            msg = (
                msg
                + "\n\n建议：保持图片“大图”窗口打开；若提示权限不足请回主界面点“管理员运行”；"
                + "若微信进程名不是 Weixin.exe 可在 config.json 的 wechat_process 修改后重试。"
            )
            self._status.setText(msg)
            try:
                self._mw._append_log(f"[imgkey] failed: {msg}", tag="gui")
            except Exception:
                pass
            QtWidgets.QMessageBox.warning(self, "未成功", msg)
            return

        aes = ""
        xor = None
        try:
            if isinstance(payload, dict):
                aes = str(payload.get("aes_key") or "").strip()
                xor = payload.get("xor_key", None)
        except Exception:
            aes = ""
            xor = None

        if not aes:
            QtWidgets.QMessageBox.warning(self, "未成功", "未提取到 AES key。请先点开几张图片（大图）后重试。")
            return

        self._mw.set_image_keys(aes, int(xor) if isinstance(xor, int) else None)
        try:
            xor_text = f"0x{int(xor):02X}" if isinstance(xor, int) else "unknown"
            self._mw._append_log(f"[imgkey] success: aes_len={len(aes)} xor={xor_text}", tag="gui")
        except Exception:
            pass
        QtWidgets.QMessageBox.information(self, "完成", "已保存图片密钥到配置。现在可以尝试打开调试页查看图片。")
        self.accept()

    def _save_manual(self):
        aes = (self._aes_in.text() or "").strip()
        if not aes:
            QtWidgets.QMessageBox.warning(self, "提示", "AES key 不能为空。")
            return
        xor_text = (self._xor_in.text() or "").strip()
        xor_val: int | None = None
        if xor_text:
            try:
                xv = int(xor_text, 16) if xor_text.lower().startswith("0x") else int(xor_text, 10)
                if xv < 0 or xv > 255:
                    raise ValueError("out of range")
                xor_val = int(xv)
            except Exception:
                QtWidgets.QMessageBox.warning(self, "提示", "XOR key 必须是 0-255 的数字（支持 0xA2）。")
                return
        self._mw.set_image_keys(aes, xor_val)
        try:
            xor_text = f"0x{int(xor_val):02X}" if isinstance(xor_val, int) else "unknown"
            self._mw._append_log(f"[imgkey] manual save: aes_len={len(aes)} xor={xor_text}", tag="gui")
        except Exception:
            pass
        QtWidgets.QMessageBox.information(self, "完成", "已保存。")
        self.accept()
