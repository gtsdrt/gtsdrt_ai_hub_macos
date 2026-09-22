#!/bin/bash
#
# 打包可分发的 AIChatApp DMG（可重复执行）
#
#   ./scripts/build_dmg.sh
#
# 可选环境变量：
#   SKIP_CODESIGN=1                 强制跳过签名
#   CODESIGN_IDENTITY="Developer ID Application: xxx (TEAMID)"  指定签名证书
#   AICHAT_ARM64_OVERRIDE=1         仅自动化用：允许在 Rosetta 终端里继续（产物仍会被 lipo 硬校验）
#
# 产物：项目根目录 AIChatApp-<版本号>.dmg（版本号取自构建出来的 Info.plist）

set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
PROJECT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
cd "$PROJECT_DIR"

# 本项目只做 Apple Silicon 原生 arm64：被 Rosetta 翻译的 shell 直接拒绝（详见脚本内说明）
REPO_DIR="$(cd "$PROJECT_DIR/.." && pwd)"
# shellcheck source=../../scripts/arm64_native.sh
source "$REPO_DIR/scripts/arm64_native.sh"
arm64_require_native_shell

PROJECT="AIChatApp.xcodeproj"
SCHEME="AIChatApp"
CONFIGURATION="Release"
APP_NAME="AIChatApp"
VOLUME_NAME="AIChatApp"

BUILD_DIR="$PROJECT_DIR/build"
ARCHIVE_PATH="$BUILD_DIR/$APP_NAME.xcarchive"
DERIVED_DATA="$BUILD_DIR/dmg-derived"
STAGING_DIR="$BUILD_DIR/dmg-staging"
APP_SRC="$ARCHIVE_PATH/Products/Applications/$APP_NAME.app"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step() { printf "${GREEN}==>${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}warning:${NC} %s\n" "$*"; }
fail() { printf "${RED}error:${NC} %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. Archive

step "清理上次产物"
rm -rf "$ARCHIVE_PATH" "$STAGING_DIR" "$DERIVED_DATA"

step "xcodebuild archive（${CONFIGURATION}）"
xcodebuild \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -configuration "$CONFIGURATION" \
  -archivePath "$ARCHIVE_PATH" \
  -derivedDataPath "$DERIVED_DATA" \
  "${ARM64_XCODEBUILD_FLAGS[@]}" \
  archive \
  | tail -5

[ -d "$APP_SRC" ] || fail "archive 里没找到 $APP_SRC"

# 架构硬校验：主程序 + 内嵌后端 + 整个 bundle 都不允许出现 x86_64
step "校验产物架构（纯 arm64）"
arm64_assert_binary "$APP_SRC/Contents/MacOS/$APP_NAME"
for candidate in \
    "$APP_SRC/Contents/MacOS/backend_server" \
    "$APP_SRC/Contents/Resources/backend_server/backend_server"; do
  [ -e "$candidate" ] && arm64_assert_binary "$candidate"
done
arm64_assert_bundle "$APP_SRC"

# ---------------------------------------------------------------- 2. 版本号

VERSION=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" "$APP_SRC/Contents/Info.plist" 2>/dev/null || true)
if [ -z "$VERSION" ] || [[ "$VERSION" == *'$('* ]]; then
  # Info.plist 里写的是 $(MARKETING_VERSION)，退回从工程文件读
  VERSION=$(sed -n 's/.*MARKETING_VERSION = \(.*\);/\1/p' "$PROJECT/project.pbxproj" | head -1)
fi
[ -n "$VERSION" ] || VERSION="0.0.0"
step "版本号：$VERSION"

# ---------------------------------------------------------------- 3. 准备 DMG 内容

step "准备 DMG 目录（app + /Applications 快捷方式 + README）"
mkdir -p "$STAGING_DIR"
ditto "$APP_SRC" "$STAGING_DIR/$APP_NAME.app"
ln -s /Applications "$STAGING_DIR/Applications"

cat > "$STAGING_DIR/README.txt" <<'EOF'
AIChatApp 安装说明

1. 把左边的 AIChatApp.app 拖到右边的 Applications 文件夹。
2. 首次打开：在「应用程序」里右键点 AIChatApp → 打开 → 再点「打开」。
   （未用开发者证书签名的版本会被 Gatekeeper 拦一次，右键打开即可绕过；
     之后双击就能正常启动。）
3. 后端已内嵌在 App 里，首次启动会自动运行，无需安装 Python。

如提示「无法验证开发者」：系统设置 → 隐私与安全性 → 仍要打开。
EOF

# ---------------------------------------------------------------- 4. 签名（可选）

DETECTED_IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
  | sed -n 's/.*"\(Developer ID Application: .*\)"/\1/p' | head -1)
