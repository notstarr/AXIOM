#!/bin/bash
# Launch Chrome-SecTest profile with CDP port 9223.
# When MITM_PROXY=1, also routes traffic through mitmproxy on port 18080.
# Safe to call multiple times — if already running, does nothing.

CDP_PORT=9223
PROFILE_DIR="$HOME/Library/Application Support/Chrome-SecTest"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Check if already running
if curl -s "http://localhost:$CDP_PORT/json/list" >/dev/null 2>&1; then
    echo "✅ Chrome-SecTest already running on port $CDP_PORT"
    exit 0
fi

echo "🚀 Launching Chrome-SecTest..."

EXTRA_FLAGS=""
if [ "${MITM_PROXY:-0}" = "1" ]; then
    EXTRA_FLAGS="--proxy-server=http://127.0.0.1:18080 --ignore-certificate-errors"
    echo "🔀 mitmproxy mode: traffic → 127.0.0.1:18080"
fi

"$CHROME" \
  --user-data-dir="$PROFILE_DIR" \
  --remote-debugging-port=$CDP_PORT \
  --remote-allow-origins='*' \
  --no-first-run \
  --window-size=1280,800 \
  $EXTRA_FLAGS \
  2>/dev/null &

# Wait up to 10s for port to be ready
for i in $(seq 1 10); do
    sleep 1
    if curl -s "http://localhost:$CDP_PORT/json/list" >/dev/null 2>&1; then
        echo "✅ Chrome-SecTest ready (${i}s)"
        exit 0
    fi
done

echo "❌ Chrome-SecTest failed to start"
exit 1
