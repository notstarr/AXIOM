# AXIOM macOS (Apple Silicon ARM64) 一键部署指南

专为配备 Apple Silicon (M1/M2/M3/M4 系列芯片) 的 macOS 设备优化的原生部署方案。无需 Docker 虚拟化损耗，充分发挥 Mac 原生 ARM64 性能。

---

## ⚡ 极速一键安装

在终端直接运行安装脚本：

```bash
cd deploy-mac-arm64
./install.sh
```

脚本将自动完成：
1. 检测 macOS 与 Apple Silicon ARM64 架构；
2. 自动检查或安装 `uv` 包管理器；
3. 初始化 Python 虚拟环境并编译 ARM64 原生依赖；
4. 启动/检查 PostgreSQL 数据库；
5. 注册系统级后台服务（macOS `LaunchAgent` 守护）；
6. 将全局管理工具 `axiomctl` 安装至 `~/.local/bin`。

---

## 🛠️ 日常运维管理 (axiomctl)

安装完成后，可直接使用 `axiomctl` 命令管理 AXIOM：

```bash
# 查看服务状态与健康检查
axiomctl status

# 重启所有服务（API 与 调度器）
axiomctl restart

# 实时查看日志
axiomctl logs

# 仅查看调度器日志
axiomctl logs runner

# 仅查看 Web API 日志
axiomctl logs server

# 停止所有服务
axiomctl stop

# 启动服务
axiomctl start
```

---

## 🌐 访问地址
* 控制台 Web 界面：`http://127.0.0.1:8888`
* 数据库与运行数据存储目录：`~/.local/share/axiom-v3/`
* 日志文件目录：`~/.local/share/axiom-v3/logs/`
