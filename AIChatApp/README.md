# AIChatApp（macOS SwiftUI 客户端）

调用本地 Python 后端（`../main.py`，默认 `http://127.0.0.1:8000/api`）的原生 macOS 客户端。

## 三个页面

| 页面 | 关键行为 |
|---|---|
| 登录页 | Google OAuth + PKCE 或本地管理员应急登录；两条路径最终都由后端签发 JWT，token 存进 Keychain |
| 对话页 | `POST /api/chat_async` 拿 `instance_id`，**每 2 秒**轮询 `GET /api/ai_task_status` 直到 `completed` / `failed`；左侧会话列表来自 `/api/chat_conversations`，点开会话加载 `/api/chat_history` |
| 设置页 | 配置后端地址、Azure / Meraki 凭据（各带「测试连接」）、`DEEPSEEK_API_KEY` / `KIMI_API_KEY` / 默认 Provider（带「测试余额」）；可启动/停止/重启本地后端、写入 `.env`、查看后端日志，项目目录与 Python 解释器收在「高级选项」里 |

### 界面字体大小（⌘+ / ⌘−）

跟 Safari / Xcode 一样用系统原生快捷键调整，菜单栏「显示」里也有对应项：

| 快捷键 | 作用 |
|---|---|
| ⌘+ | 放大字体 |
| ⌘− | 缩小字体 |
| ⌘0 | 恢复默认（115%） |

档位是 80 / 90 / 100 / **115%（默认）** / 130 / 150 / 175 / 200%，到两端就不再变化；改完立即生效并在窗口底部
短暂显示当前百分比，选择会存进 UserDefaults（`ui.fontScale`），下次启动沿用。会话列表宽度、气泡、输入框高度、
检查清单面板、设置项标签列宽，以及窗口最小尺寸都会一起缩放，不需要重启或重新登录。

> 实现说明：macOS 上 SwiftUI 的 `dynamicTypeSize` 对字体不起作用（实测各档位渲染尺寸完全一致），
> 所以字体缩放是自己实现的 —— `Sources/Views/AppTypography.swift` 提供 `.appFont(…)` / `.appFontScale(…)`，
> 基准字号直接问 AppKit 的 `NSFont.preferredFont(forTextStyle:)` 拿，再乘缩放比例；快捷键走 SwiftUI 的
> `CommandMenu("显示")`。
> 新增界面代码时用 `.appFont(.body)` 代替 `.font(.body)` 即可自动跟随这个缩放。

## 运行

```bash
# 1) 准备后端依赖（只做一次）
cd ..                       # 项目根目录（含 main.py）
python3 -m venv .venv
.venv/bin/pip install -r requirements-fastapi.txt

# 2) 打开客户端
open AIChatApp/AIChatApp.xcodeproj
# 或者命令行构建/运行
xcodebuild -project AIChatApp/AIChatApp.xcodeproj -scheme AIChatApp -configuration Debug \
           -derivedDataPath .xcbuild build
open .xcbuild/Build/Products/Debug/AIChatApp.app
```

首次启动会：自动探测项目目录与 `.venv/bin/python` → 拉起 `main.py` → 用 `/api/health` 做健康检查。

登录默认 `admin` / `password123`（后端可用 `ADMIN_USERNAME`、`ADMIN_PASSWORD` 覆盖）。

### 配置 Google 登录

1. 在 Google Cloud Console 创建 OAuth 2.0 Client（桌面应用）。
2. 在 App「设置 → 登录」填写 Client ID、允许邮箱/域名后点「保存并重启后端」；开发模式也可直接在
   项目根目录 `.env` 设置 `GOOGLE_CLIENT_ID`。
3. 本地默认回调是 `http://127.0.0.1:8000/api/auth/google/callback`；若后端地址或端口不同，设置
   `GOOGLE_REDIRECT_URI`，并确保 Google Cloud 中允许相同回调地址。
4. 建议设置 `GOOGLE_ALLOWED_EMAILS`（逗号分隔邮箱）或 `GOOGLE_ALLOWED_DOMAINS`（逗号分隔域名）。
   两项均为空时，任何拥有已验证邮箱的 Google 账号都能登录。

登录过程使用系统浏览器和 Authorization Code + PKCE。Google access token 只在后端短暂用于读取账号身份，
不会保存到客户端；客户端最终保存的仍是本后端签发的 JWT。本地管理员登录继续作为应急入口。

### 配置 GitHub 登录

1. GitHub → Settings → Developer settings → OAuth Apps → New OAuth App。
2. Homepage URL 填 `http://127.0.0.1:8000`，Authorization callback URL 填
   `http://127.0.0.1:8000/api/auth/github/callback`（Device Flow 实际不依赖该回调）。
3. 创建后在 OAuth App 设置中启用 `Enable Device Flow`。
4. 在 App「设置 → 登录」填写 GitHub Client ID，并建议填写允许的 GitHub 用户名或邮箱，保存并重启后端。

