from __future__ import annotations

import json
from pathlib import Path
import re
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
const ctx = Object.assign({{}}, methods, {{
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
}});
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


def _run_global_search_race_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    core = ROOT / "src/axiom/static/js/core.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
const pending = [];
global.fetch = (url, options = {{}}) => new Promise((resolve, reject) => {{
  const request = {{ url, signal: options.signal, resolve, reject }};
  pending.push(request);
  options.signal?.addEventListener('abort', () => {{
    const error = new Error('aborted');
    error.name = 'AbortError';
    reject(error);
  }}, {{ once: true }});
}});
eval(fs.readFileSync({json.dumps(str(core))}, 'utf8'));
const ctx = Object.assign({{
  globalSearchQuery: 'old',
  globalSearchVulnerabilities: [],
  globalSearchLoading: false,
  showGlobalSearch: true,
  _globalSearchGeneration: 0,
  _globalSearchAbortController: null,
}}, window.AxiomModules.core.methods);
(async () => {{
  const first = ctx.searchGlobalVulnerabilities('old');
  ctx.globalSearchQuery = 'new';
  const second = ctx.searchGlobalVulnerabilities('new');
  pending[1].resolve({{ ok: true, json: async () => [{{ id: 'v-new', title: 'new' }}] }});
  await Promise.all([first, second]);
  const beforeClose = {{
    firstAborted: pending[0].signal.aborted,
    ids: ctx.globalSearchVulnerabilities.map(item => item.id),
    loading: ctx.globalSearchLoading,
  }};
  ctx.closeGlobalSearch();
  process.stdout.write(JSON.stringify({{
    beforeClose,
    afterClose: {{
      query: ctx.globalSearchQuery,
      resultCount: ctx.globalSearchVulnerabilities.length,
      loading: ctx.globalSearchLoading,
      open: ctx.showGlobalSearch,
    }},
  }}));
}})();
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_skill_editor_frontmatter_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    skills = ROOT / "src/axiom/static/js/skills.js"
    content = """---
name: vuln-report
description: 漏洞上报
---

---
name: vuln-report
description: 漏洞上报
---

# Vulnerability reporting

正文内容
"""
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(skills))}, 'utf8'));
const calls = [];
const listed = [{{ name: 'vuln-report', description: '漏洞上报', content: {json.dumps(content)}, builtin: false, enabled: true }}];
const ctx = Object.assign({{
  skills: listed,
  skillEditor: {{ name: 'vuln-report', description: '漏洞上报', isNew: false, activeFile: '', fileContent: '', fileDirty: false }},
  api: async (method, path, body) => {{
    calls.push({{ method, path, body }});
    if (method === 'GET' && path === '/skills') return listed;
    return listed[0];
  }},
  showToast: () => {{}},
}}, window.AxiomModules.skills.methods);
(async () => {{
  await ctx.openSkillFile('SKILL.md');
  const opened = ctx.skillEditor.fileContent;
  ctx.skillEditor.fileDirty = true;
  await ctx.saveSkillFile();
  process.stdout.write(JSON.stringify({{ opened, calls }}));
}})();
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


