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


def _run_artifact_classification_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }},
  core: {{ methods: {{}} }},
  projects: {{ methods: {{}} }},
  graph: {{ methods: {{}} }},
  files: {{ methods: {{}} }},
}} }};
global.location = {{ origin: 'http://testserver' }};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
const methods = window.AxiomModules.files.methods;
const items = [
  {{ path:'i002/findings.md', artifact_type:'explore.findings' }},
  {{ path:'i002/leads.md', artifact_type:'explore.leads' }},
  {{ path:'i002/evidence/response.txt', artifact_type:'explore.evidence' }},
];
process.stdout.write(JSON.stringify(items.map(item => ({{
  phase: methods.artifactSourcePhase(item),
  label: methods.artifactKindLabel(item),
  icon: methods.artifactTypeIcon(item.artifact_type),
}}))));
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_artifact_preview_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
const calls = [];
const payloads = {{
  '/api/v1/tasks/t_148/artifacts/a_txt/content': '第一行\\n<script>alert(1)</script>',
  '/api/v1/tasks/t_148/artifacts/a_json/content': '{{"name":"测试","items":[1,2]}}',
  '/api/v1/tasks/t_148/artifacts/a_array/content': '[{{"id":1}},{{"id":2}}]',
  '/api/v1/tasks/t_148/artifacts/a_invalid/content': '{{"broken":',
  '/api/v1/tasks/t_148/artifacts/a_html/content': '<img src=x onerror=alert(1)>',
  '/api/v1/tasks/t_148/artifacts/a_empty/content': '',
  '/api/v1/tasks/t_148/artifacts/a_md/content': '# 标题',
}};
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }},
  core: {{ methods: {{}} }},
  projects: {{ methods: {{}} }},
  graph: {{ methods: {{}} }},
  files: {{ methods: {{}} }},
}} }};
global.location = {{ origin: 'http://testserver' }};
global.marked = {{ parse: content => `<h1>${{content.replace(/^# /, '')}}</h1>` }};
global.fetch = async (url, init = {{}}) => {{
  calls.push({{ method: init.method || 'GET', url }});
  if (!(url in payloads)) throw new Error(`unexpected request: ${{url}}`);
  return {{
    status: 200,
    ok: true,
    text: async () => JSON.stringify({{ artifact: {{}}, content: payloads[url] }}),
  }};
}};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
const methods = window.AxiomModules.files.methods;
const ctx = Object.assign({{
  artifactCenterSelected: null,
  isImageFile: name => /\\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i.test(name),
  isMdFile: name => /\\.md$/i.test(name),
}}, methods);
let selectedState = null;
Object.defineProperty(ctx, 'artifactCenterSelected', {{
  configurable: true,
  get: () => selectedState,
  set: value => {{ selectedState = value === null ? null : new Proxy(value, {{}}); }},
}});
const item = (id, path, size = 10) => ({{ id, task_id:'t_148', path, size }});
const capture = selected => ({{
  kind: selected._previewKind,
  loading: selected._loading,
  content: selected._content,
  rendered: selected._rendered,
  warning: selected._warning,
  notice: selected._notice,
  error: selected._error,
}});
(async () => {{
  const result = {{}};
  await ctx.openArtifactCenter(item('a_txt', 'i014/evidence/result.txt'));
  result.txt = capture(ctx.artifactCenterSelected);
  await ctx.openArtifactCenter(item('a_json', 'i014/evidence/result.json'));
  result.json = capture(ctx.artifactCenterSelected);
  await ctx.openArtifactCenter(item('a_array', 'i014/evidence/result-array.json'));
  result.array = capture(ctx.artifactCenterSelected);
  await ctx.openArtifactCenter(item('a_invalid', 'i014/evidence/invalid.json'));
  result.invalid = capture(ctx.artifactCenterSelected);
  await ctx.openArtifactCenter(item('a_html', 'i014/evidence/poc.html'));
  result.html = capture(ctx.artifactCenterSelected);
  await ctx.openArtifactCenter(item('a_empty', 'i014/evidence/empty.txt', 0));
  result.empty = capture(ctx.artifactCenterSelected);
  await ctx.openArtifactCenter(item('a_md', 'i014/findings.md'));
  result.markdown = capture(ctx.artifactCenterSelected);
  await ctx.openArtifactCenter(item('a_large', 'i014/evidence/large.txt', 5 * 1024 * 1024 + 1));
  result.large = capture(ctx.artifactCenterSelected);
  await ctx.openArtifactCenter(item('a_binary', 'i014/evidence/cache.pyc'));
  result.binary = capture(ctx.artifactCenterSelected);
  await ctx.openArtifactCenter(item('a_image', 'i014/evidence/proof.png'));
  result.image = capture(ctx.artifactCenterSelected);
  result.calls = calls;
  result.downloadUrl = ctx.artifactDownloadUrl(item('a_txt', 'result.txt'));
  process.stdout.write(JSON.stringify(result));
}})();
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_credential_editor_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
const calls = [];
const loaded = [];
const toasts = [];
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }},
  core: {{ methods: {{}} }},
  projects: {{ methods: {{}} }},
  graph: {{ methods: {{}} }},
  files: {{ methods: {{}} }},
}} }};
global.location = {{ origin: 'http://testserver' }};
global.fetch = async (url, init = {{}}) => {{
  const method = init.method || 'GET';
  const body = init.body ? JSON.parse(init.body) : null;
  calls.push({{ method, url, body }});
  let payload;
  if (method === 'GET' && url === '/api/v1/projects/prj_001/credentials/cred_001') {{
    payload = {{ id:'cred_001', project_id:'prj_001', name:'会话', content:'Cookie: old-secret', notes:'低权限', refresh_id:'crf_001', enabled:true }};
  }} else if (method === 'GET' && url === '/api/v1/projects/prj_001/credential-refreshes/crf_001') {{
    payload = {{ id:'crf_001', credential_id:'cred_001', content:'重新登录', created_at:'now', updated_at:'now' }};
  }} else if (method === 'PUT' && url === '/api/v1/projects/prj_001/credentials/cred_001') {{
    payload = {{ id:'cred_001', project_id:'prj_001', ...body, refresh_id:'crf_001', created_at:'now', updated_at:'now' }};
  }} else if (method === 'PATCH' && url === '/api/v1/projects/prj_001/credentials/cred_001/enabled') {{
    payload = {{ id:'cred_001', project_id:'prj_001', name:'会话', content:'Cookie: new-secret', notes:'低权限', refresh_id:'crf_001', enabled:false, created_at:'now', updated_at:'now' }};
  }} else {{
    throw new Error(`unexpected request: ${{method}} ${{url}}`);
  }}
  return {{ status: 200, ok: true, text: async () => JSON.stringify(payload) }};
}};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
const ctx = Object.assign({{
  selectedProjectManagementId: 'prj_001',
  projectCredentials: [],
  projectCredentialSearch: '',
  credentialEditor: {{}},
  credentialSaving: false,
  showToast: (message, type = 'success') => toasts.push({{ message, type }}),
}}, window.AxiomModules.projects.methods);
ctx.loadProjectCredentials = async projectId => loaded.push(projectId);
(async () => {{
  await ctx.openProjectCredentialEditor({{ id:'cred_001', project_id:'prj_001', name:'会话', notes:'低权限', refresh_id:'crf_001', enabled:true }});
  const opened = {{ ...ctx.credentialEditor }};
  ctx.credentialEditor.content = 'Cookie: new-secret';
  ctx.credentialEditor.refreshContent = 'POST /login\\n\\nusername=a&password=b';
  await ctx.saveProjectCredential();
  await ctx.toggleProjectCredential({{ id:'cred_001', project_id:'prj_001', enabled:true }});
  process.stdout.write(JSON.stringify({{ opened, calls, loaded, toasts }}));
}})();
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
    assert "/static/js/state.js?v=20260727.3" in response.text
    assert "/static/js/skills.js?v=20260727.4" in response.text
    assert "/static/js/v2-adapter.js?v=20260728.2" in response.text
    assert "/static/js/helpers.js?v=20260724.2" in response.text
    assert "/static/js/graph.js?v=20260727.3" in response.text
    assert "/static/js/human.js?v=20260727.1" in response.text
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

    vulnerabilities = client.get("/static/templates/vulnerabilities.html")
    assert vulnerabilities.status_code == 200
    assert 'x-model="vulnEditor.reproduction_steps"' not in vulnerabilities.text
    assert 'x-model="vulnEditor.curl_command"' not in vulnerabilities.text
    assert 'x-model="vulnEditor.evidence"' not in vulnerabilities.text
    assert "finding_artifact" in vulnerabilities.text
    assert "evidence_artifacts" in vulnerabilities.text

    files = client.get("/static/templates/files.html")
    assert files.status_code == 200
    assert "artifactKindLabel" in files.text
    assert "explore.findings" in files.text
    assert "explore.leads" in files.text
    assert "explore.evidence" in files.text
    assert 'data-testid="artifact-preview-text"' in files.text
    assert '<template x-if="!artifactCenterSelected._isImage"><template' not in files.text
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

    credential_template = client.get("/static/templates/project-mgmt.html")
    assert credential_template.status_code == 200
    assert "凭证如何刷新" in credential_template.text
    assert "格式不限" in credential_template.text
    assert "凭证内容（JSON 对象）" not in credential_template.text
    assert "适用域名（每行一个" not in credential_template.text


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


