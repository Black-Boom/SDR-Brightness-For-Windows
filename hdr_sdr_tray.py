import configparser
import ctypes
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
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog

from PIL import Image, ImageDraw
import pystray


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
    MOD_ALT = 0x0001
    MOD_WIN = 0x0008
    VK_S = 0x53
    HOTKEY_ID = 1

    def __init__(self) -> None:
        self.app_dir = self._get_app_dir()
        self.resource_dir = self._get_resource_dir()
        self.data_dir = self._get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "config.ini"

        self.settings = self._load_settings()
        self.last_applied_percent: int | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("HDR SDR Brightness")

        self.slider_window: tk.Toplevel | None = None
        self.slider_var = tk.IntVar(value=self.settings.manual)
        self.hotkey_registered = False
        self.slider_canvas: tk.Canvas | None = None
        self.slider_track_left = 18
        self.slider_track_right = 318
        self.slider_track_y = 18
        self.slider_dragging = False
        self.sun_icon: tk.Label | None = None
        self.slider_shown_at = 0.0

        self.icon = pystray.Icon(
            "hdr_sdr_brightness",
            self._build_icon_image(),
            "HDR SDR Brightness",
            menu=self._build_menu(),
        )

        self._apply_root_theme()
        self.root.after(500, self._initial_apply)
        self.root.after(60_000, self._schedule_tick)
        self._setup_hotkey()

    def run(self) -> None:
        tray_thread = threading.Thread(target=self.icon.run, daemon=True)
        tray_thread.start()
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
        img = Image.new("RGBA", (64, 64), (20, 20, 20, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((6, 10, 58, 54), radius=12, fill=(40, 120, 220, 255))
        d.rectangle((16, 26, 48, 32), fill=(255, 255, 255, 255))
        d.rectangle((20, 20, 44, 24), fill=(255, 255, 255, 255))
        d.rectangle((20, 34, 44, 38), fill=(255, 255, 255, 255))
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
                if not silent:
                    messagebox.showerror(
                        "缺少组件",
                        "未找到 set_sdrwhite.exe，且自动下载未完成。",
                    )
                return False

        p = self._clamp(percent, 0, 100)
        nits = 80 + p * 4

        try:
            subprocess.run([str(tool), "0", str(nits)], check=True, creationflags=0x08000000)
            self.last_applied_percent = p
            return True
        except Exception as ex:
            if not silent:
                messagebox.showerror("应用失败", f"设置 SDR 亮度失败:\n{ex}")
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
        if self.slider_window is None or not self.slider_window.winfo_exists():
            p = self._palette()
            self.slider_window = tk.Toplevel(self.root)
            self.slider_window.attributes("-topmost", True)
            self.slider_window.attributes("-alpha", 0.98)
            self.slider_window.resizable(False, False)
            self.slider_window.overrideredirect(True)

            frame = tk.Frame(self.slider_window, padx=12, pady=10, bg=p["panel"])
            frame.pack(fill="both", expand=True)

            row = tk.Frame(frame, bg=p["panel"])
            row.pack(fill="x")
            self.sun_icon = tk.Label(row, text="☀", font=("Segoe UI Symbol", 14), bg=p["panel"], fg=p["icon"])
            self.sun_icon.pack(side="left", padx=(0, 10))

            self.slider_canvas = tk.Canvas(row, width=336, height=36, bg=p["panel"], highlightthickness=0, bd=0)
            self.slider_canvas.pack(side="left")
            self.slider_canvas.bind("<Button-1>", self._on_slider_press)
            self.slider_canvas.bind("<B1-Motion>", self._on_slider_drag)
            self.slider_canvas.bind("<ButtonRelease-1>", self._on_slider_release)

            self.slider_window.bind("<Escape>", lambda e: self._hide_slider())
            self.slider_window.bind("<FocusOut>", self._on_slider_focus_out)
            self.slider_window.protocol("WM_DELETE_WINDOW", self._hide_slider)

        self.slider_var.set(self._clamp(self.settings.manual, 0, 100))
        self._apply_slider_theme()
        self._position_window(self.slider_window, width=420, height=64)
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
        x = max(0, win.winfo_screenwidth() - width - 30)
        y = max(0, win.winfo_screenheight() - height - 90)
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
        c.create_line(self.slider_track_left, y, self.slider_track_right, y, width=4, fill=p["track_inactive"], capstyle=tk.ROUND)
        c.create_line(self.slider_track_left, y, x, y, width=4, fill=p["track_active"], capstyle=tk.ROUND)

        r_outer = 10
        r_inner = 6
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