def _run_manual_reason_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
const calls = [];
const refreshed = [];
const toasts = [];
let requestCount = 0;
global.window = {{ AxiomModules: {{
  state: {{ state: {{ manualReasonTriggering: false }} }},
  core: {{ methods: {{}} }},
  projects: {{ methods: {{}} }},
  graph: {{ methods: {{}} }},
  files: {{ methods: {{}} }},
}} }};
global.location = {{ origin: 'http://testserver' }};
global.fetch = async (url, init = {{}}) => {{
  calls.push({{ method: init.method || 'GET', url, body: init.body ? JSON.parse(init.body) : null }});
  requestCount += 1;
  if (requestCount === 2) return {{
    status: 409,
    ok: false,
    text: async () => JSON.stringify({{ detail: 'ReasonRun rr000124 已在进行中' }}),
  }};
  return {{
    status: 200,
    ok: true,
    text: async () => JSON.stringify({{ id: 'rr000123', trigger: 'manual' }}),
  }};
}};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
const methods = window.AxiomModules.projects.methods;
const ctx = Object.assign({{
  selectedProjectId: 't_150',
  manualReasonTriggering: false,
  project: {{
    project: {{
      id: 't_150', status: 'active', legacy_readonly: false,
      run_deadline_at: null, reason: null,
    }},
    intents: [{{ id: 'i001', kind: 'bootstrap', status: 'completed', archived: false }}],
  }},
  actorName: () => 'Alice',
  loadProject: async taskId => refreshed.push(`task:${{taskId}}`),
  loadProjects: async () => refreshed.push('tasks'),
  showToast: (message, type = 'success') => toasts.push({{ message, type }}),
}}, methods);
(async () => {{
  const enabled = ctx.canTriggerManualReason();
  await ctx.triggerManualReason();
  const afterSuccess = {{ loading: ctx.manualReasonTriggering, refreshed: [...refreshed], toasts: [...toasts] }};
  ctx.project.project.reason = null;
  await ctx.triggerManualReason();
  const afterConflict = {{ loading: ctx.manualReasonTriggering, refreshed: [...refreshed], toasts: [...toasts] }};
  ctx.project.project.reason = {{ id: 'rr000124' }};
  const activeReason = {{ enabled: ctx.canTriggerManualReason(), reason: ctx.manualReasonDisabledReason() }};
  ctx.project.project.reason = null;
  ctx.project.project.status = 'stopped';
  const stopped = {{ enabled: ctx.canTriggerManualReason(), reason: ctx.manualReasonDisabledReason() }};
  process.stdout.write(JSON.stringify({{ enabled, calls, afterSuccess, afterConflict, activeReason, stopped }}));
}})();
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_artifact_folder_navigation_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }}, core: {{ methods: {{}} }}, projects: {{ methods: {{}} }},
  graph: {{ methods: {{}} }}, files: {{ methods: {{}} }},
}} }};
global.location = {{ origin: 'http://testserver', hash: '#/files' }};
global.fetch = async () => {{ throw new Error('unexpected request'); }};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
const methods = window.AxiomModules.files.methods;
const ctx = Object.assign({{
  artifactCenterSelectedTask: '', artifactCenterFolderKey: '', artifactCenterRouteArtifactId: '',
  artifactCenterSearch: '', artifactCenterType: 'all', artifactCenterPhase: 'all', artifactCenterSource: 'all',
  artifactCenterPage: 0, artifactCenterFolderPage: 0, artifactCenterSelected: null,
  artifactCenterTasks: [{{ id:'t_150', title:'任务 150' }}], artifactCenterItems: [],
  loadArtifactCenter: () => {{}}, showToast: () => {{}},
}}, methods);
ctx.applyArtifactRouteParams(new URLSearchParams('task=t_150&folder=i113&artifact=a686'));
const applied = {{
  task: ctx.artifactCenterSelectedTask,
  folder: ctx.artifactCenterFolderKey,
  artifact: ctx.artifactCenterRouteArtifactId,
  mode: ctx.artifactCenterMode(),
  taskTitle: ctx.artifactCenterCurrentTask().title,
}};
ctx.navigateArtifactCenter('t_150', 'i113', 'a686');
const intentHash = location.hash;
ctx.navigateArtifactCenter('t_150');
const taskHash = location.hash;
ctx.artifactCenterFolderKey = 'i113';
const displayPath = ctx.artifactDisplayPath({{ id:'a686', path:'i113/evidence/request.txt' }});
ctx.artifactCenterSelected = {{ id:'a686' }};
const selected = ctx.isArtifactCenterSelected({{ id:'a686' }});
process.stdout.write(JSON.stringify({{ applied, intentHash, taskHash, displayPath, selected }}));
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_task_run_window_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    projects = ROOT / "src/axiom/static/js/projects.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(projects))}, 'utf8'));
const calls = [];
const ctx = Object.assign({{}}, window.AxiomModules.projects.methods, {{
  runClockNow: Date.parse('2026-07-28T10:00:00Z'),
  newProject: {{ run_time_limited: true, run_hours: 2, run_minutes: 30 }},
  resumeTaskDialog: {{ open: true, taskId: 't_150', title: '测试', limited: true, hours: 1, minutes: 15, submitting: false }},
  formatDate: value => value,
  setProjectStatus: async (taskId, status, options) => calls.push({{ taskId, status, options }}),
  showToast: () => {{}},
}});
(async () => {{
  const timedTask = {{ status: 'active', run_deadline_at: '2026-07-28T11:00:00Z' }};
  const unlimitedTask = {{ status: 'active', run_deadline_at: null }};
  const pausedTask = {{ status: 'stopped', run_deadline_at: '2026-07-28T10:00:00Z', last_pause_reason: 'run_deadline_reached' }};
  const values = {{
    createSeconds: ctx.newProjectRunDurationSeconds(),
    createValid: ctx.newProjectRunDurationValid(),
    timedLabel: ctx.taskRunWindowLabel(timedTask),
    unlimitedLabel: ctx.taskRunWindowLabel(unlimitedTask),
    pausedLabel: ctx.taskRunWindowLabel(pausedTask),
    invalid: Number.isNaN(ctx.runDurationSeconds(true, 0, 0)),
  }};
  await ctx.confirmResumeTask();
  process.stdout.write(JSON.stringify({{ values, calls, dialog: ctx.resumeTaskDialog }}));
}})();
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_task_run_window_adapter_fixture() -> list[dict[str, object]]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
const calls = [];
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }}, core: {{ methods: {{}} }}, projects: {{ methods: {{}} }},
  graph: {{ methods: {{}} }}, files: {{ methods: {{}} }},
}} }};
global.location = {{ origin: 'http://testserver' }};
global.fetch = async (url, init = {{}}) => {{
  const body = init.body ? JSON.parse(init.body) : null;
  calls.push({{ method: init.method || 'GET', url, body }});
  const task = {{
    id:'t_150', project_id:'prj_1', title:'测试', origin:{{host:'a.example.com',original_input:'a.example.com'}},
    goal:'测试', status:'running', phase:'bootstrap', scope:{{enabled:true,targets:['a.example.com'],exclude:[]}},
    worker_pool:[], guard_enabled:false, external_report_enabled:false, legacy_readonly:false,
    created_at:'2026-07-28T00:00:00Z', updated_at:'2026-07-28T00:00:00Z',
    run_duration_seconds:7200, run_started_at:'2026-07-28T00:00:00Z', run_deadline_at:'2026-07-28T02:00:00Z', last_pause_reason:null,
  }};
  return {{ status: 200, ok: true, text: async () => JSON.stringify(task) }};
}};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
(async () => {{
  await window.AxiomModules.core.methods.api('POST', '/projects', {{
    parent_project_id:'prj_1', title:'测试', origin:'a.example.com', goal:'测试',
    run_duration_seconds:7200,
  }});
  await window.AxiomModules.core.methods.api('PUT', '/projects/t_150/status', {{
    status:'active', run_duration_seconds:4500,
  }});
  process.stdout.write(JSON.stringify(calls));
}})();
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_graph_visual_update_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    graph = ROOT / "src/axiom/static/js/graph.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(graph))}, 'utf8'));
const methods = window.AxiomModules.graph.methods;
const element = (data, kind) => ({{
  length: 1,
  _data: {{ ...data }},
  data(keyOrObject) {{
    if (typeof keyOrObject === 'string') return this._data[keyOrObject];
    this._data = {{ ...this._data, ...keyOrObject }};
  }},
  id() {{ return this._data.id; }},
  remove() {{}},
  source() {{ return {{ id: () => this._data.source }}; }},
  target() {{ return {{ id: () => this._data.target }}; }},
}});
const nodeElement = element({{ id:'f001', label:'旧状态', width:120, height:54, nodeType:'fact' }}, 'node');
const edgeElement = element({{ id:'e001', source:'f001', target:'goal', status:'unclaimed', label:'' }}, 'edge');
let layouts = 0;
let decorations = 0;
const ctx = Object.assign({{}}, methods, {{
  project: {{}},
  cy: {{
    nodes: () => [nodeElement],
    edges: () => [edgeElement],
    getElementById: id => id === 'f001' ? nodeElement : edgeElement,
    add: () => {{ throw new Error('unexpected add'); }},
    layout: () => ({{ run: () => {{ layouts += 1; }} }}),
  }},
  buildElements: () => ({{
    nodes: [{{ data: {{ id:'f001', label:'新状态', width:120, height:54, nodeType:'fact' }} }}],
    edges: [{{ data: {{ id:'e001', source:'f001', target:'goal', status:'in_progress', label:'' }} }}],
  }}),
  snapshotNodePositions: () => new Map(),
  pulseActive: () => {{}},
  refreshGraphDecorations: () => {{ decorations += 1; }},
}});
ctx.updateGraph();
process.stdout.write(JSON.stringify({{
  layouts,
  decorations,
  nodeLabel: nodeElement.data('label'),
  edgeStatus: edgeElement.data('status'),
}}));
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
    for script in [
        "state", "projects", "skills", "v2-adapter", "helpers", "graph", "human", "timeline",
    ]:
        assert re.search(
            rf'/static/js/{re.escape(script)}\.js\?v=[a-zA-Z0-9._-]+',
            response.text,
        )
    assert "/static/tailwind.generated.css" in response.text
    assert "/static/vendor/tailwindcss.js" not in response.text
    assert 'id="page-host"' in response.text
    assert response.text.count('data-page=') == 0
    assert 'class="nav-label">仪表盘' in response.text
    assert 'class="nav-label">设置' in response.text
    assert "localStorage" not in response.text

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
    assert "triggerManualReason()" in graph.text
    assert "manual-reason-trigger" in graph.text

    human = client.get("/static/templates/human-view.html")
    assert human.status_code == 200
    assert 'x-if="humanTab === \'workspace\'"' in human.text
    assert 'x-if="humanTab === \'artifacts\'"' in human.text
    assert "humanArtifactPageCount" in human.text
    assert "triggerManualReason()" in human.text
    assert "manual-reason-trigger" in human.text

    archive = client.get("/static/templates/archive-view.html")
    assert archive.status_code == 200
    assert "triggerManualReason()" in archive.text
    assert "manual-reason-trigger" in archive.text

    core = client.get("/static/js/core.js").text
    projects = client.get("/static/js/projects.js").text
    graph_js = client.get("/static/js/graph.js").text
    adapter = client.get("/static/js/v2-adapter.js").text
    assert "Alpine.destroyTree(host)" in core
    assert "localStorage.setItem('axiom.sidebarCollapsed'" in core
    assert "setInterval(async" not in projects
    assert "loadDispatchSettings()," not in projects[projects.index("startPolling()"):]
    assert "topologyChanged || geometryChanged" in graph_js
    assert "pan zoom layoutstop add remove data" not in graph_js
    assert "source.map(async task" not in adapter
    assert "if (fromRoute) this.mountCurrentPage()" not in projects
    assert "this.navigateTo('tasks'); this.openProject" not in core
    assert "_globalSearchAbortController?.abort()" in core
    assert "`/audit-events?task_id=${encodeURIComponent(taskId)}&limit=100`" not in adapter
    assert "const offset = append ? currentLength : 0" in adapter
    assert "kind: intent.kind || ''" in adapter
    assert "`/tasks/${taskId}/reason-runs/trigger`" in adapter

    css = client.get("/static/apple.css").text
    assert "@media (prefers-color-scheme: dark)" not in css
    assert "#cy {\n  background: #f8fafc" not in css
    assert "background-color: var(--surface-graph)" in css
    assert "button.manual-reason-trigger" in css
    assert "applyGraphTheme()" in graph_js
    assert "edgeLabelBackground" in graph_js
    assert "this.$nextTick(() => this.applyGraphTheme?.())" in core

    for template_name in (
        "dashboard",
        "task-list",
        "vulnerabilities",
        "project-mgmt",
        "guard-audit",
        "skills",
        "files",
        "mcp",
        "prompts",
        "settings",
        "crawler",
        "assets",
        "action-replay",
        "human-view",
        "graph-view",
        "archive-view",
    ):
        template = client.get(f"/static/templates/{template_name}.html")
        assert template.status_code == 200
        assert "app-page-header" in template.text, template_name

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