IDENTITY="${CODESIGN_IDENTITY:-$DETECTED_IDENTITY}"

if [ "${SKIP_CODESIGN:-0}" = "1" ]; then
  warn "SKIP_CODESIGN=1：跳过签名。未签名版本首次打开需要右键 →「打开」。"
elif [ -z "$IDENTITY" ]; then
  warn "没找到 Developer ID 证书：跳过签名。未签名版本首次打开需要右键 →「打开」。"
else
  step "签名：$IDENTITY"
  APP="$STAGING_DIR/$APP_NAME.app"

  # 内嵌的 PyInstaller 后端要单独签，且【不能】带 hardened runtime：
  # 它运行时会从 _internal 里 dlopen Python.framework 与各 .so/.dylib，
  # 开了 library validation 会因 Team ID 不一致而加载失败。
  # 后端位置：onedir 优先（Contents/Resources/backend_server/backend_server），
  # onefile 回退（Contents/MacOS/backend_server）。
  BACKEND_BIN=""
  if [ -x "$APP/Contents/Resources/backend_server/backend_server" ]; then
    BACKEND_BIN="$APP/Contents/Resources/backend_server/backend_server"
  elif [ -x "$APP/Contents/MacOS/backend_server" ]; then
    BACKEND_BIN="$APP/Contents/MacOS/backend_server"
  fi

  if [ -n "$BACKEND_BIN" ]; then
    codesign --force --timestamp --sign "$IDENTITY" "$BACKEND_BIN"
  fi

  # onedir 的 _internal 里有 Python.framework 与多个 .dylib/.so 的 Mach-O，
  # 逐个补签（同样不带 runtime），否则 --verify --deep 会因嵌套未签代码而失败。
  if [ -d "$APP/Contents/Resources/backend_server/_internal" ]; then
    find "$APP/Contents/Resources/backend_server/_internal" -type f -exec /bin/sh -c '
      for f do
        case "$(/usr/bin/file -b "$f" 2>/dev/null)" in
          *Mach-O*) codesign --force --timestamp --sign "$1" "$f" 2>/dev/null || true ;;
        esac
      done
    ' _ "$IDENTITY" {} +
  fi

  # 外层 app 用 hardened runtime；不加 --deep，避免把内嵌后端重签成 runtime
  codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP"

  step "验证签名"
  codesign --verify --deep --strict --verbose=2 "$APP" || fail "签名验证失败"
  step "签名验证通过"
fi

# ---------------------------------------------------------------- 5. 生成 DMG

DMG_NAME="$APP_NAME-$VERSION.dmg"
DMG_PATH="$PROJECT_DIR/$DMG_NAME"

step "hdiutil 打包 $DMG_NAME"
hdiutil create \
  -volname "$VOLUME_NAME" \
  -srcfolder "$STAGING_DIR" \
  -ov -format UDZO \
  "$DMG_PATH" \
  | tail -3

step "校验 DMG"
hdiutil verify "$DMG_PATH" >/dev/null || fail "DMG 校验失败"

SIZE=$(du -h "$DMG_PATH" | cut -f1)
echo
printf "${GREEN}完成${NC}\n"
echo "DMG 路径：$DMG_PATH"
echo "文件大小：$SIZE"
