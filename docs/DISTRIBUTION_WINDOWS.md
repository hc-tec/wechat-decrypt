# Windows 分发建议（面向普通用户）

目标：让用户「双击即可运行」本地服务，上层应用通过 HTTP API 读取消息/联系人。

## 推荐分发形态

- **onedir 文件夹包**（推荐）：
  - `WeChatDataService/WeChatDataService.exe`（服务进程）
  - `WeChatDataServiceGUI/WeChatDataServiceGUI.exe`（GUI 启动器，普通用户推荐用它）
- 进一步封装：用 Inno Setup / NSIS 做 `Setup.exe`，安装到用户目录并创建桌面快捷方式

说明：
- 可执行文件所在目录可写时：`config.json` / `all_keys.json` 默认放在 exe 同目录（便于“解压即用”）。
- 安装到 `Program Files` 等只读目录时：会自动改为用户目录：`%APPDATA%\\WeChatDataService\\`。

## 用户使用流程（建议写到你的产品说明里）

1. 打开微信并保持登录
2. 首次运行建议 **右键 → 以管理员身份运行**（需要读取微信进程内存提取数据库密钥）
3. 程序会自动生成 `config.json` 并提取 `all_keys.json`
4. 启动后访问本地地址（默认）：`http://127.0.0.1:5678`
5. 上层应用调用 API：见 `docs/API.md`

可选配置（`config.json`）：
- `listen_host`：默认 `127.0.0.1`（只允许本机访问，避免暴露到局域网）
- `listen_port`：默认 `5678`
- `open_browser`：是否自动打开浏览器

## 自启动（可选）

对普通用户友好做法：
- 安装器里创建「开机启动」选项：用 Windows 任务计划程序（Task Scheduler）在登录后启动 `WeChatDataService.exe`
- 或者把快捷方式放到启动文件夹：`shell:startup`
