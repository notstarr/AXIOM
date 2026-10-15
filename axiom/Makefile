# AXIOM 一键部署 / 升级（容器化路线，配合 compose.deploy.yaml）
#
# 常用：
#   make up          首次部署（本地构建镜像，约 10-20 分钟）
#   make pull-up     新设备最快路径：直接拉 GHCR 预构建镜像（不构建）
#   make upgrade     日常升级 = git pull + 换最新镜像（秒级切换，数据卷不受影响）
#   make health      健康检查
#   make logs        跟踪 API 日志
#   make down        停止（数据卷保留）

COMPOSE = docker compose -f compose.deploy.yaml
GHCR_IMAGE = ghcr.io/notstarr/axiom:feat-reason-queue-050a20a
PORT ?= 18888

.PHONY: up pull-up upgrade health logs down

up:
	$(COMPOSE) up -d --build
	@$(MAKE) health

pull-up:
	AXIOM_APP_IMAGE=$(GHCR_IMAGE) $(COMPOSE) pull app
	AXIOM_APP_IMAGE=$(GHCR_IMAGE) $(COMPOSE) up -d
	@$(MAKE) health

upgrade:
	git pull
	$(MAKE) pull-up

health:
	@curl -fsS --max-time 5 http://127.0.0.1:$(PORT)/health && echo

logs:
	$(COMPOSE) logs -f app

down:
	$(COMPOSE) down
