#!/bin/bash
# 组装 DMG。
# 用法: make-dmg.sh [version]
set -euo pipefail

VERSION="${1:-1.0.0}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$HERE/build"
APP="$BUILD/AXIOM Mac Bridge.app"
DIST="$HERE/dist"
STAGE="$DIST/stage"
NAME="AXIOM-Mac-Bridge-$VERSION-arm64.dmg"

if [ ! -d "$APP" ]; then
  echo "找不到 $APP，先跑 make-app.sh" >&2
  exit 1
fi

echo "==> 组装 DMG 内容"
rm -rf "$STAGE" "$DIST/rw.dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp "$BUILD/安装说明.txt" "$STAGE/"
# Finder 里拖拽安装用的软链
ln -sf /Applications "$STAGE/Applications"

echo "==> 生成可写镜像"
hdiutil create \
  -volname "AXIOM Mac Bridge" \
  -srcfolder "$STAGE" \
  -ov -format UDRW -fs HFS+ \
  "$DIST/rw.dmg" >/dev/null

echo "==> 压缩成 UDZO"
# hdiutil convert 不会覆盖已存在的目标，必须先删
rm -f "$DIST/$NAME"
hdiutil convert "$DIST/rw.dmg" \
  -format UDZO -imagekey zlib-level=9 \
  -o "$DIST/$NAME" >/dev/null

echo "==> 给 DMG 打 adhoc 签名"
# 注意：hdiutil 没有 sign 动词，DMG 签名走 codesign。
# adhoc 签名对 Gatekeeper 没有实际作用，只是完整性标记，
# 不要拿 `spctl -a` 当验收条件 —— adhoc 必然 rejected。
codesign --force --sign - "$DIST/$NAME" || true

echo "==> 校验"
hdiutil verify "$DIST/$NAME" >/dev/null && echo "verify OK"

rm -f "$DIST/rw.dmg"
echo "==> 完成: $DIST/$NAME"
ls -la "$DIST/$NAME"
