#!/usr/bin/env bash
# AXIOM macOS (Apple Silicon ARM64) 独立单行一键安装脚本
# 支持通过 `curl -fsSL <URL> | bash` 直接运行
set -Eeuo pipefail

AXIOM_INSTALL_DIR="${AXIOM_INSTALL_DIR:-$HOME/AXIOM}"
REPO_URL="${AXIOM_REPO_URL:-https://github.com/notstarr/AXIOM.git}"
PORT="${AXIOM_PORT:-8888}"
HOST="${AXIOM_HOST:-127.0.0.1}"

log() { printf '\033[1;32m[axiom-install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[axiom-install] WARN:\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[axiom-install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# 1. 架构校验
log "1/5 检查系统与芯片架构..."
[[ "$(uname -s)" == "Darwin" ]] || fail "仅支持 macOS，当前系统: $(uname -s)"
[[ "$(uname -m)" == "arm64" ]] || fail "仅支持 Apple Silicon (M系列芯片)，当前架构: $(uname -m)"

# 2. 检查并克隆/更新代码
log "2/5 拉取 AXIOM 最新源码至 $AXIOM_INSTALL_DIR..."
if [[ -d "$AXIOM_INSTALL_DIR/.git" ]]; then
  cd "$AXIOM_INSTALL_DIR"
  git pull origin main || true
else
  git clone "$REPO_URL" "$AXIOM_INSTALL_DIR"
  cd "$AXIOM_INSTALL_DIR"
fi

# 3. 检查/安装 uv 并构建虚拟环境
log "3/5 准备 Python 虚拟环境与依赖 (uv sync)..."
if ! command -v uv >/dev/null 2>&1; then
  log "正在安装 uv 工具..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [[ ! -d ".venv" ]]; then
  uv venv .venv --python 3.14 || uv venv .venv --python 3.13 || uv venv .venv
fi
source .venv/bin/activate
uv sync --frozen || uv sync

# 4. 检查 Docker 托管 PostgreSQL
log "4/5 检查/拉起 PostgreSQL 数据库..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker compose -f "$AXIOM_INSTALL_DIR/compose.yaml" up -d postgres 2>/dev/null || true
else
  warn "未检测到运行中的 Docker，请确保本地已有 PostgreSQL 或启动 Docker Desktop。"
fi

# 5. 配置 LaunchAgent 守护与 CLI
log "5/5 配置系统服务守护 (LaunchAgent) 与 axiomctl..."
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS_DIR" "$HOME/.local/share/axiom-v3/logs" "$HOME/.local/share/axiom-v3/run" "$HOME/.local/bin"

SERVER_PLIST="$LAUNCH_AGENTS_DIR/com.xrayai.axiom.plist"
RUNNER_PLIST="$LAUNCH_AGENTS_DIR/com.xrayai.axiom.runner.plist"

cat <<EOF > "$SERVER_PLIST"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xrayai.axiom</string>
    <key>ProgramArguments</key>
    <array>
        <string>$AXIOM_INSTALL_DIR/.venv/bin/python</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>axiom.server:app</string>
        <string>--host</string>
        <string>$HOST</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$AXIOM_INSTALL_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$AXIOM_INSTALL_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin</string>
        <key>PYTHONPATH</key>
        <string>$AXIOM_INSTALL_DIR/src</string>
        <key>AXIOM_DATA_DIR</key>
        <string>$HOME/.local/share/axiom-v3</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.local/share/axiom-v3/logs/server.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.local/share/axiom-v3/logs/server.log</string>
</dict>
</plist>
EOF

cat <<EOF > "$RUNNER_PLIST"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.xrayai.axiom.runner</string>
    <key>ProgramArguments</key>
    <array>
        <string>$AXIOM_INSTALL_DIR/.venv/bin/python</string>
        <string>-m</string>
        <string>axiom.dispatcher.runner</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$AXIOM_INSTALL_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$AXIOM_INSTALL_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin</string>
        <key>PYTHONPATH</key>
        <string>$AXIOM_INSTALL_DIR/src</string>
        <key>AXIOM_DATA_DIR</key>
        <string>$HOME/.local/share/axiom-v3</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.local/share/axiom-v3/logs/dispatcher.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.local/share/axiom-v3/logs/dispatcher.log</string>
</dict>
</plist>
EOF

# 生成 axiomctl 到 ~/.local/bin
cat <<'EOF' > "$HOME/.local/bin/axiomctl"
#!/usr/bin/env bash
set -euo pipefail
ACTION="${1:-status}"
DOMAIN="gui/$(id -u)"
SERVER_LABEL="com.xrayai.axiom"
RUNNER_LABEL="com.xrayai.axiom.runner"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
HOST="${AXIOM_HOST:-127.0.0.1}"
PORT="${AXIOM_PORT:-8888}"

is_running() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }

