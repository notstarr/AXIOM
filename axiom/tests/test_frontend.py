from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_intent_lifecycle_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    scripts = [
        ROOT / "src/axiom/static/js/helpers.js",
        ROOT / "src/axiom/static/js/graph.js",
    ]
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
for (const script of {json.dumps([str(path) for path in scripts])}) {{
  eval(fs.readFileSync(script, 'utf8'));
}}
const methods = Object.assign(
  {{}},
  window.AxiomModules.helpers.methods,
  window.AxiomModules.graph.methods,
);
const intents = {{
  completed: {{ id:'i002', from:['f001'], to:null, description:'reason', status:'completed', worker:'worker-a', concluded_at:'2026-07-24T00:00:00Z' }},
  cancelled: {{ id:'i003', from:['f001'], to:null, description:'cancelled', status:'cancelled', worker:'worker-a' }},
  failed: {{ id:'i004', from:['f001'], to:null, description:'failed', status:'failed', worker:'worker-a' }},
  pending_with_worker: {{ id:'i005', from:['f001'], to:null, description:'pending', status:'pending', worker:'worker-a' }},
  running_without_worker: {{ id:'i006', from:['f001'], to:null, description:'running', status:'running', worker:null }},
  legacy_worker: {{ id:'i007', from:['f001'], to:null, description:'legacy', worker:'worker-a' }},
}};
const ctx = Object.assign({{
  project: {{
    project: {{ depth_mode:false }},
    goal_from_fact_ids: ['f001'],
    facts: [
      {{ id:'origin', description:'origin' }},
      {{ id:'f001', description:'bootstrap complete' }},
      {{ id:'goal', description:'goal' }},
    ],
    intents: Object.values(intents),
  }},
}}, methods);
const rows = Object.fromEntries(Object.entries(intents).map(([name, intent]) => [name, {{
  state: ctx.intentLifecycleState(intent),
  type: ctx.openIntentNodeType(intent),
  label: ctx.intentStatusLabel(intent),
}}]));
const elements = ctx.buildElements();
rows.completed.graphNode = elements.nodes.find(node => node.data.intentId === 'i002')?.data || null;
rows.completed.graphEdge = elements.edges.find(edge => edge.data.intentId === 'i002')?.data || null;
rows.__goalEdge = elements.edges.find(edge => edge.data.edgeType === 'goal_evidence')?.data || null;
process.stdout.write(JSON.stringify(rows));
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_runtime_save_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
const calls = [];
const current = {{
  version: 5,
  updated_at: '2026-07-27T00:00:00Z',
  applied_version: 5,
  applied_at: '2026-07-27T00:00:01Z',
  config: {{
    reason_low_watermark: 1,
    network_proxy_mode: 'off',
    network_proxy_url: 'http://127.0.0.1:7890',
  }},
}};
const saved = {{
  ...current,
  version: 6,
  updated_at: '2026-07-27T00:00:02Z',
  config: {{ ...current.config, reason_low_watermark: 0 }},
}};
const workers = [{{ name: 'worker-a', capabilities: ['reason'], max_running: 1, priority: 0, config: {{}}, disabled: false }}];
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }},
  core: {{ methods: {{}} }},
  projects: {{ methods: {{}} }},
  graph: {{ methods: {{}} }},
  files: {{ methods: {{}} }},
}} }};
global.location = {{ origin: 'http://testserver' }};
global.fetch = async (url, init = {{}}) => {{
  calls.push({{ method: init.method || 'GET', url, body: init.body ? JSON.parse(init.body) : null }});
  let payload;
  if (url === '/api/v1/settings/runtime' && (init.method || 'GET') === 'GET') payload = current;
  else if (url === '/api/v1/settings/runtime' && init.method === 'PUT') payload = saved;
  else if (url === '/api/v1/workers' && (init.method || 'GET') === 'GET') payload = workers;
  else throw new Error(`unexpected request: ${{init.method || 'GET'}} ${{url}}`);
  return {{ status: 200, ok: true, text: async () => JSON.stringify(payload) }};
}};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
window.AxiomModules.core.methods.api('PUT', '/settings/dispatch', {{
  config_version: 5,
  reason_low_watermark: 0,
}}).then(result => process.stdout.write(JSON.stringify({{ result, calls }})));
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_skill_toggle_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    skills = ROOT / "src/axiom/static/js/skills.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(skills))}, 'utf8'));
const calls = [];
const toasts = [];
const ctx = Object.assign({{
  skills: [{{ name: 'browser-control', enabled: true }}],
  skillEditor: {{ name: 'browser-control', enabled: true }},
  skillToggleLoading: '',
  api: async (method, path, body) => {{
    calls.push({{ method, path, body }});
    return {{ name: 'browser-control', enabled: false }};
  }},
  showToast: (message, type = 'success') => toasts.push({{ message, type }}),
}}, window.AxiomModules.skills.methods);
ctx.toggleSkillFromEditor().then(() => process.stdout.write(JSON.stringify({{
  calls,
  toasts,
  listedEnabled: ctx.skills[0].enabled,
  editorEnabled: ctx.skillEditor.enabled,
  loading: ctx.skillToggleLoading,
}})));
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_frontend_and_static_assets_are_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>axiom</title>" in response.text
    assert "/static/js/state.js?v=20260727.1" in response.text
    assert "/static/js/skills.js?v=20260727.4" in response.text
    assert "/static/js/v2-adapter.js?v=20260727.3" in response.text
    assert "/static/js/helpers.js?v=20260724.2" in response.text
    assert "/static/js/graph.js?v=20260727.2" in response.text
    assert "/static/js/human.js?v=20260724.2" in response.text
    assert "/static/js/timeline.js?v=20260724.3" in response.text

    asset = client.get("/static/js/v2-adapter.js")
    assert asset.status_code == 200
    assert "Backend V2 adapter" in asset.text

    state = client.get("/static/js/state.js")
    assert state.status_code == 200
    assert "projectDetailsLoaded: false" in state.text
    assert "projectsLoaded: false" in state.text

    dashboard = client.get("/static/templates/dashboard.html")
    assert dashboard.status_code == 200
    assert "正在加载任务" in dashboard.text
    assert "正在加载事件" in dashboard.text

    settings = client.get("/static/templates/settings.html")
    assert settings.status_code == 200
    for heading in ("调度并发", "阶段预算", "Reason 触发与派发", "高级可靠性"):
        assert heading in settings.text
    assert "Bootstrap 并发" in settings.text
    assert "Reason 并发" in settings.text
    assert settings.text.count("1（固定）") >= 2

    graph = client.get("/static/templates/graph-view.html")
    assert graph.status_code == 200
    assert "ReasonRun" in graph.text
    assert "配置快照" in graph.text

    graph_script = client.get("/static/js/graph.js")
    assert graph_script.status_code == 200
    assert "explore_timeout" not in graph_script.text
    assert "bootstrap_timeout" not in graph_script.text


