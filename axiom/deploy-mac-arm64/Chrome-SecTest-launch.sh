#!/bin/bash
# Chrome-SecTest 安全测试浏览器启动器（macOS）
#
# 用途：从访达/程序坞手动打开 AXIOM 专用的 Sec 浏览器（独立 Profile + CDP 9223）。
#      与 dispatcher browser_broker.py 保持同一套性能参数，避免手动打开时退化卡顿。
#
# 安装方式（生成 ~/Applications/Chrome-SecTest.app）：
#   mkdir -p "$HOME/Applications/Chrome-SecTest.app/Contents/MacOS"
#   cp "$(dirname "$0")/Chrome-SecTest-launch.sh" \
#      "$HOME/Applications/Chrome-SecTest.app/Contents/MacOS/launch"
#   chmod +x "$HOME/Applications/Chrome-SecTest.app/Contents/MacOS/launch"
#
# 参数说明：
#   --enable-gpu-rasterization / --enable-zero-copy / --ignore-gpu-blocklist
#       Apple Silicon (M4) 上直通 Metal 硬件渲染管线，避免 Canvas/WebGL 二次拷贝
#   --autoplay-policy=user-gesture-required
#       拦截现代站点自发拉取的大视频/高频 rAF 动画，杜绝 Renderer CPU 飙至 100%+
#   --disable-backgrounding-occluded-windows / --disable-renderer-backgrounding
#   --disable-background-timer-throttling
#       保证 CDP 驱动下的后台事件循环全速执行（远程调试端口会削弱 JIT 深度优化）
#   --window-size=1280,800
#       默认窗口尺寸；不设屏幕外坐标，避免 macOS 将窗口识别为不可见锁 10~15fps

exec "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$HOME/Library/Application Support/Chrome-SecTest" \
  --remote-debugging-port=9223 \
  --remote-allow-origins="*" \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-background-timer-throttling \
  --enable-gpu-rasterization \
  --enable-zero-copy \
  --ignore-gpu-blocklist \
  --autoplay-policy=user-gesture-required \
  --no-first-run \
  --no-default-browser-check \
  --window-size=1280,800 "$@"
