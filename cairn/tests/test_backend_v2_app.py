from __future__ import annotations

from fastapi.testclient import TestClient

from axiom.infrastructure.database.engine import create_sqlite_engine
from axiom.infrastructure.database.metadata import metadata
from axiom.server.v2_app import create_v2_app


def test_v2_app_exposes_only_versioned_business_api(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "axiom-v2.db")
    metadata.create_all(engine)
    app = create_v2_app(
        engine=engine,
        data_dir=tmp_path / "data",
        service_token="service-token",
    )

    with TestClient(app) as client:
        html = client.get("/").text
        assert "cairnApp" in html
        assert "/static/js/v2-adapter.js" in html
        assert client.get("/static/js/app.js").status_code == 200
        assert client.get("/static/templates/graph-view.html").status_code == 200
        task_list_html = client.get("/static/templates/task-list.html").text
        assert "任务诊断" in task_list_html
        assert "Token 消耗排行榜" in task_list_html
        assert "第一步：基础信息" in task_list_html
        assert "第二步：其他设置" in task_list_html
        assert "taskComposerStep === 1" in task_list_html
        assert 'aria-label="前往第二步：其他设置"' in task_list_html
        assert ':disabled="!String(newProject.title' not in task_list_html
        assert "默认选择全部启用的 Worker" in task_list_html
        assert "Bootstrap 榜" not in task_list_html  # labels are supplied by the established JS helpers
        assert "activeDiagnosticPhase: 'bootstrap'" in task_list_html
        dashboard_html = client.get("/static/templates/dashboard.html").text
        assert "Skill 调用统计" in dashboard_html
        settings_html = client.get("/static/templates/settings.html").text
        assert "judgeSettings.system_prompt" in settings_html
        assert "resetJudgePrompt()" in settings_html
        assert "bootstrap" in settings_html.lower()
        assert "asm" not in settings_html.lower()
        assert "Explore 探索预算（秒）" not in settings_html
        assert "Explore 强制收口预算（秒）" not in settings_html
        assert "每个 Explore Intent 的探索和收口时间由 Reason" in settings_html
        state_js = client.get("/static/js/state.js").text
        assert "对该系统进行渗透测试，并挖掘10个高置信度的漏洞" in state_js
        projects_js = client.get("/static/js/projects.js").text
        assert ".filter(w => w && w.name && !w.disabled)" in projects_js
        assert "this.taskComposerStep = 1" in projects_js
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/api/v1/projects").status_code == 200
        assert client.get("/projects").status_code == 404
        paths = client.get("/openapi.json").json()["paths"]
        assert "/api/v1/tasks" in paths
        assert all(path == "/health" or path.startswith("/api/v1/") for path in paths)
