import configparser
import ctypes
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from ctypes import wintypes
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog

from PIL import Image, ImageDraw
import pystray
from pystray._util import win32 as pystray_win32


@dataclass
class Settings:
    manual: int = 35
    day: int = 40
    night: int = 25
    day_start: str = "08:00"
    night_start: str = "20:00"
    auto_enabled: bool = True
    theme: str = "dark"


class HdrSdrTrayApp:
    RELEASE_ZIP_URL = "https://github.com/ledoge/set_maxtml/releases/download/v0.2/release.zip"
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_ALT = 0x0001
    MOD_WIN = 0x0008
    VK_S = 0x53
    HOTKEY_ID = 1
    WM_MOUSEFIRST = 0x0200
    WM_MOUSELAST = 0x020E
    WM_MOUSEWHEEL = 0x020A
    WM_INPUT = 0x00FF
    RID_INPUT = 0x10000003
    RIDEV_INPUTSINK = 0x00000100
    RIM_TYPEMOUSE = 0
    RI_MOUSE_WHEEL = 0x0400
    NOTIFYICON_VERSION_4 = 4
    WH_MOUSE_LL = 14
    HC_ACTION = 0
    TRAY_HOVER_GRACE_SECONDS = 1.8
    TRAY_HOVER_RADIUS_PX = 72
    WHEEL_MISS_LOG_INTERVAL_SECONDS = 1.2
    WHEEL_QUEUE_WARN_THRESHOLD = 120
    WHEEL_DIAG_LOG_INTERVAL_SECONDS = 3.0
    LOG_FILENAME = "HDR-SDR-Brightness.log"

    def __init__(self) -> None:
        self.app_dir = self._get_app_dir()
        self.resource_dir = self._get_resource_dir()
        self.data_dir = self._get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "config.ini"

        self.log_path: Path | None = None
        self.debug_enabled = self._is_debug_enabled()
        self.logger = self._init_logger()
        self._log_info("app-start app_dir=%s data_dir=%s frozen=%s", self.app_dir, self.data_dir, getattr(sys, "frozen", False))

        self.settings = self._load_settings()
        self.last_applied_percent: int | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("HDR SDR Brightness")

        self.slider_window: tk.Toplevel | None = None
        self.slider_var = tk.IntVar(value=self.settings.manual)
        self.hotkey_registered = False
        self.slider_canvas: tk.Canvas | None = None
        self.slider_canvas_width = 176
        self.slider_canvas_height = 22
        self.slider_track_left = 7
        self.slider_track_right = self.slider_canvas_width - 7
        self.slider_track_y = self.slider_canvas_height // 2
        self.slider_dragging = False
        self.sun_icon: tk.Label | None = None
        self.slider_shown_at = 0.0
        self._tray_handlers_installed = False
        self._raw_input_registered = False
        self._orig_taskbarcreated_handler = None
        self._orig_notify_handler = None
        self._notify_event_count = 0
        self._notify_log_budget = 60
        self._tray_icon_rect_cache: tuple[int, int, int, int, float] | None = None
        self._tray_uid_candidates: list[int] = []
        self._tray_uid_preferred: int | None = None
        self._tray_hover_until = 0.0
        self._tray_last_anchor: tuple[int, int] | None = None
        self._tray_bad_anchor_logged_at = 0.0
        self._tray_rect_fail_logged_at = 0.0
        self._wheel_hook_thread: threading.Thread | None = None
        self._wheel_hook_thread_id: int | None = None
        self._wheel_hook_handle = None
        self._wheel_hook_proc = None
        self._wheel_hook_events = 0
        self._wheel_hook_miss_count = 0
        self._wheel_hook_miss_logged_at = 0.0
        self._wheel_dispatch_lock = threading.Lock()
        self._wheel_delta_pending = 0
        self._wheel_flush_scheduled = False
        self._wheel_enqueued_count = 0
        self._wheel_flushed_count = 0
        self._wheel_diag_logged_at = 0.0
        self._rawinput_device_type = None
        self._rawinput_header_type = None
        self._rawinput_type = None
        self._notify_icon_identifier_type = None
        self._init_win_struct_types()

        self.icon = pystray.Icon(
            "hdr_sdr_brightness",
            self._build_icon_image(),
            "HDR SDR Brightness",
            menu=self._build_menu(),
        )
        self._init_tray_uid_candidates()

        self._apply_root_theme()
        self.root.after(500, self._initial_apply)
        self.root.after(60_000, self._schedule_tick)
        if self.debug_enabled:
            self.root.after(15_000, self._debug_mem_tick)
        self._setup_hotkey()

    def _init_tray_uid_candidates(self) -> None:
        # pystray win32 backend currently writes hID (not uID), which means the
        # effective uID may be 0 in NOTIFYICONDATA on some versions.
        cands: list[int] = [0]
        try:
            cands.append(ctypes.c_uint(id(self.icon)).value)
        except Exception:
            pass
        seen = set()
        self._tray_uid_candidates = []
        for uid in cands:
            if uid in seen:
                continue
            seen.add(uid)
            self._tray_uid_candidates.append(uid)

    def _init_win_struct_types(self) -> None:
        if os.name != "nt":
            return

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class NOTIFYICONIDENTIFIER(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("guidItem", GUID),
            ]

        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [
                ("usUsagePage", ctypes.c_ushort),
                ("usUsage", ctypes.c_ushort),
                ("dwFlags", ctypes.c_uint),
                ("hwndTarget", wintypes.HWND),
            ]

        class RAWINPUTHEADER(ctypes.Structure):
            _fields_ = [
                ("dwType", wintypes.DWORD),
                ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE),
                ("wParam", wintypes.WPARAM),
            ]

        class RAWMOUSE_BUTTONS(ctypes.Structure):
            _fields_ = [
                ("usButtonFlags", ctypes.c_ushort),
                ("usButtonData", ctypes.c_ushort),
            ]

        class RAWMOUSE_UNION(ctypes.Union):
            _fields_ = [
                ("ulButtons", wintypes.ULONG),
                ("buttons", RAWMOUSE_BUTTONS),
            ]

        class RAWMOUSE(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [
                ("usFlags", ctypes.c_ushort),
                ("u", RAWMOUSE_UNION),
                ("ulRawButtons", wintypes.ULONG),
                ("lLastX", ctypes.c_long),
                ("lLastY", ctypes.c_long),
                ("ulExtraInformation", wintypes.ULONG),
            ]

        class RAWINPUT_UNION(ctypes.Union):
            _fields_ = [
                ("mouse", RAWMOUSE),
                ("_padding", ctypes.c_byte * 40),
            ]

        class RAWINPUT(ctypes.Structure):
            _anonymous_ = ("data",)
            _fields_ = [
                ("header", RAWINPUTHEADER),
                ("data", RAWINPUT_UNION),
            ]

        self._rawinput_device_type = RAWINPUTDEVICE
        self._rawinput_header_type = RAWINPUTHEADER
        self._rawinput_type = RAWINPUT
        self._notify_icon_identifier_type = NOTIFYICONIDENTIFIER

    def _debug_mem_tick(self) -> None:
        if not self.debug_enabled:
            return
        self._log_info(
            "mem-sample private_mb=%.1f wheel_enqueued=%s wheel_flushed=%s hook_events=%s",
            self._get_private_mem_mb(),
            self._wheel_enqueued_count,
            self._wheel_flushed_count,
            self._wheel_hook_events,
        )
        self.root.after(15_000, self._debug_mem_tick)

    def run(self) -> None:
        tray_thread = threading.Thread(target=self.icon.run, daemon=True, name="tray-thread")
        tray_thread.start()
        self._log_info("run tray-thread-started")
        self.root.after(900, self._setup_tray_wheel_support)
        self.root.after(1500, self._setup_wheel_hook_support)
        self.root.mainloop()

    def _get_app_dir(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def _get_resource_dir(self) -> Path:
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return Path(getattr(sys, "_MEIPASS"))
        return Path(__file__).resolve().parent

    def _get_data_dir(self) -> Path:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "HDR-SDR-Brightness"
        return self.app_dir

    @staticmethod
    def _is_debug_enabled() -> bool:
        for arg in sys.argv[1:]:
            if arg.strip().lower() == "-debug":
                return True
        return False

    def _init_logger(self) -> logging.Logger | None:
        if not self.debug_enabled:
            return None

        logger = logging.getLogger(f"hdr_sdr_{id(self)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for h in list(logger.handlers):
            logger.removeHandler(h)

        primary = self.app_dir / self.LOG_FILENAME
        fallback = self.data_dir / self.LOG_FILENAME
        try:
            handler = RotatingFileHandler(primary, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
            self.log_path = primary
        except Exception:
            handler = RotatingFileHandler(fallback, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
            self.log_path = fallback

        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s"))
        logger.addHandler(handler)
        logger.info("logger-initialized path=%s", self.log_path)
        return logger

    def _log_info(self, msg: str, *args) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            logger.info(msg, *args)
        except Exception:
            pass

    def _log_warn(self, msg: str, *args) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            logger.warning(msg, *args)
        except Exception:
            pass

    def _log_error(self, msg: str, *args) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            logger.error(msg, *args)
        except Exception:
            pass

    def _startup_shortcut_path(self) -> Path:
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        return startup / "HDR-SDR-Brightness.lnk"

    def _is_startup_enabled(self) -> bool:
        return self._startup_shortcut_path().exists()

    def _set_startup_enabled(self, enabled: bool) -> None:
        link_path = self._startup_shortcut_path()
        if enabled:
            link_path.parent.mkdir(parents=True, exist_ok=True)

            if getattr(sys, "frozen", False):
                target = str(Path(sys.executable).resolve())
                args = ""
                workdir = str(Path(sys.executable).resolve().parent)
                icon = target
            else:
                target = str(Path(sys.executable).resolve())
                args = f'"{Path(__file__).resolve()}"'
                workdir = str(Path(__file__).resolve().parent)
                icon = target

            def esc(text: str) -> str:
                return text.replace("'", "''")

            ps = (
                "$WshShell = New-Object -ComObject WScript.Shell; "
                f"$Shortcut = $WshShell.CreateShortcut('{esc(str(link_path))}'); "
                f"$Shortcut.TargetPath = '{esc(target)}'; "
                f"$Shortcut.Arguments = '{esc(args)}'; "
                f"$Shortcut.WorkingDirectory = '{esc(workdir)}'; "
                f"$Shortcut.IconLocation = '{esc(icon)}'; "
                "$Shortcut.Save();"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                check=True,
                creationflags=0x08000000,
            )
        else:
            if link_path.exists():
                link_path.unlink()

    def _build_icon_image(self) -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        cx = 32
        cy = 32
        ray_inner = 17
        ray_outer = 27
        ray_color = (236, 173, 35, 255)
        for i in range(8):
            a = i * math.pi / 4.0
            x1 = cx + math.cos(a) * ray_inner
            y1 = cy + math.sin(a) * ray_inner
            x2 = cx + math.cos(a) * ray_outer
            y2 = cy + math.sin(a) * ray_outer
            d.line((x1, y1, x2, y2), fill=ray_color, width=4)

        d.ellipse((15, 15, 49, 49), fill=(249, 193, 47, 255))
        d.ellipse((21, 21, 43, 43), fill=(255, 224, 120, 255))
        return img

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("打开滑块", self._on_open_slider, default=True),
            pystray.MenuItem("立即应用", self._on_apply_now),
            pystray.MenuItem(
                "自动时间调节",
                self._on_toggle_auto,
                checked=lambda _: self.settings.auto_enabled,
            ),
            pystray.MenuItem(
                "开机自启动",
                self._on_toggle_startup,
                checked=lambda _: self._is_startup_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("主题-浅色", self._on_set_light_theme, checked=lambda _: self.settings.theme == "light"),
            pystray.MenuItem("主题-深色", self._on_set_dark_theme, checked=lambda _: self.settings.theme == "dark"),
            pystray.MenuItem("快捷键: Win+Alt+S", lambda icon, item: None, enabled=False),
            pystray.MenuItem("编辑时间计划", self._on_edit_schedule),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._on_exit),
        )

    def _setup_hotkey(self) -> None:
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        ok = user32.RegisterHotKey(None, self.HOTKEY_ID, self.MOD_WIN | self.MOD_ALT, self.VK_S)
        self.hotkey_registered = bool(ok)
        if self.hotkey_registered:
            t = threading.Thread(target=self._hotkey_loop, daemon=True)
            t.start()

    def _hotkey_loop(self) -> None:
        msg = wintypes.MSG()
        user32 = ctypes.windll.user32
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                self.root.after(0, self._show_slider)
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _setup_tray_wheel_support(self) -> None:
        if os.name != "nt":
            self._log_warn("wheel-setup skipped non-windows")
            return
        hwnd = getattr(self.icon, "_hwnd", None)
        if not hwnd:
            self._log_info("wheel-setup tray-hwnd-missing retry")
            self.root.after(700, self._setup_tray_wheel_support)
            return

        self._log_info("wheel-setup tray-hwnd=%s", hwnd)
        self._log_info("wheel-setup tray-uid-candidates=%s preferred=%s", self._tray_uid_candidates, self._tray_uid_preferred)
        self._install_tray_message_handlers()
        self._set_notify_icon_version()
        self._register_raw_mouse_input(hwnd)

    def _install_tray_message_handlers(self) -> None:
        if self._tray_handlers_installed:
            self._log_info("wheel-handlers already-installed")
            return
        try:
            handlers = self.icon._message_handlers
            self._orig_taskbarcreated_handler = handlers.get(pystray_win32.WM_TASKBARCREATED)
            self._orig_notify_handler = handlers.get(pystray_win32.WM_NOTIFY)
            handlers[self.WM_INPUT] = self._on_tray_raw_input
            handlers[pystray_win32.WM_NOTIFY] = self._on_tray_notify
            handlers[pystray_win32.WM_TASKBARCREATED] = self._on_tray_taskbarcreated
            self._tray_handlers_installed = True
            self._log_info(
                "wheel-handlers installed notify_handler=%s",
                "yes" if self._orig_notify_handler else "no",
            )
        except Exception as ex:
            self._tray_handlers_installed = False
            self._log_error("wheel-handlers install-failed error=%s", ex)

    def _on_tray_notify(self, wparam, lparam):
        raw_w = int(wparam) & 0xFFFFFFFF
        raw_l = int(lparam) & 0xFFFFFFFF
        event_full = raw_l
        event_low = raw_l & 0xFFFF
        event_high = (raw_l >> 16) & 0xFFFF

        self._notify_event_count += 1
        if self._notify_event_count <= self._notify_log_budget:
            self._log_info(
                "wheel-notify raw count=%s l=0x%08X low=0x%04X high=0x%04X w=0x%08X",
                self._notify_event_count,
                raw_l,
                event_low,
                event_high,
                raw_w,
            )

        event_code = self._extract_notify_event_code(event_full, event_low)
        if self.WM_MOUSEFIRST <= event_code <= self.WM_MOUSELAST:
            self._mark_tray_hover(raw_w)
        if event_code == self.WM_MOUSEWHEEL:
            delta = self._extract_notify_wheel_delta(raw_w, raw_l)
            self._log_info(
                "wheel-notify wheel-event delta=%s x=%s y=%s raww=0x%08X rawl=0x%08X",
                delta,
                self._signed_word(raw_w & 0xFFFF),
                self._signed_word((raw_w >> 16) & 0xFFFF),
                raw_w,
                raw_l,
            )
            if delta != 0:
                self._enqueue_wheel_delta(delta, "notify")
            else:
                self._log_warn("wheel-notify wheel-event delta-missing")
            return 0

        if self._orig_notify_handler:
            return self._orig_notify_handler(wparam, lparam)
        return 0

    def _extract_notify_event_code(self, event_full: int, event_low: int) -> int:
        if self.WM_MOUSEFIRST <= event_full <= self.WM_MOUSELAST:
            return event_full
        if self.WM_MOUSEFIRST <= event_low <= self.WM_MOUSELAST:
            return event_low
        return event_low

    def _extract_notify_wheel_delta(self, raw_wparam: int, raw_lparam: int) -> int:
        # On some shells/framework paths, WM_MOUSEWHEEL callback preserves
        # standard wParam (HIWORD=delta). On others (NOTIFYICON_VERSION_4),
        # wParam carries anchor coordinates and has no delta.
        cand_w = self._signed_word((raw_wparam >> 16) & 0xFFFF)
        if cand_w != 0 and abs(cand_w) % 120 == 0:
            return cand_w

        cand_l = self._signed_word((raw_lparam >> 16) & 0xFFFF)
        if cand_l != 0 and abs(cand_l) % 120 == 0:
            return cand_l
        return 0

    def _mark_tray_hover(self, raw_wparam: int) -> None:
        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            self._refresh_tray_hover(pt.x, pt.y)
            return
        x = self._signed_word(raw_wparam & 0xFFFF)
        y = self._signed_word((raw_wparam >> 16) & 0xFFFF)
        self._refresh_tray_hover(x, y)

    def _refresh_tray_hover(self, x: int, y: int) -> None:
        # Ignore invalid anchor injected by some shell callback paths.
        if x == 0 and y == 0:
            now = time.time()
            if now - self._tray_bad_anchor_logged_at >= 2.0:
                self._tray_bad_anchor_logged_at = now
                self._log_warn("wheel-hover ignored-invalid-anchor x=0 y=0")
            self._tray_hover_until = now + self.TRAY_HOVER_GRACE_SECONDS
            return
        self._tray_last_anchor = (x, y)
        self._tray_hover_until = time.time() + self.TRAY_HOVER_GRACE_SECONDS

    def _is_tray_hover_recent(self, x: int, y: int) -> bool:
        if time.time() > self._tray_hover_until:
            return False
        if not self._tray_last_anchor:
            return True
        ax, ay = self._tray_last_anchor
        return abs(x - ax) <= self.TRAY_HOVER_RADIUS_PX and abs(y - ay) <= self.TRAY_HOVER_RADIUS_PX

    def _on_tray_taskbarcreated(self, wparam, lparam):
        self._log_info("wheel-taskbarcreated received")
        self._tray_icon_rect_cache = None
        if self._orig_taskbarcreated_handler:
            self._orig_taskbarcreated_handler(wparam, lparam)
        self.root.after(1200, self._setup_tray_wheel_support)
        self.root.after(1500, self._setup_wheel_hook_support)
        return 0

    def _set_notify_icon_version(self) -> None:
        try:
            # Request modern tray behavior for richer mouse notifications.
            self.icon._message(pystray_win32.NIM_SETVERSION, 0, uVersion=self.NOTIFYICON_VERSION_4)
            self._log_info("wheel-notify-version set v4")
        except Exception as ex:
            self._log_warn("wheel-notify-version set-failed error=%s", ex)

    def _setup_wheel_hook_support(self) -> None:
        if os.name != "nt":
            return
        if self._wheel_hook_thread and self._wheel_hook_thread.is_alive():
            self._log_info("wheel-llhook already-running")
            return
        t = threading.Thread(target=self._wheel_hook_loop, daemon=True, name="wheel-hook-thread")
        self._wheel_hook_thread = t
        t.start()

    def _wheel_hook_loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        ulong_ptr_t = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = wintypes.LPARAM

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("pt", wintypes.POINT),
                ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr_t),
            ]

        hook_proc_type = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        @hook_proc_type
        def _hook_proc(n_code, w_param, l_param):
            try:
                if n_code == self.HC_ACTION and int(w_param) == self.WM_MOUSEWHEEL:
                    mouse = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    delta = self._signed_word((int(mouse.mouseData) >> 16) & 0xFFFF)
                    if delta:
                        self._wheel_hook_events += 1
                        event_id = self._wheel_hook_events
                        hit_by_rect = self._is_point_on_our_tray_icon(mouse.pt.x, mouse.pt.y)
                        hit_by_hover = False
                        if not hit_by_rect:
                            hit_by_hover = self._is_tray_hover_recent(mouse.pt.x, mouse.pt.y)
                        hit = hit_by_rect or hit_by_hover
                        if hit:
                            # Keep continuous wheel sessions stable even when tray rect lookup is intermittent.
                            self._refresh_tray_hover(mouse.pt.x, mouse.pt.y)
                            self._log_info(
                                "wheel-llhook event=%s delta=%s x=%s y=%s hit=%s source=%s",
                                event_id,
                                delta,
                                mouse.pt.x,
                                mouse.pt.y,
                                hit,
                                "rect" if hit_by_rect else "hover",
                            )
                            self._enqueue_wheel_delta(delta, "llhook")
                        else:
                            self._wheel_hook_miss_count += 1
                            now = time.time()
                            if now - self._wheel_hook_miss_logged_at >= self.WHEEL_MISS_LOG_INTERVAL_SECONDS:
                                self._wheel_hook_miss_logged_at = now
                                hover_left_ms = int(max(0.0, self._tray_hover_until - now) * 1000)
                                self._log_info(
                                    "wheel-llhook miss event=%s miss_count=%s x=%s y=%s hover_left_ms=%s anchor=%s",
                                    event_id,
                                    self._wheel_hook_miss_count,
                                    mouse.pt.x,
                                    mouse.pt.y,
                                    hover_left_ms,
                                    self._tray_last_anchor,
                                )
            except Exception as ex:
                self._log_error("wheel-llhook callback-error error=%s", ex)
            return user32.CallNextHookEx(self._wheel_hook_handle, n_code, w_param, l_param)

        self._wheel_hook_thread_id = kernel32.GetCurrentThreadId()
        self._wheel_hook_proc = _hook_proc
        self._wheel_hook_handle = user32.SetWindowsHookExW(
            self.WH_MOUSE_LL,
            self._wheel_hook_proc,
            None,
            0,
        )
        if not self._wheel_hook_handle:
            err = kernel32.GetLastError()
            self._log_error("wheel-llhook install-failed winerr=%s", err)
            self._wheel_hook_thread_id = None
            return

        self._log_info("wheel-llhook installed thread_id=%s", self._wheel_hook_thread_id)
        msg = wintypes.MSG()
        lpmsg = ctypes.byref(msg)
        try:
            while user32.GetMessageW(lpmsg, None, 0, 0) != 0:
                user32.TranslateMessage(lpmsg)
                user32.DispatchMessageW(lpmsg)
        except Exception as ex:
            self._log_error("wheel-llhook loop-error error=%s", ex)
        finally:
            try:
                if self._wheel_hook_handle:
                    user32.UnhookWindowsHookEx(self._wheel_hook_handle)
                    self._log_info("wheel-llhook removed")
            except Exception as ex:
                self._log_warn("wheel-llhook remove-failed error=%s", ex)
            self._wheel_hook_handle = None
            self._wheel_hook_proc = None
            self._wheel_hook_thread_id = None

    def _stop_wheel_hook_support(self) -> None:
        if os.name != "nt":
            return
        tid = self._wheel_hook_thread_id
        if tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(tid, self.WM_QUIT, 0, 0)
                self._log_info("wheel-llhook stop-posted thread_id=%s", tid)
            except Exception as ex:
                self._log_warn("wheel-llhook stop-post-failed error=%s", ex)

        t = self._wheel_hook_thread
        if t and t.is_alive():
            t.join(timeout=1.2)
        self._wheel_hook_thread = None

    def _register_raw_mouse_input(self, hwnd) -> None:
        rawinput_device_type = self._rawinput_device_type
        if rawinput_device_type is None:
            self._log_error("wheel-rawinput register-skipped missing-rawinput-type")
            return

        rid = rawinput_device_type(0x01, 0x02, self.RIDEV_INPUTSINK, hwnd)
        ok = ctypes.windll.user32.RegisterRawInputDevices(
            ctypes.byref(rid),
            1,
            ctypes.sizeof(rawinput_device_type),
        )
        self._raw_input_registered = bool(ok)
        if ok:
            self._log_info("wheel-rawinput registered hwnd=%s", hwnd)
        else:
            err = ctypes.windll.kernel32.GetLastError()
            self._log_error("wheel-rawinput register-failed hwnd=%s winerr=%s", hwnd, err)

    def _on_tray_raw_input(self, wparam, lparam):
        if not self._raw_input_registered:
            return 0
        pt = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return 0
        hit = self._is_point_on_our_tray_icon(pt.x, pt.y) or self._is_tray_hover_recent(pt.x, pt.y)
        if not hit:
            return 0

        delta = self._extract_wheel_delta(lparam)
        if delta == 0:
            return 0

        self._log_info("wheel-rawinput delta=%s hit=%s", delta, hit)
        if not hit:
            return 0
        try:
            self._refresh_tray_hover(pt.x, pt.y)
        except Exception:
            pass
        self._enqueue_wheel_delta(delta, "rawinput")
        return 0

    def _extract_wheel_delta(self, lparam) -> int:
        rawinput_header_type = self._rawinput_header_type
        rawinput_type = self._rawinput_type
        if rawinput_header_type is None or rawinput_type is None:
            return 0

        size = wintypes.UINT(0)
        user32 = ctypes.windll.user32
        hraw = ctypes.c_void_p(lparam)
        header_size = ctypes.sizeof(rawinput_header_type)
        if user32.GetRawInputData(hraw, self.RID_INPUT, None, ctypes.byref(size), header_size) == 0xFFFFFFFF:
            return 0
        if size.value == 0:
            return 0

        buffer = ctypes.create_string_buffer(size.value)
        if user32.GetRawInputData(
            hraw,
            self.RID_INPUT,
            buffer,
            ctypes.byref(size),
            header_size,
        ) == 0xFFFFFFFF:
            return 0

        raw = ctypes.cast(buffer, ctypes.POINTER(rawinput_type)).contents
        if raw.header.dwType != self.RIM_TYPEMOUSE:
            return 0

        if raw.mouse.usButtonFlags & self.RI_MOUSE_WHEEL:
            return ctypes.c_short(raw.mouse.usButtonData).value
        return 0

    @staticmethod
    def _signed_word(value: int) -> int:
        return ctypes.c_short(value & 0xFFFF).value

    def _get_tray_icon_rect(self) -> tuple[int, int, int, int] | None:
        hwnd = getattr(self.icon, "_hwnd", None)
        if not hwnd:
            now = time.time()
            if now - self._tray_rect_fail_logged_at >= 2.0:
                self._tray_rect_fail_logged_at = now
                self._log_warn("wheel-hit-test tray-hwnd-missing")
            return None

        shell32 = ctypes.windll.shell32
        if not hasattr(shell32, "Shell_NotifyIconGetRect"):
            now = time.time()
            if now - self._tray_rect_fail_logged_at >= 2.0:
                self._tray_rect_fail_logged_at = now
                self._log_warn("wheel-hit-test Shell_NotifyIconGetRect missing")
            return None

        rect = wintypes.RECT()
        notify_icon_identifier_type = self._notify_icon_identifier_type
        if notify_icon_identifier_type is None:
            self._log_warn("wheel-hit-test missing-notify-icon-type")
            return None

        uid_list: list[int] = []
        if self._tray_uid_preferred is not None:
            uid_list.append(int(self._tray_uid_preferred))
        uid_list.extend(self._tray_uid_candidates)

        # Keep order, remove duplicates.
        seen = set()
        uid_try: list[int] = []
        for uid in uid_list:
            u = int(ctypes.c_uint(uid).value)
            if u in seen:
                continue
            seen.add(u)
            uid_try.append(u)

        for uid in uid_try:
            nii = notify_icon_identifier_type()
            nii.cbSize = ctypes.sizeof(notify_icon_identifier_type)
            nii.hWnd = hwnd
            nii.uID = uid
            hr = shell32.Shell_NotifyIconGetRect(ctypes.byref(nii), ctypes.byref(rect))
            if hr == 0:
                if self._tray_uid_preferred != uid:
                    self._tray_uid_preferred = uid
                    self._log_info("wheel-hit-test get-rect uid-selected uid=%s", uid)
                return rect.left, rect.top, rect.right, rect.bottom

        now = time.time()
        if now - self._tray_rect_fail_logged_at >= 2.0:
            self._tray_rect_fail_logged_at = now
            self._log_warn(
                "wheel-hit-test get-rect-failed hwnd=%s tried_uid=%s",
                hwnd,
                uid_try,
            )
        return None

    def _get_tray_icon_rect_cached(self) -> tuple[int, int, int, int] | None:
        now = time.time()
        cache = self._tray_icon_rect_cache
        if cache and now - cache[4] <= 0.35:
            return cache[0], cache[1], cache[2], cache[3]

        rect = self._get_tray_icon_rect()
        if rect:
            self._tray_icon_rect_cache = (rect[0], rect[1], rect[2], rect[3], now)
        return rect

    def _is_point_on_our_tray_icon(self, x: int, y: int) -> bool:
        rect = self._get_tray_icon_rect_cached()
        if not rect:
            return False
        left, top, right, bottom = rect
        return left <= x <= right and top <= y <= bottom

    def _is_cursor_on_our_tray_icon(self) -> bool:
        pt = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return False
        return self._is_point_on_our_tray_icon(pt.x, pt.y)

    def _enqueue_wheel_delta(self, delta: int, source: str) -> None:
        if delta == 0:
            return
        should_schedule = False
        pending_count = 0
        with self._wheel_dispatch_lock:
            self._wheel_delta_pending += int(delta)
            self._wheel_enqueued_count += 1
            if not self._wheel_flush_scheduled:
                self._wheel_flush_scheduled = True
                should_schedule = True
            pending_count = self._wheel_enqueued_count - self._wheel_flushed_count

        now = time.time()
        if pending_count >= self.WHEEL_QUEUE_WARN_THRESHOLD and now - self._wheel_diag_logged_at >= self.WHEEL_DIAG_LOG_INTERVAL_SECONDS:
            self._wheel_diag_logged_at = now
            self._log_info(
                "wheel-queue backlog pending=%s enqueued=%s flushed=%s source=%s private_mb=%.1f",
                pending_count,
                self._wheel_enqueued_count,
                self._wheel_flushed_count,
                source,
                self._get_private_mem_mb(),
            )

        if should_schedule:
            self.root.after(0, self._flush_wheel_delta)

    def _flush_wheel_delta(self) -> None:
        with self._wheel_dispatch_lock:
            delta = self._wheel_delta_pending
            self._wheel_delta_pending = 0
            self._wheel_flushed_count = self._wheel_enqueued_count
            self._wheel_flush_scheduled = False
        if delta != 0:
            self._on_tray_wheel(delta)

    def _get_private_mem_mb(self) -> float:
        if os.name != "nt":
            return 0.0
        try:
            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb,
            )
            if ok:
                return counters.PrivateUsage / (1024 * 1024)
        except Exception:
            pass
        return 0.0

    def _on_tray_wheel(self, delta: int) -> None:
        old = self.settings.manual
        notches = int(delta / 120) if abs(delta) >= 120 else (1 if delta > 0 else -1)
        if notches == 0:
            return
        step = 2 * notches
        target = self._clamp(old + step, 0, 100)
        if target == old:
            self._log_info("wheel-apply skipped old=%s target=%s", old, target)
            return

        self.settings.manual = target
        self.slider_var.set(target)
        if self.slider_window and self.slider_window.winfo_exists() and self.slider_window.state() != "withdrawn":
            self._draw_slider_canvas()

        if self.settings.auto_enabled:
            self.settings.auto_enabled = False
            try:
                self.icon.update_menu()
            except Exception:
                pass

        ok = self._apply_percent(target, silent=True)
        self._save_settings()
        self._log_info("wheel-apply delta=%s old=%s target=%s ok=%s", delta, old, target, ok)

    def _load_settings(self) -> Settings:
        cfg = configparser.ConfigParser()
        if self.config_path.exists():
            cfg.read(self.config_path, encoding="utf-8")

        theme = cfg.get("UI", "Theme", fallback="dark").lower().strip()
        if theme not in {"light", "dark"}:
            theme = "dark"

        s = Settings(
            manual=self._clamp(cfg.getint("Brightness", "Manual", fallback=35), 0, 100),
            day=self._clamp(cfg.getint("Brightness", "Day", fallback=40), 0, 100),
            night=self._clamp(cfg.getint("Brightness", "Night", fallback=25), 0, 100),
            day_start=cfg.get("Schedule", "DayStart", fallback="08:00"),
            night_start=cfg.get("Schedule", "NightStart", fallback="20:00"),
            auto_enabled=cfg.getint("Schedule", "Enabled", fallback=1) == 1,
            theme=theme,
        )

        if not self._valid_hhmm(s.day_start):
            s.day_start = "08:00"
        if not self._valid_hhmm(s.night_start):
            s.night_start = "20:00"

        self._save_settings(s)
        return s

    def _save_settings(self, settings: Settings | None = None) -> None:
        s = settings or self.settings
        cfg = configparser.ConfigParser()
        cfg["Brightness"] = {
            "Manual": str(s.manual),
            "Day": str(s.day),
            "Night": str(s.night),
        }
        cfg["Schedule"] = {
            "Enabled": "1" if s.auto_enabled else "0",
            "DayStart": s.day_start,
            "NightStart": s.night_start,
        }
        cfg["UI"] = {
            "Theme": s.theme,
        }
        with self.config_path.open("w", encoding="utf-8") as f:
            cfg.write(f)

    def _palette(self) -> dict:
        if self.settings.theme == "light":
            return {
                "window": "#EFEFEF",
                "panel": "#F2F2F2",
                "border": "#D7D7D7",
                "icon": "#262626",
                "track_active": "#6D7030",
                "track_inactive": "#9B9B9B",
                "thumb_fill": "#6D7030",
                "thumb_ring": "#F2F2F2",
            }
        return {
            "window": "#171717",
            "panel": "#1F1F1F",
            "border": "#3A3A3A",
            "icon": "#E8E8E8",
            "track_active": "#8F9A47",
            "track_inactive": "#626262",
            "thumb_fill": "#8F9A47",
            "thumb_ring": "#1F1F1F",
        }

    def _apply_root_theme(self) -> None:
        p = self._palette()
        self.root.configure(bg=p["window"])

    def _apply_slider_theme(self) -> None:
        if not self.slider_window or not self.slider_window.winfo_exists():
            return

        p = self._palette()
        self.slider_window.configure(bg=p["panel"], highlightthickness=1, highlightbackground=p["border"])
        self._set_bg_recursive(self.slider_window, p["panel"])

        if self.sun_icon:
            self.sun_icon.configure(bg=p["panel"], fg=p["icon"])
        if self.slider_canvas:
            self.slider_canvas.configure(bg=p["panel"], highlightthickness=0, bd=0)
            self._draw_slider_canvas()
        self._apply_native_window_style()

    def _set_bg_recursive(self, widget, bg: str) -> None:
        for w in widget.winfo_children():
            try:
                w.configure(bg=bg)
            except Exception:
                pass
            self._set_bg_recursive(w, bg)

    def _apply_native_window_style(self) -> None:
        if os.name != "nt":
            return
        if not self.slider_window or not self.slider_window.winfo_exists():
            return

        hwnd = self.slider_window.winfo_id()
        try:
            dwmapi = ctypes.WinDLL("dwmapi")
        except Exception:
            return

        def set_attr(attr: int, value: int) -> None:
            v = ctypes.c_int(value)
            try:
                dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(attr),
                    ctypes.byref(v),
                    ctypes.sizeof(v),
                )
            except Exception:
                pass

        # Rounded corners on Windows 11.
        set_attr(33, 2)
        # Use system backdrop (Mica/Auto based on OS support).
        set_attr(38, 2)
        # Native dark/light window hint.
        set_attr(20, 1 if self.settings.theme == "dark" else 0)

    def _set_theme(self, theme: str) -> None:
        if theme not in {"light", "dark"}:
            return
        self.settings.theme = theme
        self._save_settings()
        self._apply_root_theme()
        self._apply_slider_theme()
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _resolve_set_tool(self) -> Path | None:
        candidates = [
            self.app_dir / "set_sdrwhite.exe",
            self.app_dir / "tools" / "set_sdrwhite.exe",
            self.data_dir / "set_sdrwhite.exe",
            self.data_dir / "tools" / "set_sdrwhite.exe",
            self.resource_dir / "set_sdrwhite.exe",
            self.resource_dir / "tools" / "set_sdrwhite.exe",
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    def _apply_percent(self, percent: int, silent: bool = False) -> bool:
        tool = self._resolve_set_tool()
        if tool is None:
            if not silent and self._download_set_tool():
                tool = self._resolve_set_tool()
            if tool is None:
                self._log_error("apply failed missing set_sdrwhite.exe")
                if not silent:
                    messagebox.showerror(
                        "Missing component",
                        "set_sdrwhite.exe is missing and auto download was not completed.",
                    )
                return False

        p = self._clamp(percent, 0, 100)
        nits = 80 + p * 4

        try:
            subprocess.run([str(tool), "0", str(nits)], check=True, creationflags=0x08000000)
            self.last_applied_percent = p
            return True
        except Exception as ex:
            self._log_error("apply failed percent=%s nits=%s error=%s", p, nits, ex)
            if not silent:
                messagebox.showerror("Apply failed", f"Failed to set SDR brightness:\n{ex}")
            return False

    def _download_set_tool(self) -> bool:
        consent = messagebox.askyesno(
            "下载组件",
            "缺少 set_sdrwhite.exe。\n是否从 GitHub 自动下载并安装到本地？",
        )
        if not consent:
            return False

        tmp_zip = Path(tempfile.gettempdir()) / "hdr_sdr_release.zip"
        tmp_dir = Path(tempfile.gettempdir()) / "hdr_sdr_release_extract"

        try:
            urllib.request.urlretrieve(self.RELEASE_ZIP_URL, tmp_zip)
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extract("set_sdrwhite.exe", path=tmp_dir)

            src = tmp_dir / "set_sdrwhite.exe"
            dst = self.app_dir / "set_sdrwhite.exe"
            try:
                shutil.copy2(src, dst)
            except PermissionError:
                dst = self.data_dir / "set_sdrwhite.exe"
                shutil.copy2(src, dst)
            messagebox.showinfo("安装完成", f"已安装: {dst}")
            return True
        except Exception as ex:
            messagebox.showerror("下载失败", f"自动下载 set_sdrwhite.exe 失败:\n{ex}")
            return False
        finally:
            try:
                if tmp_zip.exists():
                    tmp_zip.unlink()
            except Exception:
                pass
            try:
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    def _initial_apply(self) -> None:
        if self.settings.auto_enabled:
            self._apply_schedule_now(silent=True)
        else:
            self._apply_percent(self.settings.manual, silent=True)

    def _schedule_tick(self) -> None:
        self._apply_schedule_now(silent=True)
        self.root.after(60_000, self._schedule_tick)

    def _apply_schedule_now(self, silent: bool = True) -> None:
        if not self.settings.auto_enabled:
            return

        now = self._current_minutes()
        day_m = self._hhmm_to_minutes(self.settings.day_start)
        night_m = self._hhmm_to_minutes(self.settings.night_start)

        use_day = self._in_clock_range(now, day_m, night_m)
        target = self.settings.day if use_day else self.settings.night

        if self.last_applied_percent != target:
            self._apply_percent(target, silent=silent)

    def _show_slider(self) -> None:
        self._log_info("slider-open requested")
        if self.slider_window is None or not self.slider_window.winfo_exists():
            p = self._palette()
            self.slider_window = tk.Toplevel(self.root)
            self.slider_window.attributes("-topmost", True)
            self.slider_window.attributes("-alpha", 0.98)
            self.slider_window.resizable(False, False)
            self.slider_window.overrideredirect(True)

            frame = tk.Frame(self.slider_window, padx=7, pady=6, bg=p["panel"])
            frame.pack(fill="both", expand=True)

            row = tk.Frame(frame, bg=p["panel"])
            row.pack(fill="x")
            self.sun_icon = tk.Label(row, text="☀", font=("Segoe UI Symbol", 11), bg=p["panel"], fg=p["icon"])
            self.sun_icon.pack(side="left", padx=(0, 6))

            self.slider_canvas = tk.Canvas(
                row,
                width=self.slider_canvas_width,
                height=self.slider_canvas_height,
                bg=p["panel"],
                highlightthickness=0,
                bd=0,
            )
            self.slider_canvas.pack(side="left")
            self.slider_canvas.bind("<Button-1>", self._on_slider_press)
            self.slider_canvas.bind("<B1-Motion>", self._on_slider_drag)
            self.slider_canvas.bind("<ButtonRelease-1>", self._on_slider_release)

            self.slider_window.bind("<Escape>", lambda e: self._hide_slider())
            self.slider_window.bind("<FocusOut>", self._on_slider_focus_out)
            self.slider_window.protocol("WM_DELETE_WINDOW", self._hide_slider)

        self.slider_var.set(self._clamp(self.settings.manual, 0, 100))
        self._apply_slider_theme()
        self.slider_window.update_idletasks()
        self._position_window(
            self.slider_window,
            width=self.slider_window.winfo_reqwidth(),
            height=self.slider_window.winfo_reqheight(),
        )
        self.slider_window.deiconify()
        self.slider_window.lift()
        self.slider_window.focus_force()
        self.slider_shown_at = time.monotonic()
        self._apply_native_window_style()

    def _hide_slider(self) -> None:
        if self.slider_window and self.slider_window.winfo_exists():
            self.slider_window.withdraw()

    def _on_slider_focus_out(self, event=None) -> None:
        self.root.after(40, self._hide_if_focus_outside)

    def _hide_if_focus_outside(self) -> None:
        if not self.slider_window or not self.slider_window.winfo_exists():
            return
        if self.slider_window.state() == "withdrawn":
            return
        if time.monotonic() - self.slider_shown_at < 0.35:
            return
        try:
            px, py = self.slider_window.winfo_pointerxy()
        except Exception:
            self._hide_slider()
            return
        if not self._is_point_in_window(self.slider_window, px, py):
            self._hide_slider()

    def _is_point_in_window(self, win: tk.Toplevel, x: int, y: int) -> bool:
        left = win.winfo_rootx()
        top = win.winfo_rooty()
        right = left + win.winfo_width()
        bottom = top + win.winfo_height()
        return left <= x <= right and top <= y <= bottom

    def _position_window(self, win: tk.Toplevel, width: int, height: int) -> None:
        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        outer_margin = 6
        taskbar_gap = 2

        # Prefer anchoring above our tray icon so popup visually sticks to taskbar.
        rect = self._get_tray_icon_rect_cached()
        if rect:
            left, top, right, bottom = rect
            x = right - width
            if top >= screen_h // 2:
                y = top - height - taskbar_gap
            else:
                y = bottom + taskbar_gap
            x = max(outer_margin, min(x, screen_w - width - outer_margin))
            y = max(outer_margin, min(y, screen_h - height - outer_margin))
            win.geometry(f"{width}x{height}+{x}+{y}")
            return

        x = screen_w - width - 12
        y = screen_h - height - 48
        if os.name == "nt":
            try:
                work = wintypes.RECT()
                ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)
                if ok:
                    # Bottom taskbar (typical): place just above it.
                    if work.bottom < screen_h:
                        y = work.bottom - height - taskbar_gap
                    # Top taskbar.
                    elif work.top > 0:
                        y = work.top + taskbar_gap

                    # Right or left taskbar docking.
                    if work.right < screen_w:
                        x = work.right - width - taskbar_gap
                    elif work.left > 0:
                        x = work.left + taskbar_gap
            except Exception:
                pass

        x = max(outer_margin, min(x, screen_w - width - outer_margin))
        y = max(outer_margin, min(y, screen_h - height - outer_margin))
        win.geometry(f"{width}x{height}+{x}+{y}")

    def _draw_slider_canvas(self) -> None:
        if not self.slider_canvas:
            return

        p = self._palette()
        c = self.slider_canvas
        c.delete("all")

        v = self._clamp(self.slider_var.get(), 0, 100)
        x = self._value_to_x(v)
        y = self.slider_track_y
        c.create_line(
            self.slider_track_left,
            y,
            self.slider_track_right,
            y,
            width=3,
            fill=p["track_inactive"],
            capstyle=tk.ROUND,
        )
        c.create_line(
            self.slider_track_left,
            y,
            x,
            y,
            width=3,
            fill=p["track_active"],
            capstyle=tk.ROUND,
        )

        r_outer = 7
        r_inner = 4
        c.create_oval(x - r_outer, y - r_outer, x + r_outer, y + r_outer, fill=p["thumb_ring"], outline=p["thumb_ring"])
        c.create_oval(x - r_inner, y - r_inner, x + r_inner, y + r_inner, fill=p["thumb_fill"], outline=p["thumb_fill"])

    def _value_to_x(self, value: int) -> int:
        ratio = self._clamp(value, 0, 100) / 100.0
        return int(self.slider_track_left + (self.slider_track_right - self.slider_track_left) * ratio)

    def _x_to_value(self, x: int) -> int:
        x = max(self.slider_track_left, min(self.slider_track_right, x))
        ratio = (x - self.slider_track_left) / (self.slider_track_right - self.slider_track_left)
        return self._clamp(int(round(ratio * 100)), 0, 100)

    def _on_slider_press(self, event) -> None:
        self.slider_dragging = True
        self._on_slider_drag(event)

    def _on_slider_drag(self, event) -> None:
        v = self._x_to_value(event.x)
        self.slider_var.set(v)
        self.settings.manual = v
        self._draw_slider_canvas()
        self._apply_percent(v)

    def _on_slider_release(self, event=None) -> None:
        if self.slider_dragging:
            self.slider_dragging = False
        if self.settings.auto_enabled:
            self.settings.auto_enabled = False
            try:
                self.icon.update_menu()
            except Exception:
                pass
        self._save_settings()
        self._hide_slider()

    def _on_open_slider(self, icon=None, item=None) -> None:
        self._log_info("menu-open-slider clicked")
        self.root.after(0, self._show_slider)

    def _on_apply_now(self, icon=None, item=None) -> None:
        def _apply():
            self.settings.manual = self._clamp(self.slider_var.get(), 0, 100)
            ok = self._apply_percent(self.settings.manual)
            if ok:
                self._save_settings()

        self.root.after(0, _apply)

    def _on_toggle_auto(self, icon=None, item=None) -> None:
        def _toggle():
            self.settings.auto_enabled = not self.settings.auto_enabled
            self._save_settings()
            if self.settings.auto_enabled:
                self._apply_schedule_now(silent=False)
            try:
                self.icon.update_menu()
            except Exception:
                pass

        self.root.after(0, _toggle)

    def _on_toggle_startup(self, icon=None, item=None) -> None:
        def _toggle():
            try:
                enable = not self._is_startup_enabled()
                self._set_startup_enabled(enable)
                state = "已开启" if enable else "已关闭"
                messagebox.showinfo("开机自启动", f"开机自启动{state}")
            except Exception as ex:
                messagebox.showerror("开机自启动", f"设置失败:\n{ex}")
            finally:
                try:
                    self.icon.update_menu()
                except Exception:
                    pass

        self.root.after(0, _toggle)

    def _on_set_light_theme(self, icon=None, item=None) -> None:
        self.root.after(0, lambda: self._set_theme("light"))

    def _on_set_dark_theme(self, icon=None, item=None) -> None:
        self.root.after(0, lambda: self._set_theme("dark"))

    def _on_edit_schedule(self, icon=None, item=None) -> None:
        def _edit():
            current = f"{self.settings.day_start},{self.settings.night_start},{self.settings.day},{self.settings.night}"
            text = simpledialog.askstring(
                "编辑时间计划",
                "格式: dayStart,nightStart,dayValue,nightValue\n示例: 08:00,20:00,40,25",
                initialvalue=current,
                parent=self.root,
            )
            if not text:
                return

            parts = [p.strip() for p in text.split(",")]
            if len(parts) != 4:
                messagebox.showerror("格式错误", "请输入 4 个逗号分隔项")
                return

            day_start, night_start, day_v, night_v = parts
            if not self._valid_hhmm(day_start) or not self._valid_hhmm(night_start):
                messagebox.showerror("时间格式错误", "时间必须为 HH:MM")
                return

            try:
                day_i = self._clamp(int(day_v), 0, 100)
                night_i = self._clamp(int(night_v), 0, 100)
            except ValueError:
                messagebox.showerror("数值错误", "亮度必须为 0-100 的整数")
                return

            self.settings.day_start = day_start
            self.settings.night_start = night_start
            self.settings.day = day_i
            self.settings.night = night_i
            self._save_settings()
            self._apply_schedule_now(silent=False)

        self.root.after(0, _edit)

    def _on_exit(self, icon=None, item=None) -> None:
        self.root.after(0, self._quit)

    def _quit(self) -> None:
        self._log_info("app-quit requested")
        self._stop_wheel_hook_support()
        if self.hotkey_registered and os.name == "nt":
            try:
                ctypes.windll.user32.UnregisterHotKey(None, self.HOTKEY_ID)
            except Exception:
                pass
        try:
            self.icon.stop()
        except Exception:
            pass
        self.root.quit()

    @staticmethod
    def _clamp(v: int, low: int, high: int) -> int:
        return max(low, min(high, v))

    @staticmethod
    def _valid_hhmm(text: str) -> bool:
        if len(text) != 5 or text[2] != ":":
            return False
        h, m = text.split(":", 1)
        if not (h.isdigit() and m.isdigit()):
            return False
        hh = int(h)
        mm = int(m)
        return 0 <= hh <= 23 and 0 <= mm <= 59

    @staticmethod
    def _hhmm_to_minutes(text: str) -> int:
        h, m = text.split(":", 1)
        return int(h) * 60 + int(m)

    @staticmethod
    def _in_clock_range(cur: int, start: int, end: int) -> bool:
        if start == end:
            return True
        if start < end:
            return start <= cur < end
        return cur >= start or cur < end

    @staticmethod
    def _current_minutes() -> int:
        import datetime

        now = datetime.datetime.now()
        return now.hour * 60 + now.minute


if __name__ == "__main__":
    HdrSdrTrayApp().run()
