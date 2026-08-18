#!/usr/bin/env bash
# ==============================================================================
# AXIOM V2 一键平滑升级脚本 (upgrade.sh)
# 
# 功能：
# 1. 检查 Docker/PG 状态与工作区安全
# 2. 自动拉取远端最新代码 (main 分支)
# 3. 同步 Python 依赖 (uv / pip)
# 4. 执行 Alembic 数据库 Schema 迁移
# 5. 平滑重启 serve & runner 守护服务 (macOS launchd / Linux systemd)
# 6. 健康检查与超时回滚
# ==============================================================================

set -euo pipefail

# 颜色与输出格式
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AXIOM_DIR="${AXIOM_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
AXIOM_DATA_DIR="${AXIOM_DATA_DIR:-$HOME/.local/share/axiom-v3}"
PG_ENV_FILE="${AXIOM_PG_ENV_FILE:-$AXIOM_DATA_DIR/postgresql.env}"

log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
}

# 1. 切换到项目目录
cd "$AXIOM_DIR"

echo -e "${BOLD}======================================================${NC}"
echo -e "${BOLD}         AXIOM V2 自动化一键升级系统                  ${NC}"
echo -e "${BOLD}======================================================${NC}"

# 加载 PG 环境变量
if [[ -f "$PG_ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$PG_ENV_FILE"
    set +a
fi

DB_HOST="${AXIOM_DB_HOST:-127.0.0.1}"
DB_PORT="${AXIOM_DB_PORT:-55432}"

# 阶段 1: 前置健康检查
log_info ">>> [1/6] 检查前置依赖与数据库连接..."

# 检查 Git 仓库
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log_error "目录 $AXIOM_DIR 不是一个有效的 Git 仓库。"
    exit 1
fi

OLD_COMMIT=$(git rev-parse HEAD)
OLD_COMMIT_SHORT=$(git rev-parse --short HEAD)
log_info "当前代码版本: ${BOLD}${OLD_COMMIT_SHORT}${NC}"

# 检查本地修改
if ! git diff --quiet || ! git diff --cached --quiet; then
    log_warn "检测到本地工作区存在未提交的修改。"
    if [[ "${1:-}" != "--force" ]]; then
        log_error "升级已中止以保护本地修改。请先提交/暂存，或使用 './scripts/upgrade.sh --force' 强制暂存并升级。"
        exit 1
    else
        log_warn "已启用 --force，正在自动执行 git stash 暂存修改..."
        git stash push -m "upgrade-auto-stash-$(date +%Y%m%d%H%M%S)"
    fi
fi

# 检查 Docker & PostgreSQL 端口
if ! nc -z "$DB_HOST" "$DB_PORT" 2>/dev/null; then
    log_warn "未检测到 PostgreSQL ($DB_HOST:$DB_PORT)。尝试启动 Docker 容器..."
    if command -v docker >/dev/null 2>&1; then
        docker compose -f "$AXIOM_DIR/compose.yaml" -f "$AXIOM_DIR/compose.host.yaml" up -d postgres || {
            log_error "无法通过 docker compose 启动 postgres 服务，请检查 Docker Desktop 是否运行。"
            exit 1
        }
        log_info "等待数据库就绪..."
        sleep 3
    else
        log_error "未安装 docker 命令，且 $DB_HOST:$DB_PORT 无法连接。"
        exit 1
    fi
fi

# 阶段 2: 拉取远端最新代码
log_info ">>> [2/6] 同步远端最新代码 (git pull)..."
git fetch origin main

NEW_REMOTE_COMMIT=$(git rev-parse origin/main)
NEW_REMOTE_SHORT=$(git rev-parse --short origin/main)

if [ "$OLD_COMMIT" = "$NEW_REMOTE_COMMIT" ]; then
    log_success "当前代码已是最新 (${OLD_COMMIT_SHORT})。"
else
    log_info "准备升级: ${OLD_COMMIT_SHORT} -> ${NEW_REMOTE_SHORT}"
    git pull --ff-only origin main || {
        log_error "Git pull 失败（可能存在分叉分支）。"
        exit 1
    }
fi

# 阶段 3: 同步 Python 依赖
log_info ">>> [3/6] 同步 Python 虚拟环境与依赖..."
if [ -f "$AXIOM_DIR/.venv/bin/python" ]; then
    if command -v uv >/dev/null 2>&1; then
        log_info "使用 uv 同步依赖..."
        (cd "$AXIOM_DIR" && uv sync --frozen 2>/dev/null || uv pip install -e .)
    else
        log_info "使用 pip 同步依赖..."
        "$AXIOM_DIR/.venv/bin/pip" install -e . --quiet
    fi
else
    log_warn "未检测到 .venv 虚拟环境，跳过 Python 依赖同步。"
fi

# 阶段 4: 数据库 Schema 迁移
log_info ">>> [4/6] 执行数据库 Schema 迁移 (axiom db upgrade)..."
if [ -f "$AXIOM_DIR/.venv/bin/axiom" ]; then
    MIGRATOR_URL="${AXIOM_MIGRATOR_DATABASE_URL:-}"
    if [ -n "$MIGRATOR_URL" ]; then
        "$AXIOM_DIR/.venv/bin/axiom" db upgrade --database-url "$MIGRATOR_URL" || {
            log_error "数据库迁移失败！正在尝试回滚代码至 $OLD_COMMIT_SHORT..."
            git reset --hard "$OLD_COMMIT"
            exit 1
        }
    else
        "$AXIOM_DIR/.venv/bin/axiom" db upgrade || {
            log_error "数据库迁移失败！正在尝试回滚代码至 $OLD_COMMIT_SHORT..."
            git reset --hard "$OLD_COMMIT"
            exit 1
        }
    fi
    log_success "数据库 Schema 已同步至最新版本。"
fi

# 阶段 5: 重启后台服务
log_info ">>> [5/6] 平滑重启 AXIOM 服务进程..."

restart_services() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        local uid_num
        uid_num=$(id -u)
        log_info "检测到 macOS 环境，使用 launchctl 重启服务..."
        launchctl kickstart -k "gui/${uid_num}/com.xrayai.axiom" 2>/dev/null || true
        launchctl kickstart -k "gui/${uid_num}/com.xrayai.axiom.runner" 2>/dev/null || true
    elif command -v systemctl >/dev/null 2>&1; then
        log_info "检测到 Linux systemd 环境，重启服务..."
        systemctl --user restart axiom axiom-runner 2>/dev/null || systemctl restart axiom axiom-runner 2>/dev/null || true
    else
        log_warn "未识别到 launchd 或 systemd 管理器。若为前台进程，请手动重启 axiom serve 与 runner。"
    fi
}

restart_services

# 阶段 6: 健康检查与验证
log_info ">>> [6/6] 进行健康检查 (Healthcheck)..."
HEALTH_OK=0
API_URL="http://127.0.0.1:8888"

for i in $(seq 1 15); do
    if curl -s -f -m 2 "$API_URL/api/tasks" >/dev/null 2>&1 || curl -s -f -m 2 "$API_URL/" >/dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
    sleep 1
done

if [ "$HEALTH_OK" -eq 1 ]; then
    FINAL_COMMIT_SHORT=$(git rev-parse --short HEAD)
    echo ""
    echo -e "${GREEN}${BOLD}======================================================${NC}"
    echo -e "${GREEN}${BOLD} ✔ AXIOM V2 升级成功！                                ${NC}"
    echo -e "${GREEN}${BOLD}   当前版本: ${FINAL_COMMIT_SHORT}                   ${NC}"
    echo -e "${GREEN}${BOLD}   服务状态: http://127.0.0.1:8888 正常运行           ${NC}"
    echo -e "${GREEN}${BOLD}======================================================${NC}"
else
    log_error "服务在重启 15 秒后未响应 8888 端口！"
    log_warn "请检查日志: tail -n 50 ~/.local/share/axiom-v3/logs/api.log 或 server.log"
    exit 1
fi
