# 自动发布和更新

## 日常使用

1. 修改应用代码并合入 `main`（可直接从 GitHub 网页编辑）。
2. Actions 的 **Build and publish macOS update** 自动使用 Apple Silicon runner 构建 Python 后端、Xcode App、DMG 和 ZIP。
3. 验证后发布新的 GitHub Release，包含 `appcast.xml`；App 默认每小时检查，自动下载，退出时安装。

菜单 **AIChatApp → 检查更新…** 和 **设置 → 软件更新** 均可手动检查。关闭自动检查/下载后，Sparkle 会记住用户选择。自动安装可能由 Sparkle 根据包签名、安装位置及系统权限要求用户确认；不会为了升级主动中断当前对话。首次安装或缺少更新器的旧版必须从 Releases 手动安装一次新版本，建议放到 `/Applications` 后运行。

发布整包包含客户端和后端；本地 SQLite、UserDefaults 与 Keychain 位于 App bundle 外，更新保留它们。重启使用现有 `applicationWillTerminate` 路径停止本应用启动的后端。

## 配置与密钥

- 更新地址固定为 `https://github.com/gtsdrt/gtsdrt_ai_hub_macos/releases/latest/download/appcast.xml`，不需要 GitHub Pages 或个人访问令牌。
- 公钥提交在 `AIChatApp/Support/Info.plist` 的 `SUPublicEDKey`；`SURequireSignedFeed` 强制验证 feed 签名，ZIP 另有 Ed25519 签名。
- `SPARKLE_PRIVATE_KEY` 存为 GitHub Actions repository secret。本机备份在 `.sparkle/eddsa-private.key`，权限为 `0600`，整个目录被 Git 忽略。
- 不把私钥、`.env`、API key、运行数据库放进 Release 或构建 artifact。PR 不接收私钥；主分支签名步骤结束后删除 runner 上的临时私钥文件。
- 当前无 Developer ID 证书，CI 使用 ad-hoc 签名且关闭 hardened runtime，避免没有 Team ID 的 App 因 library validation 不能加载 Sparkle。此方式保留既有首次安装 Gatekeeper 限制；Ed25519 更新签名不等同于 Apple Developer ID 签名/公证。

私钥丢失后不能直接换一个公钥并继续向已有用户分发。请将 `.sparkle/eddsa-private.key` 安全备份到密码管理器，按 [Sparkle 密钥迁移说明](https://sparkle-project.org/documentation/) 处理更换。

## 版本和发布

工程的 `MARKETING_VERSION` 前两段决定系列（目前 `0.3`）。Actions 自动得到 `0.3.<run_number>` 和 `CFBundleVersion=10.<run_number>.<run_attempt>`。Sparkle 按构建号比较，因此无需每次手改版本；重试使用独立 tag `v<版本>-build.<run_attempt>`。不要删除重建整个 workflow 来重置运行编号，否则要提升工程/脚本的构建号系列。

只有 `main` 的应用代码、Python、依赖、脚本和 workflow 改动触发发布；纯文档修改不发布。可以在 Actions 使用 **Run workflow** 手动触发。其他分支上的手动运行只构建，不发布。

生产构建串行执行；先发布 draft 并上传 ZIP、DMG 和 feed，再将 release 标记 latest。检查失败则不切换 latest，已发布版本继续可用。

## 构建验证与故障排查

- PR 使用与发布相同的 arm64、PyInstaller、Xcode 和 DMG 构建流程；只上传 artifact，不发布。
- `scripts/build_release.sh` 确认原生 Python 架构，并运行现有 `verify_backend_binary.py` 检查打包后端的 `/api/health` 和进程退出。
- 原有架构闸门继续要求整个 App/后端纯 arm64。`prepare_sparkle.sh` 将 framework/helper 裁剪为 arm64，从内到外重签名，并保留 helper 的 entitlements，修复 Xcode 移除头文件导致的原签名失效。
- `generate_appcast` 从真实 App 提取版本、最低系统及硬件要求，签署 ZIP/feed；`sign_update --verify` 验证 feed。
- `verify_update_feed.py` 独立使用内嵌公钥验证 ZIP，检查实际版本、文件大小和 GitHub HTTPS 地址。
- 若 Actions 报 `Missing repository secret SPARKLE_PRIVATE_KEY`，配置 secret 后重跑。若签名错误，检查私钥与 `Info.plist` 公钥对应。
- 若检查更新返回 404，确认最新 Release 已包含 `appcast.xml`；不要手工把缺少 appcast 的旧 release 设为 latest。
- 更新错误记录在 macOS Console 的 Sparkle 日志中。菜单手动检查会显示网络/签名错误；后台失败由 Sparkle 延后重试。

本地调试发布脚本（需要已安装 requirements、PyInstaller、cryptography）：

```bash
RELEASE_VERSION=0.3.0 RELEASE_BUILD=10.0.1 bash scripts/build_release.sh
```

如需本地签名测试，将 `PUBLISH_UPDATE=1`、`SPARKLE_PRIVATE_KEY_FILE`、`SPARKLE_TOOLS_DIR` 和 `RELEASE_DOWNLOAD_URL` 设置为对应路径/URL。产物目录 `release-output/` 被 Git 忽略。本地测试的高版本 feed 不应上传为正式最新版本。

参考：[Sparkle SwiftUI 集成](https://sparkle-project.org/documentation/programmatic-setup/)、[发布和签名](https://sparkle-project.org/documentation/publishing/)、[GitHub runner 架构](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)。