def test_artifact_center_classifies_new_explore_paths_by_type() -> None:
    assert _run_artifact_classification_fixture() == [
        {"phase": "explore", "label": "Findings", "icon": "!"},
        {"phase": "explore", "label": "Leads", "icon": "↗"},
        {"phase": "explore", "label": "Evidence", "icon": "◈"},
    ]


def test_artifact_center_previews_text_and_formats_json_safely() -> None:
    result = _run_artifact_preview_fixture()

    assert result["txt"] == {
        "kind": "text",
        "loading": False,
        "content": "第一行\n<script>alert(1)</script>",
        "rendered": "",
        "warning": "",
        "notice": "",
        "error": "",
    }
    assert result["json"]["kind"] == "json"
    assert result["json"]["content"] == '{\n  "name": "测试",\n  "items": [\n    1,\n    2\n  ]\n}'
    assert result["array"]["content"] == '[\n  {\n    "id": 1\n  },\n  {\n    "id": 2\n  }\n]'
    assert result["invalid"]["content"] == '{"broken":'
    assert result["invalid"]["warning"] == "JSON 格式无效，已按原文显示。"
    assert result["html"]["kind"] == "text"
    assert result["html"]["content"] == "<img src=x onerror=alert(1)>"
    assert result["empty"]["content"] == ""
    assert result["empty"]["loading"] is False
    assert result["markdown"]["kind"] == "markdown"
    assert result["markdown"]["rendered"] == "<h1>标题</h1>"
    assert result["large"]["kind"] == "text"
    assert "在线预览上限为 5 MB" in result["large"]["notice"]
    assert result["binary"]["kind"] == "unsupported"
    assert result["binary"]["notice"] == "该文件类型不支持在线预览，请下载查看。"
    assert result["image"]["kind"] == "image"
    assert result["downloadUrl"] == "/api/v1/tasks/t_148/artifacts/a_txt/download"
    assert [call["url"] for call in result["calls"]] == [
        "/api/v1/tasks/t_148/artifacts/a_txt/content",
        "/api/v1/tasks/t_148/artifacts/a_json/content",
        "/api/v1/tasks/t_148/artifacts/a_array/content",
        "/api/v1/tasks/t_148/artifacts/a_invalid/content",
        "/api/v1/tasks/t_148/artifacts/a_html/content",
        "/api/v1/tasks/t_148/artifacts/a_empty/content",
        "/api/v1/tasks/t_148/artifacts/a_md/content",
    ]


def test_credential_editor_progressively_loads_and_uses_free_text_protocol() -> None:
    result = _run_credential_editor_fixture()

    assert result["opened"] == {
        "open": True,
        "loading": False,
        "id": "cred_001",
        "projectId": "prj_001",
        "name": "会话",
        "content": "Cookie: old-secret",
        "notes": "低权限",
        "refreshContent": "重新登录",
        "enabled": True,
    }
    assert [(call["method"], call["url"]) for call in result["calls"]] == [
        ("GET", "/api/v1/projects/prj_001/credentials/cred_001"),
        ("GET", "/api/v1/projects/prj_001/credential-refreshes/crf_001"),
        ("PUT", "/api/v1/projects/prj_001/credentials/cred_001"),
        ("PATCH", "/api/v1/projects/prj_001/credentials/cred_001/enabled"),
    ]
    assert result["calls"][2]["body"] == {
        "name": "会话",
        "content": "Cookie: new-secret",
        "notes": "低权限",
        "refresh_content": "POST /login\n\nusername=a&password=b",
        "enabled": True,
    }
    assert result["calls"][3]["body"] == {"enabled": False}
    assert result["loaded"] == ["prj_001", "prj_001"]


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
