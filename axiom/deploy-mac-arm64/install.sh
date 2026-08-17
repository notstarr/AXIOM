#!/usr/bin/env bash
# AXIOM macOS (Apple Silicon ARM64) 一键极速部署脚本
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AXIOM_ROOT="$(cd "$SCRIPT_DIR/../" && pwd)"
INSTALL_TARGET="${1:-$HOME/.axiom}"
PORT="${AXIOM_PORT:-8888}"
HOST="${AXIOM_HOST:-127.0.0.1}"

log() { printf '\033[1;32m[axiom-mac-install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[axiom-mac-install] WARN:\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[axiom-mac-install] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# 1. 架构与操作系统校验
log "正在检查操作系统与芯片架构..."
[[ "$(uname -s)" == "Darwin" ]] || fail "当前脚本专为 macOS 设计，当前系统为: $(uname -s)"
[[ "$(uname -m)" == "arm64" ]] || fail "当前脚本专为 Apple Silicon (ARM64 / M系列芯片) 设计，检测到: $(uname -m)"

# 2. 检查基础命令
log "检查基础环境工具..."
command -v curl >/dev/null 2>&1 || fail "缺少 curl 工具"
command -v git >/dev/null 2>&1 || fail "缺少 git 工具"

# 检查 Python / uv 环境
if ! command -v uv >/dev/null 2>&1; then
  log "未检测到 uv 包管理器，正在通过官方脚本安装 uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 3. 创建/同步 Python 虚拟环境与依赖
log "准备 Python 虚拟环境与核心依赖 (uv sync)..."
cd "$AXIOM_ROOT"
if [[ ! -d ".venv" ]]; then
  uv venv .venv --python 3.14 || uv venv .venv --python 3.13 || uv venv .venv
fi
source .venv/bin/activate
uv sync --frozen || uv sync

# 4. 检查 Docker (用于托管 PostgreSQL)
log "检查数据库容器支持 (Docker)..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  log "Docker 已就绪，启动/确保 PostgreSQL 容器运行..."
  docker compose -f "$AXIOM_ROOT/compose.yaml" up -d postgres
else
  warn "未检测到 Docker 或 Docker 守护进程未启动，请确保本地已有可用的 PostgreSQL 服务。"
fi

# 5. 安装 macOS LaunchAgent 守护配置
log "配置 macOS LaunchAgent 系统服务守护 (127.0.0.1:$PORT)..."
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS_DIR" "$HOME/.local/share/axiom-v3/logs" "$HOME/.local/share/axiom-v3/run"

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
        <string>$AXIOM_ROOT/.venv/bin/python</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>axiom.server:app</string>
        <string>--host</string>
        <string>$HOST</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$AXIOM_ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$AXIOM_ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin</string>
        <key>PYTHONPATH</key>
        <string>$AXIOM_ROOT/src</string>
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
        <string>$AXIOM_ROOT/.venv/bin/python</string>
        <string>-m</string>
        <string>axiom.dispatcher.runner</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$AXIOM_ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$AXIOM_ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin</string>
        <key>PYTHONPATH</key>
        <string>$AXIOM_ROOT/src</string>
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

# 6. 安装管理命令行 axiomctl 到 /usr/local/bin 或 ~/.local/bin
mkdir -p "$HOME/.local/bin"
cp "$SCRIPT_DIR/axiomctl" "$HOME/.local/bin/axiomctl"
chmod +x "$HOME/.local/bin/axiomctl"

log "启动 AXIOM 服务..."
"$HOME/.local/bin/axiomctl" start

# 7. 回显管理员初始凭据（首次启动由 AuthService 自动生成随机密码）
CRED_FILE="$HOME/.local/share/axiom-v3/credentials.txt"
log "等待服务完成初始化以生成管理员凭据..."
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [[ -f "$CRED_FILE" ]] && break
  sleep 1
done

if [[ -f "$CRED_FILE" ]]; then
  ADMIN_USER="$(grep -m1 '^Username:' "$CRED_FILE" | cut -d: -f2- | xargs)"
  ADMIN_PASS="$(grep -m1 '^Password:' "$CRED_FILE" | cut -d: -f2- | xargs)"
  log "================================================="
  log "🔐 管理员登录凭据（请妥善保存，登录后建议立即修改密码）"
  log "================================================="
  printf '\033[1;32m[axiom-mac-install] 登录地址: \033[0m\033[1;36mhttp://%s:%s\033[0m\n' "$HOST" "$PORT"
  printf '\033[1;32m[axiom-mac-install] 登录账号: \033[0m\033[1;36m%s\033[0m\n' "$ADMIN_USER"
  printf '\033[1;32m[axiom-mac-install] 登录密码: \033[0m\033[1;33;41m %s \033[0m\n' "$ADMIN_PASS"
  log "凭据文件: $CRED_FILE (权限 600，仅当前用户可读)"
  log "================================================="
else
  warn "暂未找到凭据文件，可稍后执行: cat $CRED_FILE"
fi

log "================================================="
log "🎉 AXIOM Mac ARM64 部署完成！"
log "访问控制台: http://$HOST:$PORT"
log "日常管理命令:"
log "  axiomctl status   - 查看服务状态"
log "  axiomctl restart  - 重启所有服务"
log "  axiomctl logs     - 查看实时日志"
log "  axiomctl stop     - 停止服务"
log "================================================="
