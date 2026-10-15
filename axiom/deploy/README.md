# deploy/ —— macOS 开发机路线脚本

本目录是 **macOS 开发机**的部署辅助（宿主机原生运行，非容器）：

- `install-pi-runtime.sh`：Pi Worker 运行时引导（按 `src/axiom/modules/workers/pi_config.py` 版本常量）
- `install-worker-tools.sh` + `tools-versions.env`：Worker 安全工具链（版本对齐生产 Mac 实测）
- `start.sh` / `restart.sh`：launchd/nohup 启停

**Linux 服务器正式部署体系在仓库根的 `deploy/`**（install.sh 一行安装 + axiomctl 管理 + GHCR 镜像发布），勿混淆。
