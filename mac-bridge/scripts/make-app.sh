#!/bin/bash
# 组装 .app bundle。
# 用法: make-app.sh [version]
set -euo pipefail

VERSION="${1:-1.0.0}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$HERE/build"
APP="$BUILD/AXIOM Mac Bridge.app"
BIN="$HERE/bin/axiom-mac-bridge"

export PATH="$HOME/.local/go/bin:$PATH"
export CGO_ENABLED=1
export MACOSX_DEPLOYMENT_TARGET=15.0
SDK="$(xcrun --show-sdk-path)"
export CGO_CFLAGS="-I$SDK/usr/include"
export CGO_LDFLAGS="-F$SDK/System/Library/Frameworks"

echo "==> 编译 ($VERSION)"
go build -C "$HERE" -trimpath \
  -ldflags "-s -w -X main.version=$VERSION" \
  -o bin/axiom-mac-bridge ./cmd/axiom-mac-bridge

echo "==> 组装 bundle"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/axiom-mac-bridge"
chmod +x "$APP/Contents/MacOS/axiom-mac-bridge"
cp "$BUILD/Info.plist" "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# 版本号写进 plist
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist"

echo "==> 校验 plist"
plutil -lint "$APP/Contents/Info.plist"

echo "==> adhoc 签名"
# 必须在改完 bundle 内容之后签。adhoc（-s -）不需要证书。
codesign --force --sign - --identifier com.hexin.axiom.macbridge "$APP"
codesign --verify --strict --verbose=2 "$APP"

echo "==> 完成: $APP"
ls -la "$APP/Contents/MacOS/"
