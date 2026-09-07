#!/bin/bash
# AXIOM 测试浏览器（Chrome-SecTest）：点一下打开专用浏览器，人工登录/维护会话。
# AI 空闲时 Broker 会自动退出无头实例并释放 Profile 锁，此时即可正常打开。
PORT=9223
PROFILE="$HOME/Library/Application Support/Chrome-SecTest"
CF_APP="$HOME/Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app"
[ -d "$CF_APP" ] || CF_APP="/Applications/Google Chrome.app"
if curl -s --max-time 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  osascript -e 'display notification "测试浏览器正被 AI 使用中（无头模式）。任务空闲后会自动释放，届时再打开即可登录。" with title "AXIOM 测试浏览器"'
  exit 0
fi
open -na "$CF_APP" --args --user-data-dir="$PROFILE" --remote-debugging-port=$PORT --remote-allow-origins=* --no-first-run --no-default-browser-check --window-size=1280,800
