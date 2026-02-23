# HDR SDR Brightness (Standalone EXE)

这是一个用于调节 Windows HDR 中“调节 SDR 内容亮度”的托盘工具。

## 功能
- 托盘滑块：点击托盘图标可弹出亮度滑块（靠近右下角）。
- 自动时间调节：支持白天/夜间时间段自动切换亮度。
- 全局快捷键：`Win+Alt+S` 可随时呼出滑块。
- 手动优先：手动拖动滑块并松手后，会自动关闭“自动时间调节”，避免下一轮定时覆盖你的手动亮度。
- 开机自启动：右键菜单可开启/关闭。
- 主题切换：右键菜单可选浅色或深色（Windows 11 风格）。
- 窗口外点击自动关闭滑块窗口。
- 使用 Windows 原生窗口属性 API（圆角/系统背景/深浅色提示）提升系统风格一致性。
- 鼠标停留托盘图标时可滚轮调节亮度（优先托盘消息与 Raw Input，必要时启用仅滚轮的低级监听兜底，且不拦截系统事件）。

## 关于“音量下面的系统快速设置面板”
Windows 11 快速设置面板当前没有稳定公开的第三方扩展接口，无法可靠把自定义滑块直接嵌入该面板。此项目采用托盘弹窗作为替代入口。

## 无需安装 AutoHotkey
本项目已改为 Python 程序，可打包成单文件 `exe`。最终用户只需运行 exe，不需要安装 AutoHotkey。

## 组件集成方式
程序依赖 `set_sdrwhite.exe` 来真正写入 SDR 白电平，支持两种方式：
- 自动：首次应用亮度时若缺失，会弹窗提示自动从 GitHub 下载并安装到 `exe` 同级目录。
- 手动：把 `set_sdrwhite.exe` 放到程序目录或 `tools` 子目录。

## 运行方式（开发态）
1. 安装 Python 3.10+。
2. 安装依赖：
   `pip install -r requirements.txt`
3. 启动：
   `python hdr_sdr_tray.py`
4. 如需输出日志（调试模式）：
   `python hdr_sdr_tray.py -debug`

## 打包单文件 EXE
执行：
`powershell -ExecutionPolicy Bypass -File .\build.ps1`

输出：
`dist\HDR-SDR-Brightness.exe`

如果 `tools\set_sdrwhite.exe` 存在，打包时会自动内置进 exe。

## 配置文件位置
程序会将配置写入：
`%LOCALAPPDATA%\HDR-SDR-Brightness\config.ini`

配置项：
- `Manual`: 手动亮度 0-100
- `Day` / `Night`: 自动亮度 0-100
- `DayStart` / `NightStart`: 时间（HH:MM）
- `Enabled`: 自动调节开关（1/0）
- `Theme`: 主题（`light` / `dark`）

## 日志文件
- 默认不输出日志文件。
- 仅在使用 `-debug` 参数启动时输出日志：
  - 开发态：`python hdr_sdr_tray.py -debug`
  - 打包后：`HDR-SDR-Brightness.exe -debug`
- 日志默认写到 `exe` 同级目录：`HDR-SDR-Brightness.log`
- 如果 `exe` 同级目录不可写，会回退到：`%LOCALAPPDATA%\HDR-SDR-Brightness\HDR-SDR-Brightness.log`
- 排查“托盘滚轮无效”时，请提供包含以下关键字的日志行：
  - `wheel-setup`
  - `wheel-notify raw`
  - `wheel-rawinput`
  - `wheel-llhook`
  - `wheel-llhook miss`
  - `wheel-hit-test`
  - `wheel-apply`