def test_manual_reason_button_uses_shared_trigger_and_disables_invalid_states() -> None:
    result = _run_manual_reason_fixture()

    assert result["enabled"] is True
    assert result["calls"] == [
        {
            "method": "POST",
            "url": "/api/v1/tasks/t_150/reason-runs/trigger",
            "body": {"actor": "Alice"},
        },
        {
            "method": "POST",
            "url": "/api/v1/tasks/t_150/reason-runs/trigger",
            "body": {"actor": "Alice"},
        },
    ]
    assert result["afterSuccess"] == {
        "loading": False,
        "refreshed": ["task:t_150", "tasks"],
        "toasts": [
            {"message": "已触发 rr000123，等待 Reason Worker 领取", "type": "success"}
        ],
    }
    assert result["afterConflict"] == {
        "loading": False,
        "refreshed": ["task:t_150", "tasks", "task:t_150"],
        "toasts": [
            {"message": "已触发 rr000123，等待 Reason Worker 领取", "type": "success"},
            {"message": "ReasonRun rr000124 已在进行中", "type": "error"},
        ],
    }
    assert result["activeReason"] == {
        "enabled": False,
        "reason": "rr000124 已在进行中",
    }
    assert result["stopped"] == {
        "enabled": False,
        "reason": "只有运行中的任务可以触发 Reason",
    }


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