case "$ACTION" in
  start)
    [[ -f "$LAUNCH_AGENTS_DIR/$SERVER_LABEL.plist" ]] && launchctl bootstrap "$DOMAIN" "$LAUNCH_AGENTS_DIR/$SERVER_LABEL.plist" 2>/dev/null || true
    [[ -f "$LAUNCH_AGENTS_DIR/$SERVER_LABEL.plist" ]] && launchctl kickstart -k "$DOMAIN/$SERVER_LABEL" 2>/dev/null || true
    [[ -f "$LAUNCH_AGENTS_DIR/$RUNNER_LABEL.plist" ]] && launchctl bootstrap "$DOMAIN" "$LAUNCH_AGENTS_DIR/$RUNNER_LABEL.plist" 2>/dev/null || true
    [[ -f "$LAUNCH_AGENTS_DIR/$RUNNER_LABEL.plist" ]] && launchctl kickstart -k "$DOMAIN/$RUNNER_LABEL" 2>/dev/null || true
    echo "[axiomctl] 服务已启动: http://$HOST:$PORT"
    ;;
  stop)
    [[ -f "$LAUNCH_AGENTS_DIR/$RUNNER_LABEL.plist" ]] && launchctl bootout "$DOMAIN" "$LAUNCH_AGENTS_DIR/$RUNNER_LABEL.plist" 2>/dev/null || true
    [[ -f "$LAUNCH_AGENTS_DIR/$SERVER_LABEL.plist" ]] && launchctl bootout "$DOMAIN" "$LAUNCH_AGENTS_DIR/$SERVER_LABEL.plist" 2>/dev/null || true
    echo "[axiomctl] 服务已停止"
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$SERVER_LABEL" 2>/dev/null || true
    launchctl kickstart -k "$DOMAIN/$RUNNER_LABEL" 2>/dev/null || true
    echo "[axiomctl] 重启完成: http://$HOST:$PORT"
    ;;
  status)
    echo "================================================="
    echo "📊 AXIOM macOS 状态检查"
    echo "================================================="
    echo "• 访问端点: http://$HOST:$PORT"
    printf "• API 服务: %s\n" "$(is_running "$SERVER_LABEL" && echo "🟢 RUNNING" || echo "🔴 STOPPED")"
    printf "• 调度引擎: %s\n" "$(is_running "$RUNNER_LABEL" && echo "🟢 RUNNING" || echo "🔴 STOPPED")"
    health="$(curl -s --connect-timeout 2 "http://$HOST:$PORT/health" 2>/dev/null || true)"
    printf "• 健康状态: %s\n" "${health:-无法连接}"
    echo "================================================="
    ;;
  logs)
    target="${2:-all}"
    log_dir="$HOME/.local/share/axiom-v3/logs"
    if [[ "$target" == "server" ]]; then
      tail -f "$log_dir/server.log"
    elif [[ "$target" == "runner" || "$target" == "dispatcher" ]]; then
      tail -f "$log_dir/dispatcher.log"
    else
      tail -f "$log_dir/server.log" "$log_dir/dispatcher.log"
    fi
    ;;
  *)
    echo "用法: axiomctl {start|stop|restart|status|logs [server|runner]}"
    exit 1
    ;;
esac
EOF
chmod +x "$HOME/.local/bin/axiomctl"

# 启动服务
"$HOME/.local/bin/axiomctl" start

log "================================================="
log "🎉 AXIOM Mac ARM64 一键部署成功！"
log "访问地址: http://$HOST:$PORT"
log "控制命令: axiomctl status | restart | logs | stop"
log "================================================="