登录时 App 会打开 `github.com/login/device`，并把短验证码复制到剪贴板。后端只申请
`read:user user:email`，确认身份后换发自己的 JWT；不需要 Client Secret。

## API Key 怎么传给 Python 后端

设置页里填的 Key 存在 **Keychain**，启动子进程时通过**环境变量**注入：

```
DEEPSEEK_API_KEY / KIMI_API_KEY / DEFAULT_AI_PROVIDER / AI_MOCK_MODE / PORT
```

环境变量优先级高于项目里的 `.env`。如果勾选「启动时把设置写入项目根目录的 .env」，还会额外把这几项合并写入 `.env`（已有其它配置行会被保留）。

没有配置任何 Key 时，后端 `AI_MOCK_MODE=auto` 会自动返回 mock 回复，前端流程照常可跑通；只配了一个 Provider 的 Key 时，只有那个 Provider 走真实调用。

## 目录结构

```
AIChatApp/
├── AIChatApp.xcodeproj
├── Support/Info.plist              # 含 NSAllowsLocalNetworking（允许访问 127.0.0.1）
└── Sources/
    ├── AIChatAppApp.swift
    ├── Models/APIModels.swift      # 与后端 JSON 字段一一对应
    ├── Services/
    │   ├── APIClient.swift         # login / chat_async / ai_task_status / 会话管理
    │   ├── AppSettings.swift       # UserDefaults + Keychain + .env 写入 + 路径探测
    │   ├── BackendController.swift # 启动/停止 main.py、健康检查、依赖检查、日志
    │   ├── KeychainStore.swift
    │   └── SessionStore.swift      # token 与全局状态
    ├── ViewModels/ChatViewModel.swift
    └── Views/                      # Root / Login / Chat / Settings / 组件
```

## 分发与安装（DMG）

```bash
./scripts/build_dmg.sh        # 可重复执行，产物在项目根目录
```

脚本做的事：`xcodebuild archive`（Release）→ 从 archive 取 `.app` → 按需签名 → `hdiutil create -format UDZO`
打成 `AIChatApp-<版本号>.dmg`，最后 `hdiutil verify` 校验。版本号自动读构建出来的 `Info.plist`
（`CFBundleShortVersionString`，来自工程里的 `MARKETING_VERSION`，当前 `0.1.9`）。
DMG 内容是 `AIChatApp.app` + 指向 `/Applications` 的快捷方式 + `README.txt`，拖拽即可安装。

安装步骤：

1. 双击 `AIChatApp-xxx.dmg`，把 `AIChatApp.app` 拖到 `Applications`。
2. **首次打开**：在「应用程序」里右键点 `AIChatApp` →「打开」→ 再点「打开」，之后双击就能正常启动。
3. 后端已经内嵌在 App 里（`Contents/MacOS/backend_server`），首次启动会自动跑起来，**不需要装 Python**，
   也不需要配项目目录；日志在 `~/Library/Logs/AIChatApp/backend.log`。

打包时会自动把 `../dist/backend_server` 同步进 App（Run Script `Sync backend_server`），
所以换后端版本只需要重新构建后端 + 重新跑脚本。

### 签名 / 未签名版本的已知限制

脚本会在钥匙串里找 `Developer ID Application` 证书：

- **找到** → 先签内嵌的 `backend_server`，再用 `--options runtime` 签外层 App，最后 `codesign --verify --deep --strict` 验证。
- **没找到**（或 `SKIP_CODESIGN=1`）→ 跳过签名并打印黄色警告。

未签名版本的限制：

- 首次打开会被 Gatekeeper 拦一次，必须右键「打开」（或到「系统设置 → 隐私与安全性」点「仍要打开」）；
- 把这台机器上打的 DMG 拷给别人，对方也要重复这一步；
- DMG 没有做公证（notarization），不能静默安装，也不会自动更新；
- 内嵌后端会监听本机端口并写临时目录，个别企业终端安全软件会额外弹提示。

### 换成正式签名（可选）

1. 加入 Apple Developer Program，用 Xcode → Settings → Accounts 登录，或到 developer.apple.com 生成
   `Developer ID Application` 证书并导入钥匙串；
2. 重新跑 `./scripts/build_dmg.sh`（脚本会自动挑到证书），也可以显式指定：
   `CODESIGN_IDENTITY="Developer ID Application: XXX (TEAMID)" ./scripts/build_dmg.sh`；
3. 想彻底免掉 Gatekeeper 提示还需要公证：
   `xcrun notarytool submit AIChatApp-0.1.9.dmg --keychain-profile <profile> --wait`，再 `xcrun stapler staple`（脚本暂未包含这一步）。

⚠️ 内嵌后端是 PyInstaller onefile，**不要**在打包阶段用 `codesign --deep --options runtime` 重签它：
它运行时会把手里的 `Python.framework` 解压到临时目录再 `dlopen`，一旦开了 library validation
就会因为 Team ID 不一致而加载失败（后端起不来、`/api/health` 连不上）。
如果要做 hardened runtime + 公证，需要用 `pyinstaller --codesign-identity "Developer ID Application: …"` 重新构建后端，
让解压出来的 framework 与主程序同属一个 Team ID。