def test_global_search_aborts_stale_requests_and_only_keeps_latest_results() -> None:
    assert _run_global_search_race_fixture() == {
        "beforeClose": {
            "firstAborted": True,
            "ids": ["v-new"],
            "loading": False,
        },
        "afterClose": {
            "query": "",
            "resultCount": 0,
            "loading": False,
            "open": False,
        },
    }


def test_skill_editor_hides_frontmatter_and_saves_body_only() -> None:
    result = _run_skill_editor_frontmatter_fixture()

    assert result["opened"] == "# Vulnerability reporting\n\n正文内容\n"
    assert result["calls"][0] == {
        "method": "PUT",
        "path": "/skills/vuln-report",
        "body": {
            "description": "漏洞上报",
            "content": "# Vulnerability reporting\n\n正文内容\n",
        },
    }


def test_artifact_center_classifies_new_explore_paths_by_type() -> None:
    assert _run_artifact_classification_fixture() == [
        {"phase": "explore", "label": "Findings", "icon": "!"},
        {"phase": "explore", "label": "Leads", "icon": "↗"},
        {"phase": "explore", "label": "Evidence", "icon": "◈"},
    ]


def test_artifact_center_routes_task_folder_and_selected_file() -> None:
    assert _run_artifact_folder_navigation_fixture() == {
        "applied": {
            "task": "t_150",
            "folder": "i113",
            "artifact": "a686",
            "mode": "folder",
            "taskTitle": "任务 150",
        },
        "intentHash": "#/files?task=t_150&folder=i113&artifact=a686",
        "taskHash": "#/files?task=t_150",
        "displayPath": "evidence/request.txt",
        "selected": True,
    }


