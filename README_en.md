# HDR SDR Brightness (Standalone EXE)

[简体中文说明](README.md)

A tray utility for adjusting Windows HDR "SDR content brightness".

## Features
- Tray slider: click the tray icon to open a compact brightness slider near the taskbar.
- Time-based auto adjustment: switch brightness between day/night schedules.
- Global hotkey: press `Win+Alt+S` to open the slider anytime.
- Manual override: after dragging and releasing the slider, auto schedule is disabled to avoid overwriting your manual value.
- Startup with Windows: toggle in tray menu.
- Theme switch: light/dark styles in tray menu (Windows 11 style).
- Auto-hide slider when clicking outside the popup window.
- Uses native Windows window attributes API (rounded corners/system backdrop/dark-mode hint) for better system visual consistency.
- Mouse wheel adjustment on tray icon (prefers tray notifications + Raw Input, falls back to wheel-only low-level hook when needed, without blocking system events).

## About the Quick Settings Panel
Windows 11 currently has no stable public extension API for embedding third-party custom controls directly into the Quick Settings panel. This project uses a tray popup as the practical alternative.

## No AutoHotkey Required
This project is now a Python app that can be packaged into a single `exe`. End users only need the executable.

## set_sdrwhite.exe Integration
The app relies on `set_sdrwhite.exe` to actually write the SDR white level. Two ways are supported:
- Automatic: when missing on first apply, the app prompts to download and install it from GitHub to the executable directory.
- Manual: place `set_sdrwhite.exe` in the application directory.

## Run in Development Mode
1. Install Python 3.10+.
2. Install dependencies:
   `pip install -r requirements.txt`
3. Start:
   `python hdr_sdr_tray.py`
4. Enable debug logs:
   `python hdr_sdr_tray.py -debug`

## Build Single-file EXE
Run:
`powershell -ExecutionPolicy Bypass -File .\build.ps1`

Output:
`dist\HDR-SDR-Brightness.exe`

`set_sdrwhite.exe` can be auto-downloaded at runtime. If you want to distribute manually, place it next to the main executable (for example, `dist\set_sdrwhite.exe`).

## Config File Location
The app writes config to:
`%LOCALAPPDATA%\HDR-SDR-Brightness\config.ini`

Config keys:
- `Manual`: manual brightness 0-100
- `Day` / `Night`: auto brightness 0-100
- `DayStart` / `NightStart`: time (`HH:MM`)
- `Enabled`: auto schedule switch (`1/0`)
- `Theme`: theme (`light` / `dark`)

## Log Files
- No log file by default.
- Logs are written only when started with `-debug`:
  - Dev mode: `python hdr_sdr_tray.py -debug`
  - Packaged mode: `HDR-SDR-Brightness.exe -debug`
- Default log path: executable directory `HDR-SDR-Brightness.log`
- Fallback if not writable: `%LOCALAPPDATA%\HDR-SDR-Brightness\HDR-SDR-Brightness.log`
- For tray-wheel troubleshooting, provide log lines containing:
  - `wheel-setup`
  - `wheel-notify raw`
  - `wheel-rawinput`
  - `wheel-llhook`
  - `wheel-llhook miss`
  - `wheel-hit-test`
  - `wheel-apply`
