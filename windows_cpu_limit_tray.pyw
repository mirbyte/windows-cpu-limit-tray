#!/usr/bin/env python3
"""
A small Windows tray utility for quickly changing the plugged-in maximum
processor state of the active power plan. When the CPU limit is below 100%, it
can show a small always-on-top overlay with the current limit.

"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Optional

try:
    from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, QSettings, Signal
    from PySide6.QtGui import (
        QAction,
        QColor,
        QFont,
        QFontMetrics,
        QGuiApplication,
        QIcon,
        QPainter,
        QPen,
        QPixmap,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QMenu,
        QPushButton,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    # With .pyw this may not be visible, but it is useful when run from console.
    print("Missing dependency: PySide6")
    print("Install it with:")
    print("    pip install -r requirements.txt")
    raise


APP_TITLE = "CPU Maximum Processor State"
APP_MUTEX_NAME = r"Local\CPUMaximumProcessorState"
SETTINGS_ORGANIZATION = "CPUMaximumProcessorState"
SETTINGS_APPLICATION = "CPUMaximumProcessorState"

POLL_MS = 3000
POWERCFG_TIMEOUT_SECONDS = 10

# Tray behavior. Close and minimize hide only the main control window; the
# floating CPU overlay remains controlled by its checkbox and CPU percentage.
CLOSE_BUTTON_HIDES_TO_TRAY = True
MINIMIZE_BUTTON_HIDES_TO_TRAY = True

# Preset-only app. Add/remove values here if you want a different layout.
CPU_PRESETS = [
    5, 10, 15, 20, 25,
    30, 40, 50, 60, 70,
    80, 90, 95, 99, 100,
]

PRESET_COLUMNS = 5

# Set this to True if you want a warning popup before applying very low presets.
CONFIRM_LOW_PRESETS = True
LOW_PRESET_WARNING_AT_OR_BELOW = 10

# Safety net for quitting while Windows is still using a reduced CPU limit.
WARN_BEFORE_EXIT_BELOW_100_DEFAULT = True

# Windows power setting GUIDs.
PROCESSOR_SETTINGS_SUBGROUP_GUID = "54533251-82be-4824-96c1-47b60b740d00"
MAX_PROCESSOR_STATE_GUID = "bc5038f7-23e0-4960-96da-33abaf5935ec"

# Overlay appearance. Qt uses logical coordinates, so these scale correctly
# on high-DPI displays like 4K at 175%.
OVERLAY_FONT_POINT_SIZE = 15
OVERLAY_MARGIN_X = 7
OVERLAY_MARGIN_Y = 4
OVERLAY_RIGHT_OFFSET = 18
OVERLAY_TOP_OFFSET = 18

# Greenish overlay colors.
OVERLAY_TEXT_COLOR = QColor(125, 255, 170, 245)
OVERLAY_OUTLINE_COLOR = QColor(0, 35, 12, 220)


def create_green_tray_icon() -> QIcon:
    """
    Create a clearly green multi-size tray icon.

    A text-only 32x32 icon can get downscaled by Windows until it looks like a
    generic app icon, so this version adds multiple pixmap sizes and uses a
    bright green filled circle with a dark outline. That remains visibly green
    even in the 16x16 notification area.
    """
    icon = QIcon()

    for size in (16, 20, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        margin = max(1, size // 8)
        pen_width = max(1, size // 12)
        circle_rect = pixmap.rect().adjusted(margin, margin, -margin, -margin)

        # Strong green fill + dark outline, based on the overlay colors.
        painter.setPen(QPen(OVERLAY_OUTLINE_COLOR, pen_width))
        painter.setBrush(OVERLAY_TEXT_COLOR)
        painter.drawEllipse(circle_rect)

        # Text is useful at 24px+, but too cramped at 16/20px.
        if size >= 24:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(OVERLAY_OUTLINE_COLOR))
            painter.setFont(QFont("Segoe UI", max(7, size // 4), QFont.Weight.Bold))
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "CPU")

        painter.end()
        icon.addPixmap(pixmap)

    return icon


_MUTEX_HANDLE = None

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """
    Set up standard logging to a rotating file.

    The app first tries to write the log next to the script for portable use,
    then falls back to the user's temporary directory. Console logging is added
    only when stderr exists, which avoids odd behavior under pythonw.exe/.pyw.
    """
    if logger.handlers:
        return

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    logger.setLevel(logging.INFO)

    log_paths: list[str] = []

    try:
        log_dir = os.path.dirname(os.path.abspath(__file__))
        log_paths.append(os.path.join(log_dir, "windows_cpu_limit_tray.log"))
    except Exception:
        pass

    try:
        import tempfile

        log_paths.append(os.path.join(tempfile.gettempdir(), "windows_cpu_limit_tray.log"))
    except Exception:
        pass

    for log_file in log_paths:
        try:
            handler = RotatingFileHandler(
                log_file,
                maxBytes=1024 * 1024,
                backupCount=2,
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            break
        except Exception:
            # Keep trying fallbacks; logging must not stop the app from running.
            continue

    # Also log to console if run with python.exe instead of pythonw.exe.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        logger.addHandler(console)

    logger.info("--- CPU Power Limit Overlay Started ---")


@dataclass
class CpuStatus:
    percent: Optional[int]
    source: str
    error: str = ""


@dataclass
class StatusResult:
    generation: int
    status: CpuStatus


def read_bool_setting(settings: QSettings, key: str, default: bool) -> bool:
    """
    Read a boolean from QSettings defensively.

    Depending on backend and Qt version, boolean values can come back as bools,
    ints, strings, or None.
    """
    value = settings.value(key, default)

    if isinstance(value, bool):
        return value

    if value is None:
        return default

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    return bool(value)


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_running_as_admin() -> bool:
    """
    Return True if the current process is elevated on Windows.

    This is only used to improve error messages. The app does not force an
    admin prompt at startup because many per-user power-plan edits work without
    elevation on normal Windows installs.
    """
    if not is_windows():
        return False

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception as e:
        logger.warning("Failed to check if running as admin", exc_info=True)
        return False


def add_powercfg_hint(error_message: str) -> str:
    """
    Add a practical, locale-safe hint after a powercfg failure.

    powercfg error text is localized by Windows, so avoid trying to detect
    permissions problems from English-only substrings.
    """
    message = error_message.strip() or "Unknown powercfg error."

    if is_running_as_admin():
        hint = (
            "If this is a permissions or policy issue, the setting may be "
            "locked by Windows policy or device-management software."
        )
    else:
        hint = (
            "If this is a permissions issue, try closing the app and running it "
            "as Administrator."
        )

    if hint not in message:
        message = f"{message}\n\n{hint}"

    return message


def acquire_single_instance_lock() -> bool:
    """
    Prevent two copies of the app from running.

    Returns True if this is the only running copy.
    Returns False if another copy already has the mutex.
    """
    if not is_windows():
        return True

    global _MUTEX_HANDLE

    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        handle = kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
        last_error = ctypes.get_last_error()

        if not handle:
            raise ctypes.WinError(last_error)

        # Keep the handle alive for the lifetime of the process.
        _MUTEX_HANDLE = handle

        # ERROR_ALREADY_EXISTS
        return last_error != 183
    except Exception as e:
        # If the mutex check fails, don't block the app from running.
        logger.warning("Failed to acquire single instance lock", exc_info=True)
        return True


def get_powercfg_executable() -> str:
    """
    Return a stable path to powercfg.exe when running on Windows.

    Using the Windows directory avoids relying on the current PATH for a system
    tool. The fallback keeps the error message useful if Windows is unusual or
    the app is run on another platform.
    """
    if not is_windows():
        return "powercfg.exe"

    windows_dir = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if windows_dir:
        candidates = [
            os.path.join(windows_dir, "System32", "powercfg.exe"),
            os.path.join(windows_dir, "Sysnative", "powercfg.exe"),
        ]

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate

    return "powercfg.exe"


def run_powercfg(args: list[str]) -> subprocess.CompletedProcess[str]:
    """
    Run powercfg.exe safely with a timeout and no visible console window.

    shell=False is intentional: arguments are passed directly and are not
    interpreted by a shell.
    """
    kwargs = {
        "capture_output": True,
        "text": True,
        "shell": False,
        "check": False,
        "timeout": POWERCFG_TIMEOUT_SECONDS,
    }

    if is_windows():
        # Without this, launching powercfg.exe from a GUI/.pyw app can flash a
        # small console window. One click runs two powercfg calls, so you may
        # see two flashes unless console creation is suppressed.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    return subprocess.run([get_powercfg_executable(), *args], **kwargs)


def set_cpu_max_ac(percent: int) -> tuple[bool, str]:
    """
    Set the AC / plugged-in maximum processor state for the current power plan.

    This intentionally targets the AC / plugged-in setting only.
    """
    percent = max(1, min(100, int(percent)))

    try:
        result = run_powercfg([
            "-setacvalueindex",
            "SCHEME_CURRENT",
            "SUB_PROCESSOR",
            "PROCTHROTTLEMAX",
            str(percent),
        ])

        if result.returncode != 0:
            error = (result.stderr or result.stdout or "Unknown powercfg error").strip()
            return False, add_powercfg_hint(error)

        result2 = run_powercfg(["-setactive", "SCHEME_CURRENT"])
        if result2.returncode != 0:
            error = (result2.stderr or result2.stdout or "Failed to re-activate power scheme").strip()
            return False, add_powercfg_hint(error)

        return True, f"CPU maximum processor state set to {percent}%."

    except subprocess.TimeoutExpired:
        return False, "powercfg.exe took too long to respond."
    except FileNotFoundError:
        return False, "powercfg.exe was not found. This app is intended for Windows."
    except Exception as exc:
        return False, str(exc)


def read_cpu_max_ac_from_registry() -> CpuStatus:
    """
    Read the active power plan's AC maximum processor state from the registry.

    This avoids calling powercfg.exe every few seconds and avoids localized
    command-output parsing.
    """
    if not is_windows():
        return CpuStatus(None, "registry", "Registry reading is only available on Windows.")

    try:
        import winreg

        base_path = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base_path) as base_key:
            active_scheme, _ = winreg.QueryValueEx(base_key, "ActivePowerScheme")

        active_scheme = str(active_scheme).strip().strip("{}")

        setting_path = (
            rf"{base_path}\{active_scheme}"
            rf"\{PROCESSOR_SETTINGS_SUBGROUP_GUID}"
            rf"\{MAX_PROCESSOR_STATE_GUID}"
        )

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, setting_path) as setting_key:
            ac_value, _ = winreg.QueryValueEx(setting_key, "ACSettingIndex")

        return CpuStatus(int(ac_value), "registry")

    except Exception as exc:
        return CpuStatus(None, "registry", str(exc))


def read_cpu_max_ac_from_powercfg() -> CpuStatus:
    """
    Fallback: read AC maximum processor state from powercfg.exe output.

    The English regex is preferred when available. The fallback uses the known
    order of the 0x values in this setting block: Current AC appears before
    Current DC, so AC is the second-to-last 0x value.
    """
    try:
        result = run_powercfg([
            "/query",
            "SCHEME_CURRENT",
            "SUB_PROCESSOR",
            "PROCTHROTTLEMAX",
        ])
    except subprocess.TimeoutExpired:
        return CpuStatus(None, "powercfg", "powercfg.exe took too long to respond.")
    except FileNotFoundError:
        return CpuStatus(None, "powercfg", "powercfg.exe was not found.")
    except Exception as exc:
        return CpuStatus(None, "powercfg", str(exc))

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "powercfg.exe returned an error.").strip()
        return CpuStatus(None, "powercfg", add_powercfg_hint(error))

    text = result.stdout + "\n" + result.stderr

    # English Windows output.
    match = re.search(
        r"Current\s+AC\s+Power\s+Setting\s+Index:\s*0x([0-9a-fA-F]+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return CpuStatus(int(match.group(1), 16), "powercfg")

    # Locale fallback. This query normally has:
    # Minimum Possible Setting
    # Maximum Possible Setting
    # Possible Setting Increment
    # Current AC Power Setting Index
    # Current DC Power Setting Index
    #
    # Therefore AC is second-to-last, not last.
    hex_values = re.findall(r"0x([0-9a-fA-F]{1,8})", text)

    if len(hex_values) >= 2:
        try:
            return CpuStatus(int(hex_values[-2], 16), "powercfg-fallback")
        except ValueError as exc:
            return CpuStatus(None, "powercfg-fallback", str(exc))

    return CpuStatus(None, "powercfg", "Could not find the AC CPU max value in powercfg output.")


def read_cpu_max_ac() -> CpuStatus:
    """
    Read current AC CPU max.

    Registry is preferred. powercfg is used only as a fallback.
    """
    registry_status = read_cpu_max_ac_from_registry()
    if registry_status.percent is not None:
        return registry_status

    powercfg_status = read_cpu_max_ac_from_powercfg()
    if powercfg_status.percent is not None:
        return powercfg_status

    combined_error = (
        f"Registry: {registry_status.error}\n"
        f"powercfg: {powercfg_status.error}"
    )
    return CpuStatus(None, "none", combined_error)


class WorkerSignals(QObject):
    apply_finished = Signal(bool, str, int)
    restore_exit_finished = Signal(bool, str)
    status_finished = Signal(object)


class TextOverlay(QWidget):
    """
    Small transparent text-only overlay.

    The overlay is click-through where Qt/Windows supports it.
    """

    def __init__(self, owner: QWidget) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )

        # Extra click-through support on newer Qt versions. WA_TransparentForMouseEvents
        # below is still the primary fallback for older Qt/Windows combinations.
        if hasattr(Qt.WindowType, "WindowTransparentForInput"):
            flags |= Qt.WindowType.WindowTransparentForInput

        super().__init__(None, flags)

        self.owner = owner

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.text = ""
        self.font = QFont("Segoe UI", OVERLAY_FONT_POINT_SIZE, QFont.Weight.DemiBold)

        self.margin_x = OVERLAY_MARGIN_X
        self.margin_y = OVERLAY_MARGIN_Y

    def set_percent(self, percent: int) -> None:
        self.text = f"CPU {percent}%"
        self.resize_to_text()
        self.move_top_right()
        self.update()
        self.show()
        self.raise_()

    def target_screen_geometry(self):
        """
        Prefer the screen where the main app window is located.

        This helps with multi-monitor setups and different scaling per monitor.
        """
        screen = None

        try:
            center = self.owner.frameGeometry().center()
            screen = QGuiApplication.screenAt(center)
        except Exception as e:
            logger.warning("Failed to get screen at window center", exc_info=True)
            screen = None

        if screen is None:
            screen = QGuiApplication.primaryScreen()

        return screen.availableGeometry()

    def resize_to_text(self) -> None:
        metrics = QFontMetrics(self.font)
        rect = metrics.boundingRect(self.text)

        width = rect.width() + self.margin_x * 2 + 8
        height = rect.height() + self.margin_y * 2 + 6

        # Keep a sensible minimum so it does not clip at unusual DPI settings.
        self.resize(max(width, 62), max(height, 24))

    def move_top_right(self) -> None:
        area = self.target_screen_geometry()

        x = area.right() - self.width() - OVERLAY_RIGHT_OFFSET
        y = area.top() + OVERLAY_TOP_OFFSET

        self.move(QPoint(x, y))

    def refresh_layout(self) -> None:
        """
        Recalculate size and position after screen/DPI changes.
        """
        if not self.text:
            return

        self.resize_to_text()
        self.move_top_right()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt method name
        if not self.text:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setFont(self.font)

        rect = QRect(
            self.margin_x,
            self.margin_y,
            self.width() - self.margin_x * 2,
            self.height() - self.margin_y * 2,
        )

        painter.setPen(QPen(OVERLAY_OUTLINE_COLOR))
        for dx, dy in (
            (-1, -1), (-1, 0), (-1, 1),
            (0, -1),           (0, 1),
            (1, -1),  (1, 0),  (1, 1),
        ):
            painter.drawText(rect.adjusted(dx, dy, dx, dy), Qt.AlignmentFlag.AlignCenter, self.text)

        painter.setPen(QPen(OVERLAY_TEXT_COLOR))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text)


class CpuPowerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle(APP_TITLE)
        self.resize(450, 270)
        self.setMinimumSize(450, 270)

        self.overlay = TextOverlay(self)
        self.settings = QSettings(SETTINGS_ORGANIZATION, SETTINGS_APPLICATION)

        self.signals = WorkerSignals()
        self.signals.apply_finished.connect(self.on_apply_finished)
        self.signals.restore_exit_finished.connect(self.on_restore_exit_finished)
        self.signals.status_finished.connect(self.on_status_finished)

        self.apply_in_progress = False
        self.status_poll_in_progress = False
        self.status_generation = 0
        self.last_percent: Optional[int] = None

        self.preset_buttons: list[QPushButton] = []

        self.status_label = QLabel("Checking current CPU max...")
        self.status_label.setWordWrap(True)

        self.overlay_checkbox = QCheckBox("Show small overlay when CPU max is below 100%")
        self.overlay_checkbox.setChecked(
            read_bool_setting(self.settings, "show_overlay", True)
        )
        self.overlay_checkbox.stateChanged.connect(self.on_overlay_checkbox_changed)
        self.overlay_checkbox.stateChanged.connect(self.save_overlay_setting)

        self.warn_on_exit_checkbox = QCheckBox("Warn before quitting when CPU max is below 100%")
        self.warn_on_exit_checkbox.setChecked(
            read_bool_setting(
                self.settings,
                "warn_on_exit_below_100",
                WARN_BEFORE_EXIT_BELOW_100_DEFAULT,
            )
        )
        self.warn_on_exit_checkbox.stateChanged.connect(self.save_warn_on_exit_setting)

        self.tray_icon: Optional[QSystemTrayIcon] = None
        self.tray_show_action: Optional[QAction] = None
        self.tray_hide_action: Optional[QAction] = None
        self.tray_overlay_action: Optional[QAction] = None
        self.tray_exit_requested = False
        self.tray_message_shown = False

        self.build_ui()
        self.setup_tray()
        self.connect_screen_change_signals()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(POLL_MS)

        self.refresh_status()

    def build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        title = QLabel("Windows CPU Limit Tray")
        title_font = QFont("Segoe UI", 12, QFont.Weight.Bold)
        title.setFont(title_font)

        subtitle = QLabel("Controls the AC / plugged-in setting for the current Windows power plan.")
        subtitle.setWordWrap(True)

        preset_grid = QGridLayout()
        preset_grid.setHorizontalSpacing(8)
        preset_grid.setVerticalSpacing(8)

        for index, value in enumerate(CPU_PRESETS):
            button = QPushButton(f"{value}%")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, p=value: self.apply_percent(p))

            # Make 100% slightly easier to spot.
            if value == 100:
                font = button.font()
                font.setBold(True)
                button.setFont(font)

            self.preset_buttons.append(button)

            row = index // PRESET_COLUMNS
            col = index % PRESET_COLUMNS
            preset_grid.addWidget(button, row, col)

        bottom = QHBoxLayout()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_status)

        minimize_btn = QPushButton("Minimize")
        minimize_btn.setToolTip("Hide the control window to the system tray when available.")
        minimize_btn.clicked.connect(self.minimize_window)

        quit_btn = QPushButton("Quit")
        quit_btn.clicked.connect(self.request_exit)

        bottom.addWidget(refresh_btn)
        bottom.addStretch(1)
        bottom.addWidget(minimize_btn)
        bottom.addWidget(quit_btn)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(6)
        layout.addLayout(preset_grid)
        layout.addSpacing(10)
        layout.addWidget(self.status_label)
        layout.addWidget(self.overlay_checkbox)
        layout.addWidget(self.warn_on_exit_checkbox)
        layout.addStretch(1)
        layout.addLayout(bottom)

        self.setCentralWidget(central)

    def setup_tray(self) -> None:
        """
        Create the Windows system tray icon and menu.

        The tray controls the main window, while the floating CPU overlay stays
        controlled by the overlay checkbox and the current CPU percentage.
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        icon = create_green_tray_icon()

        # Set both the window/app icon and the tray icon so Windows has no
        # opportunity to fall back to the default Python/Qt icon.
        self.setWindowIcon(icon)
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(icon)

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(icon)
        self.tray_icon.setToolTip(APP_TITLE)

        tray_menu = QMenu(self)

        self.tray_show_action = QAction("Show window", self)
        self.tray_show_action.triggered.connect(self.show_from_tray)

        self.tray_hide_action = QAction("Hide window to tray", self)
        self.tray_hide_action.triggered.connect(lambda: self.hide_to_tray(show_message=False))

        refresh_action = QAction("Refresh status", self)
        refresh_action.triggered.connect(lambda: self.refresh_status(force=True))

        restore_exit_action = QAction("Restore 100% and Quit", self)
        restore_exit_action.triggered.connect(self.restore_100_and_exit)

        self.tray_overlay_action = QAction("Show CPU overlay", self)
        self.tray_overlay_action.setCheckable(True)
        self.tray_overlay_action.setChecked(self.overlay_checkbox.isChecked())
        self.tray_overlay_action.toggled.connect(self.set_overlay_enabled_from_tray)

        exit_action = QAction("Quit", self)
        exit_action.triggered.connect(self.request_exit)

        tray_menu.addAction(self.tray_show_action)
        tray_menu.addAction(self.tray_hide_action)
        tray_menu.addAction(refresh_action)
        tray_menu.addSeparator()
        tray_menu.addAction(self.tray_overlay_action)
        tray_menu.addSeparator()
        tray_menu.addAction(restore_exit_action)
        tray_menu.addAction(exit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()
        self.tray_icon.setIcon(icon)  # Re-apply after show; helps avoid stale Windows tray rendering.

        self.update_tray_actions()
        self.update_tray_tooltip()

    def minimize_window(self) -> None:
        """
        Minimize from the in-window button.

        If the tray icon exists, hide the control window to the tray. If the
        tray is unavailable, fall back to normal taskbar minimize so the user
        does not lose the window.
        """
        if (
            self.tray_icon is not None
            and self.tray_icon.isVisible()
            and not self.tray_exit_requested
        ):
            self.hide_to_tray(show_message=False)
        else:
            self.showMinimized()
            self.update_overlay_from_last_status()
            self.update_tray_actions()

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.update_overlay_from_last_status()
        self.update_tray_actions()

    def hide_to_tray(self, show_message: bool = True) -> None:
        self.hide()

        # Important: do not hide self.overlay here. It is a separate status
        # overlay and should remain visible whenever the checkbox + CPU percent
        # say it should be visible.
        self.update_overlay_from_last_status()
        self.update_tray_actions()

        if show_message:
            self.show_tray_message_once()

    def show_tray_message_once(self) -> None:
        if self.tray_icon is None or self.tray_message_shown:
            return

        self.tray_icon.showMessage(
            APP_TITLE,
            "Still running in the system tray.",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )
        self.tray_message_shown = True

    def on_tray_activated(self, reason) -> None:
        if reason not in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            return

        if self.isVisible() and not self.isMinimized():
            self.hide_to_tray(show_message=False)
        else:
            self.show_from_tray()

    def set_overlay_enabled_from_tray(self, checked: bool) -> None:
        if self.overlay_checkbox.isChecked() != checked:
            self.overlay_checkbox.setChecked(checked)
        else:
            self.update_overlay_from_last_status()
            self.update_tray_actions()

    def on_overlay_checkbox_changed(self, *args) -> None:
        self.update_overlay_from_last_status()
        self.update_tray_actions()

    def save_overlay_setting(self, *args) -> None:
        self.settings.setValue("show_overlay", self.overlay_checkbox.isChecked())

    def save_warn_on_exit_setting(self, *args) -> None:
        self.settings.setValue(
            "warn_on_exit_below_100",
            self.warn_on_exit_checkbox.isChecked(),
        )

    def update_tray_actions(self) -> None:
        if self.tray_icon is None:
            return

        visible_and_not_minimized = self.isVisible() and not self.isMinimized()

        if self.tray_show_action is not None:
            self.tray_show_action.setEnabled(not visible_and_not_minimized)

        if self.tray_hide_action is not None:
            self.tray_hide_action.setEnabled(visible_and_not_minimized)

        if self.tray_overlay_action is not None:
            self.tray_overlay_action.blockSignals(True)
            self.tray_overlay_action.setChecked(self.overlay_checkbox.isChecked())
            self.tray_overlay_action.blockSignals(False)

    def update_tray_tooltip(self) -> None:
        if self.tray_icon is None:
            return

        if self.last_percent is None:
            self.tray_icon.setToolTip(APP_TITLE)
        else:
            self.tray_icon.setToolTip(
                f"{APP_TITLE}\nCurrent AC CPU max: {self.last_percent}%"
            )

    def request_exit(self) -> None:
        """
        Handle user-requested exits from the button or tray menu.
        """
        decision = self.exit_decision()

        if decision == "cancel":
            return

        if decision == "restore":
            self.restore_100_and_exit()
            return

        self.exit_app()

    def exit_decision(self) -> str:
        """
        Return what to do when the user tries to quit.

        Possible return values are:
        - "exit" for a normal exit
        - "restore" to restore CPU max to 100% before exiting
        - "cancel" to stay open
        """
        if self.tray_exit_requested:
            return "exit"

        if self.apply_in_progress:
            self.status_label.setText("A CPU limit change is already in progress.")
            QMessageBox.information(
                self,
                APP_TITLE,
                "A CPU limit change is already in progress. Please wait for it to finish before quitting.",
            )
            return "cancel"

        if not self.warn_on_exit_checkbox.isChecked():
            return "exit"

        if self.last_percent is None or self.last_percent == 100:
            return "exit"

        message_box = QMessageBox(self)
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setWindowTitle(APP_TITLE)
        message_box.setText(
            f"CPU maximum processor state is currently set to {self.last_percent}%."
        )
        message_box.setInformativeText(
            "Quitting now will leave this Windows power setting active.\n\n"
            "What would you like to do?"
        )

        restore_button = message_box.addButton(
            "Restore 100% and Quit",
            QMessageBox.ButtonRole.AcceptRole,
        )
        exit_anyway_button = message_box.addButton(
            "Quit Anyway",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        cancel_button = message_box.addButton(QMessageBox.StandardButton.Cancel)

        message_box.setDefaultButton(restore_button)
        message_box.setEscapeButton(cancel_button)
        message_box.exec()

        clicked_button = message_box.clickedButton()

        if clicked_button == restore_button:
            return "restore"

        if clicked_button == exit_anyway_button:
            return "exit"

        return "cancel"

    def restore_100_and_exit(self) -> None:
        """
        Restore the CPU maximum processor state to 100% before quitting.
        """
        if self.apply_in_progress:
            message = "A CPU limit change is already in progress. Try again in a moment."
            self.status_label.setText(message)

            if self.tray_icon is not None and self.tray_icon.isVisible():
                self.tray_icon.showMessage(
                    APP_TITLE,
                    message,
                    QSystemTrayIcon.MessageIcon.Warning,
                    2500,
                )
            else:
                QMessageBox.information(self, APP_TITLE, message)

            return

        self.apply_in_progress = True
        self.set_buttons_enabled(False)
        self.status_label.setText("Restoring CPU max to 100% before quitting...")

        def worker() -> None:
            try:
                ok, message = set_cpu_max_ac(100)
            except Exception as exc:
                ok = False
                message = f"Unexpected error while restoring CPU max: {exc}"

            self.signals.restore_exit_finished.emit(ok, message)

        threading.Thread(target=worker, daemon=True).start()

    def on_restore_exit_finished(self, ok: bool, message: str) -> None:
        self.apply_in_progress = False
        self.set_buttons_enabled(True)

        if ok:
            self.last_percent = 100
            self.update_overlay_from_last_status()
            self.exit_app()
            return

        self.status_label.setText("Failed to restore CPU max to 100%.")
        QMessageBox.critical(
            self,
            APP_TITLE,
            f"Could not restore CPU max to 100% before quitting.\n\n{message}",
        )
        self.refresh_status(force=True)

    def exit_app(self) -> None:
        self.tray_exit_requested = True
        self.overlay.close()

        if self.tray_icon is not None:
            self.tray_icon.hide()

        QApplication.quit()

    def connect_screen_change_signals(self) -> None:
        """
        Keep the overlay position/size fresh if monitors or DPI settings change
        while the app is running.
        """
        app = QGuiApplication.instance()
        if app is None:
            return

        for signal_name in ("screenAdded", "screenRemoved", "primaryScreenChanged"):
            signal = getattr(app, signal_name, None)
            if signal is not None:
                try:
                    signal.connect(self.refresh_overlay_layout_soon)
                except Exception as e:
                    logger.warning(f"Failed to connect signal {signal_name}", exc_info=True)

        for screen in QGuiApplication.screens():
            self.connect_single_screen_signals(screen)

    def connect_single_screen_signals(self, screen) -> None:
        for signal_name in (
            "geometryChanged",
            "availableGeometryChanged",
            "logicalDotsPerInchChanged",
        ):
            signal = getattr(screen, signal_name, None)
            if signal is not None:
                try:
                    signal.connect(self.refresh_overlay_layout_soon)
                except Exception as e:
                    logger.warning(f"Failed to connect screen signal {signal_name}", exc_info=True)

    def refresh_overlay_layout_soon(self, *args) -> None:
        # If a screen was added, hook its signals too.
        if args:
            possible_screen = args[0]
            if hasattr(possible_screen, "geometry"):
                self.connect_single_screen_signals(possible_screen)

        QTimer.singleShot(0, self.refresh_overlay_layout)

    def refresh_overlay_layout(self) -> None:
        if self.overlay.isVisible():
            self.overlay.refresh_layout()

    def set_buttons_enabled(self, enabled: bool) -> None:
        for button in self.preset_buttons:
            button.setEnabled(enabled)

    def maybe_confirm_low_preset(self, percent: int) -> bool:
        if not CONFIRM_LOW_PRESETS:
            return True

        if percent > LOW_PRESET_WARNING_AT_OR_BELOW:
            return True

        answer = QMessageBox.question(
            self,
            APP_TITLE,
            f"Apply {percent}% CPU maximum processor state?\n\n"
            "This can make the system feel very slow until you restore a higher value.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        return answer == QMessageBox.StandardButton.Yes

    def apply_percent(self, percent: int) -> None:
        if self.apply_in_progress:
            return

        if not self.maybe_confirm_low_preset(percent):
            return

        self.apply_in_progress = True
        self.set_buttons_enabled(False)
        self.status_label.setText(f"Applying {percent}%...")

        def worker() -> None:
            try:
                ok, message = set_cpu_max_ac(percent)
            except Exception as exc:
                ok = False
                message = f"Unexpected error while applying CPU max: {exc}"

            self.signals.apply_finished.emit(ok, message, percent)

        threading.Thread(target=worker, daemon=True).start()

    def on_apply_finished(self, ok: bool, message: str, percent: int) -> None:
        self.apply_in_progress = False
        self.set_buttons_enabled(True)

        if ok:
            self.status_label.setText(message)
            self.refresh_status(force=True)
        else:
            self.status_label.setText("Failed to change CPU max.")
            QMessageBox.critical(
                self,
                APP_TITLE,
                f"Could not set CPU max to {percent}%.\n\n{message}",
            )
            self.refresh_status(force=True)

    def refresh_status(self, force: bool = False) -> None:
        """
        Poll status in a background thread so slow registry/powercfg reads
        cannot freeze the GUI.

        Forced refreshes are allowed to start a newer poll while an older poll
        is still running. A generation number prevents old/stale poll results
        from overwriting newer status.
        """
        if self.status_poll_in_progress and not force:
            return

        self.status_generation += 1
        generation = self.status_generation
        self.status_poll_in_progress = True

        def worker() -> None:
            try:
                status = read_cpu_max_ac()
            except Exception as exc:
                status = CpuStatus(None, "error", f"Unexpected error while reading CPU max: {exc}")

            self.signals.status_finished.emit(StatusResult(generation, status))

        threading.Thread(target=worker, daemon=True).start()

    def on_status_finished(self, result: StatusResult) -> None:
        if result.generation != self.status_generation:
            return

        self.status_poll_in_progress = False
        status = result.status

        if status.percent is None:
            self.last_percent = None
            self.status_label.setText(
                "Could not read CPU max. Make sure this is running on Windows.\n\n"
                f"{status.error}"
            )
            self.overlay.hide()
            self.update_preset_buttons()
            self.update_tray_tooltip()
            return

        self.last_percent = int(status.percent)
        self.status_label.setText(
            f"Current AC CPU maximum processor state: {self.last_percent}%"
        )

        self.update_overlay_from_last_status()
        self.update_preset_buttons()
        self.update_tray_tooltip()

    def update_preset_buttons(self) -> None:
        for button in self.preset_buttons:
            button.blockSignals(True)
            if self.last_percent is not None and button.text() == f"{self.last_percent}%":
                button.setChecked(True)
            else:
                button.setChecked(False)
            button.blockSignals(False)

    def update_overlay_from_last_status(self) -> None:
        if self.last_percent is None:
            self.overlay.hide()
            return

        if self.overlay_checkbox.isChecked() and self.last_percent != 100:
            self.overlay.set_percent(self.last_percent)
        else:
            self.overlay.hide()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt method name
        super().moveEvent(event)
        if hasattr(self, "overlay") and self.overlay.isVisible():
            self.overlay.move_top_right()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt method name
        super().changeEvent(event)

        if (
            event.type() == QEvent.Type.WindowStateChange
            and MINIMIZE_BUTTON_HIDES_TO_TRAY
            and self.isMinimized()
            and not self.tray_exit_requested
            and self.tray_icon is not None
            and self.tray_icon.isVisible()
        ):
            # Let Qt finish the minimize transition, then hide the window from
            # the taskbar so it lives only in the tray.
            QTimer.singleShot(0, lambda: self.hide_to_tray(show_message=False))

    def showEvent(self, event) -> None:  # noqa: N802 - Qt method name
        super().showEvent(event)
        self.update_overlay_from_last_status()
        self.update_tray_actions()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
        if (
            CLOSE_BUTTON_HIDES_TO_TRAY
            and not self.tray_exit_requested
            and self.tray_icon is not None
            and self.tray_icon.isVisible()
        ):
            event.ignore()
            self.hide_to_tray(show_message=False)
            return

        decision = self.exit_decision()

        if decision == "cancel":
            event.ignore()
            return

        if decision == "restore":
            event.ignore()
            self.restore_100_and_exit()
            return

        event.accept()
        self.exit_app()


def main() -> int:
    setup_logging()

    # Qt 6 is high-DPI aware by default. This rounding policy keeps fractional
    # scales like 175% from getting rounded too aggressively.
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception as e:
        logger.warning("Failed to set HighDpiScaleFactorRoundingPolicy", exc_info=True)

    app = QApplication(sys.argv)

    if not acquire_single_instance_lock():
        QMessageBox.information(
            None,
            APP_TITLE,
            "CPU Power Limit is already running.",
        )
        return 0

    if not is_windows():
        QMessageBox.warning(
            None,
            APP_TITLE,
            "This app is intended for Windows because it uses powercfg.exe and Windows power settings.",
        )
        return 1

    window = CpuPowerWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