def test_frontend_fallback_does_not_mask_unknown_api_routes(client):
    assert client.get("/tasks/t_123").status_code == 200

    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_runtime_save_uses_the_put_response_and_does_not_touch_workers() -> None:
    result = _run_runtime_save_fixture()

    assert result["result"]["config_version"] == 6
    assert result["result"]["reason_low_watermark"] == 0
    assert [(call["method"], call["url"]) for call in result["calls"]] == [
        ("GET", "/api/v1/settings/runtime"),
        ("PUT", "/api/v1/settings/runtime"),
        ("GET", "/api/v1/workers"),
    ]
    assert result["calls"][1]["body"]["expected_version"] == 5
    assert result["calls"][1]["body"]["config"]["reason_low_watermark"] == 0


def test_skill_toggle_uses_dedicated_patch_endpoint_and_server_state() -> None:
    result = _run_skill_toggle_fixture()

    assert result["calls"] == [
        {
            "method": "PATCH",
            "path": "/skills/browser-control/enabled",
            "body": {"enabled": False},
        }
    ]
    assert result["listedEnabled"] is False
    assert result["editorEnabled"] is False
    assert result["loading"] == ""
    assert result["toasts"] == [
        {"message": "browser-control 已禁用", "type": "success"}
    ]


def test_intent_lifecycle_uses_status_instead_of_retained_worker():
    rows = _run_intent_lifecycle_fixture()

    assert rows["__goalEdge"] == {
        "id": "_goal_evidence_f001",
        "source": "f001",
        "target": "goal",
        "label": "Goal 达成证据",
        "status": "goal_evidence",
        "edgeType": "goal_evidence",
    }

    assert rows["completed"]["state"] == "completed"
    assert rows["completed"]["type"] == "intent_completed"
    assert rows["completed"]["label"] == "已完成"
    assert rows["completed"]["graphNode"]["label"] == "✓"
    assert rows["completed"]["graphEdge"]["status"] == "intent_completed"

    assert rows["cancelled"] == {
        "state": "cancelled",
        "type": "intent_cancelled",
        "label": "已取消",
    }
    assert rows["failed"] == {
        "state": "failed",
        "type": "intent_failed",
        "label": "执行失败",
    }
    assert rows["pending_with_worker"] == {
        "state": "pending",
        "type": "unclaimed",
        "label": "等待 Worker",
    }
    assert rows["running_without_worker"] == {
        "state": "running",
        "type": "in_progress",
        "label": "进行中",
    }
    assert rows["legacy_worker"] == {
        "state": "running",
        "type": "in_progress",
        "label": "进行中",
    }