def test_task_run_window_helpers_and_shared_resume_dialog() -> None:
    result = _run_task_run_window_fixture()

    assert result["values"] == {
        "createSeconds": 9_000,
        "createValid": True,
        "timedLabel": "剩余 01:00:00",
        "unlimitedLabel": "不限时运行",
        "pausedLabel": "已按计划自动暂停",
        "invalid": True,
    }
    assert result["calls"] == [
        {
            "taskId": "t_150",
            "status": "active",
            "options": {"runDurationSeconds": 4_500},
        }
    ]
    assert result["dialog"]["open"] is False


def test_task_run_window_adapter_forwards_create_and_resume_duration() -> None:
    calls = _run_task_run_window_adapter_fixture()

    assert calls[0]["url"] == "/api/v1/tasks"
    assert calls[0]["body"]["run_duration_seconds"] == 7_200
    assert calls[1] == {
        "method": "POST",
        "url": "/api/v1/tasks/t_150/resume",
        "body": {"run_duration_seconds": 4_500},
    }


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


def test_graph_visual_updates_do_not_trigger_layout() -> None:
    result = _run_graph_visual_update_fixture()

    assert result == {
        "layouts": 0,
        "decorations": 1,
        "nodeLabel": "新状态",
        "edgeStatus": "in_progress",
    }