### 替换图标

图标是 `Assets.xcassets/AppIcon.appiconset` 里的占位图（深蓝渐变 + 白色 AI），替换同名 PNG 即可。
想重新生成占位图，在项目根目录执行：

```bash
swiftc -O scripts/make_icons.swift -o /tmp/make_icons && /tmp/make_icons
```

## 已知边界

- 客户端未开启 App Sandbox（需要它来启动外部 Python 进程），因此不能上架 Mac App Store 而不做改造。
- 会话与任务状态由后端持久化在 SQLite（`~/Library/Application Support/AIChatApp/aichat.db`），后端重启不丢；`/api/health` 里的 `storage` 字段会告诉设置页当前是 `sqlite` 还是回退后的 `memory`。
- 轮询间隔固定 2 秒（`ChatViewModel.pollIntervalNanoseconds`），最长轮询 300 次约 10 分钟，可在该文件里调整。
- 项目目录的默认值是编译期从 `AppSettings.swift` 的 `#filePath` 反推出来的，换机器后请在设置页用「选择…」重新指定。

## 可选：把 Python 后端打包成单文件（用户无需装 Python）

> **架构约定：本项目只发布 arm64（Apple Silicon 原生），不使用 x86_64 / Rosetta。**
> 被 Rosetta 翻译的终端**不能**用来编译/打包：翻译状态会被子进程继承，
> `arch -arm64` 只能保住直接执行的那个进程，它 fork 出来的 python / xcodebuild 仍会回到 x86_64。
> 所以打包脚本现在会直接拒绝这类终端（`scripts/arm64_native.sh` 的 `arm64_require_native_shell`）。
> 修复：访达 → 应用程序 → 终端/iTerm → 显示简介 → 取消「使用 Rosetta 打开」，重开终端确认 `uname -m` 是 `arm64`。

```bash
cd ..                                                # 项目根目录
./scripts/run_native_arm64.sh .venv/bin/python -m pip install pyinstaller   # 只需一次
./scripts/run_native_arm64.sh .venv/bin/python -m PyInstaller backend.spec --clean --noconfirm
./scripts/run_native_arm64.sh .venv/bin/python scripts/verify_backend_binary.py  # 架构 + 干净环境启动 + /api/health + 退出检查
cd AIChatApp && ./scripts/build_dmg.sh && cd ..      # 出 DMG（脚本内部会再校验一次纯 arm64）
```

产物是 `dist/backend_server`（当前约 34 MB，纯 arm64，含 azure-* SDK）。它会被 Xcode 的 Run Script 自动同步到
`AIChatApp/Resources/backend_server`，再作为 Bundle 辅助可执行文件嵌入 `.app`，
运行时由 `BackendController` 优先启动（找不到内嵌后端才回退到「项目目录 + Python 解释器」的开发模式）。
同步那一步会先 `lipo` 校验：`dist/backend_server` 必须含 arm64 且不含 x86_64，否则直接让 Xcode 构建失败。
App 侧还有两道闸门：`Info.plist` 的 `LSRequiresNativeExecution = true`（macOS 不会把 App 放进 Rosetta），
开发模式统一用 `/usr/bin/arch -arm64 <python> main.py` 启动；后端自己启动时也会检查架构并拒绝 Rosetta。
两种模式对 `PORT` / `AZURE_*` / `MERAKI_API_KEY` / `DEEPSEEK_API_KEY` 等环境变量的读法完全一致，
只有两点不同：`.env` 要放在可执行文件旁边（打包后以可执行文件所在目录为基准），
以及不支持 `RELOAD=true`（reload 需要源码模式）。

## 后续功能扩展说明

Ansible、用量统计这类都是**增量**功能，加在后端即可，前端和打包流程都不需要重构：

1. 在 `../tools/` 下新增一个模块，照着 `azure_tools.py` / `meraki_tools.py` 的写法提供 `SCHEMAS`、
   `TOOL_NAMES`、`execute()` 和 `health()`；在 `tools/__init__.py` 注册后，`/api/health`、
   `enable_tools=true` 的工具循环、`/api/test_connection` 都会自动带上它。
2. 需要新的 HTTP 入口时，在 `main.py` 里加路由，沿用 `Depends(require_auth)` 和
   `{"status": "success", ...}` / `{"status": "error", "message": ...}` 的返回约定。
3. 前端只有在要展示新数据时才需要动 UI —— 工具调用的展示已经通用化：
   后端返回的 `tool_calls_log` 会自动渲染成气泡下方的工具卡片，不用为每个新工具写界面。
4. 重新打包：`./scripts/build_dmg.sh`（自动同步最新 `dist/backend_server` → 重新生成 DMG）。
