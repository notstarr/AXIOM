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


def _run_goal_history_selection_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    graph = ROOT / "src/axiom/static/js/graph.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(graph))}, 'utf8'));
const methods = window.AxiomModules.graph.methods;
let highlighted = null;
let factSelections = 0;
const ctx = Object.assign({{}}, methods, {{
  project: {{
    project: {{ depth_mode:false }},
    goal_from_fact_ids: [],
    goal_history: [{{
      goal_cycle_id:'gc001', sequence:1, goal:'第一轮目标',
      completed_at:'2026-07-29T02:00:00Z', evidence_fact_ids:['f001','f002'],
    }}],
    goal_cycles: [{{
      id:'gc001', sequence:1, goal:'第一轮目标', status:'completed',
      continuation_reason:'结果需要继续深入', focus:'越权链路', actor:'human.frontend',
      started_at:'2026-07-28T01:00:00Z', completed_at:'2026-07-29T02:00:00Z',
    }}],
    goal_evaluations: [{{
      id:'ge001', goal_cycle_id:'gc001', achieved:true,
      reason:'证据已经证明目标达成', evaluator:'reason-worker',
      evidence_refs:['fact:f001','fact:f002'], created_at:'2026-07-29T02:00:00Z',
    }}],
    facts: [
      {{ id:'origin', description:'origin' }},
      {{ id:'f001', description:'证据一' }},
      {{ id:'f002', description:'证据二' }},
      {{ id:'goal', description:'当前目标' }},
    ],
    intents: [],
  }},
  selectedNode: null,
  selectedFacts: ['f001'],
  selectedTimelineEntryId: 'event-1',
  sideTab: 'detail',
  showGoalHistory: true,
  applyGoalHistoryHighlight: id => {{ highlighted = id; }},
  selectFact: () => {{ factSelections += 1; }},
}});
const data = {{
  id:'_goal_history_gc001', nodeType:'goal_history', goalCycleId:'gc001',
}};
ctx.onNodeTap({{
  target: {{ data: key => data[key] }},
  originalEvent: {{ shiftKey:false }},
}});
const elements = ctx.buildElements();
process.stdout.write(JSON.stringify({{
  selectedNode: ctx.selectedNode,
  selectedFacts: ctx.selectedFacts,
  selectedTimelineEntryId: ctx.selectedTimelineEntryId,
  history: ctx.selectedGoalHistoryRecord(),
  cycle: ctx.selectedGoalCycleRecord(),
  evaluation: ctx.selectedGoalEvaluationRecord(),
  evidenceFactIds: ctx.selectedGoalEvidenceFacts().map(item => item.id),
  highlighted,
  factSelections,
  nodeGoalCycleId: elements.nodes.find(item => item.data.nodeType === 'goal_history')?.data.goalCycleId || null,
}}));
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_fact_reference_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    graph = ROOT / "src/axiom/static/js/graph.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(graph))}, 'utf8'));
const calls = [];
const ctx = Object.assign({{}}, window.AxiomModules.graph.methods, {{
  selectedProjectId: 't_153',
  project: {{ project: {{ id:'t_153' }} }},
  openArtifactLocation: (taskId, artifactId) => calls.push({{ kind:'artifact', taskId, artifactId }}),
  openVulnerabilityLocation: (taskId, vulnerabilityId) => calls.push({{ kind:'vulnerability', taskId, vulnerabilityId }}),
}});
const description = '已确认事实：artifact:a016；待验证线索：artifact:a017；本轮上报漏洞：vulnerability:v001；未知 artifact:x001；<script>alert(1)</script>';
const segments = ctx.factDescriptionSegments(description);
for (const segment of segments.filter(item => item.kind !== 'text')) ctx.openFactReference(segment);
process.stdout.write(JSON.stringify({{
  segments,
  calls,
  hasHtmlSegment: segments.some(item => item.text.includes('<script>')),
}}));
"""
    result = subprocess.run(
        [node, "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _run_vulnerability_deep_link_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    helpers = ROOT / "src/axiom/static/js/helpers.js"
    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }}, core: {{ methods: {{}} }}, projects: {{ methods: {{}} }},
  dashboard: {{ methods: {{}} }}, graph: {{ methods: {{}} }}, files: {{ methods: {{}} }},
}} }};
global.location = {{ origin:'http://testserver', hash:'#/task-detail/t_153' }};
global.window.history = {{ replaceState: (_state, _title, url) => {{ global.location.hash = url; }} }};
const calls = [];
const pageItems = [{{
  id:'v002', task_id:'t_153', title:'另一个漏洞', type:'越权', severity:'low',
  description:'d', prerequisites:'p', impact:'i', remediation:'r',
  status:'待验证', finding_artifact:null, evidence_artifacts:[],
}}];
const target = {{
  id:'v001', task_id:'t_153', title:'目标漏洞', type:'越权', severity:'high',
  description:'d', prerequisites:'需要低权限账号', impact:'i', remediation:'校验对象归属',
  status:'待验证', finding_artifact:null, evidence_artifacts:[], archived:false,
}};
global.fetch = async (url, init = {{}}) => {{
  calls.push({{ url, method:init.method || 'GET' }});
  if (url.startsWith('/api/v1/vulnerabilities?')) return {{
    ok:true, status:200,
    headers:{{ get: name => name === 'X-Total-Count' ? '2' : null }},
    text: async () => JSON.stringify(pageItems),
  }};
  if (url === '/api/v1/tasks/t_153/vulnerabilities/v001') return {{
    ok:true, status:200, text: async () => JSON.stringify(target),
  }};
  throw new Error(`unexpected ${{url}}`);
}};
for (const script of [{json.dumps(str(helpers))}, {json.dumps(str(adapter))}]) eval(fs.readFileSync(script, 'utf8'));
const methods = Object.assign({{}}, window.AxiomModules.helpers.methods, window.AxiomModules.projects.methods);
const ctx = Object.assign({{}}, methods, {{
  vulnFilter: {{ projectId:'', keyword:'' }},
  vulnerabilityPage:0, vulnerabilityPageSize:50, vulnerabilityLoading:false,
  vulnerabilityItems:[], vulnerabilityTotal:0,
  selectedVulnerabilityKey:'', vulnerabilityRouteTaskId:'', vulnerabilityRouteId:'',
  vulnerabilitySelectionBlocked:false,
  _vulnerabilityListRequestId:0, projects:[{{ id:'t_153', title:'任务 153', project_id:'p001' }}], projectGroups:[],
  showToast: message => calls.push({{ toast:message }}),
}});
(async () => {{
  const hash = ctx.vulnerabilityHash('t_153', 'v001');
  ctx.applyVulnerabilityRouteParams(new URLSearchParams('task=t_153&vulnerability=v001'));
  await ctx.loadVulnerabilityPage();
  process.stdout.write(JSON.stringify({{
    hash,
    taskFilter:ctx.vulnFilter.projectId,
    selected:ctx.selectedVulnerabilityKey,
    first:ctx.vulnerabilityItems[0]?.id || null,
    detailFetch:calls.some(item => item.url === '/api/v1/tasks/t_153/vulnerabilities/v001'),
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


def _run_vulnerability_deep_link_error_fixture(*, archived: bool) -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    scripts = [
        ROOT / "src/axiom/static/js/helpers.js",
        ROOT / "src/axiom/static/js/projects.js",
        ROOT / "src/axiom/static/js/v2-adapter.js",
    ]
    archived_js = "true" if archived else "false"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }}, core: {{ methods: {{}} }}, projects: {{ methods: {{}} }},
  dashboard: {{ methods: {{}} }}, graph: {{ methods: {{}} }}, files: {{ methods: {{}} }},
}} }};
global.location = {{ origin:'http://testserver', hash:'#/vulnerabilities?task=t_153&vulnerability=v404' }};
global.window.history = {{ replaceState: (_state, _title, url) => {{ global.location.hash = url; }} }};
const calls = [];
const pageItems = [{{
  id:'v002', task_id:'t_153', title:'不应被误选的漏洞', type:'越权', severity:'low',
  description:'d', prerequisites:'p', impact:'i', remediation:'r',
  status:'待验证', finding_artifact:null, evidence_artifacts:[],
}}];
global.fetch = async (url, init = {{}}) => {{
  calls.push({{ url, method:init.method || 'GET' }});
  if (url.startsWith('/api/v1/vulnerabilities?')) return {{
    ok:true, status:200,
    headers:{{ get: name => name === 'X-Total-Count' ? '1' : null }},
    text: async () => JSON.stringify(pageItems),
  }};
  if (url === '/api/v1/tasks/t_153/vulnerabilities/v404') return {archived_js}
    ? {{
        ok:true, status:200,
        text: async () => JSON.stringify({{ ...pageItems[0], id:'v404', archived:true }}),
      }}
    : {{
        ok:false, status:404,
        text: async () => JSON.stringify({{ detail:'Vulnerability not found: v404' }}),
      }};
  throw new Error(`unexpected ${{url}}`);
}};
for (const script of {json.dumps([str(path) for path in scripts])}) eval(fs.readFileSync(script, 'utf8'));
const methods = Object.assign(
  {{}},
  window.AxiomModules.helpers.methods,
  window.AxiomModules.projects.methods,
);
const ctx = Object.assign({{}}, methods, {{
  vulnFilter: {{ projectId:'', keyword:'' }},
  vulnerabilityPage:0, vulnerabilityPageSize:50, vulnerabilityLoading:false,
  vulnerabilityItems:[], vulnerabilityTotal:0,
  selectedVulnerabilityKey:'', vulnerabilityRouteTaskId:'', vulnerabilityRouteId:'',
  vulnerabilitySelectionBlocked:false,
  _vulnerabilityListRequestId:0, projects:[{{ id:'t_153', title:'任务 153', project_id:'p001' }}], projectGroups:[],
  showToast: (message, type) => calls.push({{ toast:message, type }}),
}});
ctx.filteredVulnerabilities = () => ctx.vulnerabilityItems;
(async () => {{
  ctx.applyVulnerabilityRouteParams(new URLSearchParams('task=t_153&vulnerability=v404'));
  await ctx.loadVulnerabilityPage();
  process.stdout.write(JSON.stringify({{
    hash:global.location.hash,
    selectedKey:ctx.selectedVulnerabilityKey,
    selectedRecord:ctx.selectedVulnerability(),
    blocked:ctx.vulnerabilitySelectionBlocked,
    routeId:ctx.vulnerabilityRouteId,
    toast:calls.find(item => item.toast)?.toast || '',
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



def _run_vulnerability_project_load_order_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    scripts = [
        ROOT / "src/axiom/static/js/core.js",
        ROOT / "src/axiom/static/js/v2-adapter.js",
    ]
    program = f"""
const fs = require('fs');
const events = [];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }}, core: {{ methods: {{}} }}, projects: {{ methods: {{}} }},
  dashboard: {{ methods: {{}} }}, graph: {{ methods: {{}} }}, files: {{ methods: {{}} }},
}} }};
global.location = {{ origin:'http://testserver', hash:'#/vulnerabilities' }};
global.fetch = async url => {{
  events.push('vulnerabilities:fetch');
  return {{
    ok:true, status:200,
    headers:{{ get:name => name === 'X-Total-Count' ? '1' : null }},
    text:async () => JSON.stringify([{{
      id:'v007', task_id:'t_161', title:'目标漏洞', type:'越权', severity:'high',
      description:'d', prerequisites:'p', impact:'i', remediation:'r', status:'待验证',
      finding_artifact:null, evidence_artifacts:[],
    }}]),
  }};
}};
for (const script of {json.dumps([str(path) for path in scripts])}) eval(fs.readFileSync(script, 'utf8'));
const methods = Object.assign(
  {{}},
  window.AxiomModules.core.methods,
  window.AxiomModules.projects.methods,
);
const ctx = Object.assign({{}}, methods, {{
  projectGroups:[], projects:[],
  vulnFilter:{{ projectId:'', keyword:'' }},
  vulnerabilityPage:0, vulnerabilityPageSize:50, vulnerabilityLoading:false,
  vulnerabilityItems:[], vulnerabilityTotal:0, _vulnerabilityListRequestId:0,
  selectedVulnerabilityKey:'', vulnerabilityRouteTaskId:'', vulnerabilityRouteId:'',
  vulnerabilitySelectionBlocked:false,
  _pageAbortController:{{ signal:undefined }},
  loadProjectGroups:async () => {{
    events.push('groups:start');
    await sleep(20);
    ctx.projectGroups = [{{ id:'prj_026', title:'通用小程序' }}];
    events.push('groups:end');
  }},
  loadProjects:async () => {{
    events.push('tasks:start');
    await sleep(5);
    ctx.projects = [{{ id:'t_161', title:'AgentPark 小程序', parent_project_id:'prj_026' }}];
    events.push('tasks:end');
  }},
  showToast:() => {{}},
}});
(async () => {{
  await ctx.activateMountedPage('vulnerabilities');
  process.stdout.write(JSON.stringify({{
    events,
    projectTitle:ctx.vulnerabilityItems[0]?.project_group_title || '',
    parentProjectId:ctx.vulnerabilityItems[0]?.parent_project_id || '',
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _run_task_management_deep_link_fixture(*, task_id: str) -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    scripts = [
        ROOT / "src/axiom/static/js/projects.js",
        ROOT / "src/axiom/static/js/v2-adapter.js",
    ]
    program = f"""
const fs = require('fs');
const calls = [];
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }}, core: {{ methods: {{}} }}, projects: {{ methods: {{}} }},
  dashboard: {{ methods: {{}} }}, graph: {{ methods: {{}} }}, files: {{ methods: {{}} }},
}} }};
global.location = {{ origin:'http://testserver', hash:'#/projects' }};
global.document = {{
  querySelector: selector => selector.includes('t_161')
    ? {{ scrollIntoView: options => calls.push({{ scroll:options }}) }}
    : null,
}};
for (const script of {json.dumps([str(path) for path in scripts])}) eval(fs.readFileSync(script, 'utf8'));
const methods = window.AxiomModules.projects.methods;
const ctx = Object.assign({{}}, methods, {{
  projects:[], projectsLoading:false, projectsLoaded:false, projectsError:'',
  selectedTaskManagementId:'', taskManagementRouteId:'', taskManagementSelectionBlocked:false,
  selectedProjectManagementTaskId:'', selectedProjectManagementId:'',
  projectDetailsCache:{{}}, projectEventsCache:{{}}, taskDiagnostics:{{}}, taskDiagnosticsLoading:{{}},
  taskWorkerPoolEditor:{{ projectId:'' }}, taskScopeEditor:{{ taskId:'' }},
  taskSearch:'old query', taskStatusFilter:'active',
  api:async () => [
    {{ id:'t_160', title:'其他任务', status:'active', parent_project_id:'prj_025' }},
    {{ id:'t_161', title:'AgentPark 小程序', status:'stopped', parent_project_id:'prj_026' }},
  ],
  tasksForGroup:() => [],
  syncTaskWorkerPoolEditor:() => {{}}, syncTaskScopeEditor:() => {{}},
  loadTaskDiagnostics:() => null,
  showToast:(message, type='success') => calls.push({{ toast:message, type }}),
  $nextTick:callback => callback(),
}});
(async () => {{
  ctx.openTaskManagementLocation({json.dumps(task_id)});
  ctx.applyTaskManagementRouteParams(new URLSearchParams(`task=${{encodeURIComponent({json.dumps(task_id)})}}`));
  await ctx.loadProjects();
  process.stdout.write(JSON.stringify({{
    hash:global.location.hash,
    search:ctx.taskSearch,
    status:ctx.taskStatusFilter,
    selectedId:ctx.selectedTaskManagementId,
    selected:ctx.selectedTaskSummary()?.id || null,
    blocked:ctx.taskManagementSelectionBlocked,
    calls,
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)

def _run_vulnerability_review_ui_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
const calls = [];
const toasts = [];
let queued = false;
const vulnerability = {{
  id:'v002', project_id:'t_153', task_id:'t_153', title:'待审核漏洞', status:'待验证', archived:false,
}};
const completed = {{
  id:'vr000002', task_id:'t_153', vulnerability_id:'v002', status:'completed', trigger:'reported',
  decision:'duplicate', decision_reason:'与更早漏洞属于同一根因', duplicate_of_vulnerability_id:'v001',
  artifacts:[{{
    id:'a_review', task_id:'t_153', review_run_id:'vr000002', intent_id:null,
    artifact_type:'review.report', storage_key:'tasks/t_153/reviews/vr000002/review.md',
    summary:'审核报告', content_hash:'hash', size:42, created_by:'reviewer', created_at:'now', archived:false,
  }}],
  input_fingerprint:'fingerprint', worker_pool:[], settings_version:1,
  execute_timeout_seconds:300, conclude_timeout_seconds:120, retry_count:0,
  infrastructure_retry_count:0, last_failure_kind:'', failure_class:'', activity_kind:'completed',
  activity_summary:'done', activity_tool:'', decision_reason:'与更早漏洞属于同一根因',
  created_at:'now', updated_at:'now', concluded_at:'now',
}};
const pending = {{ ...completed, id:'vr000003', status:'pending', trigger:'manual', decision:null,
  decision_reason:'', duplicate_of_vulnerability_id:null, artifacts:[], concluded_at:null }};
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }}, core: {{ methods: {{}} }}, projects: {{ methods: {{}} }},
  graph: {{ methods: {{}} }}, files: {{ methods: {{}} }},
}} }};
global.location = {{ origin:'http://testserver', hash:'#/vulnerabilities?task=t_153&vulnerability=v002' }};
global.window.history = {{ replaceState: (_state, _title, url) => {{ global.location.hash = url; }} }};
global.window.confirm = () => true;
global.confirm = global.window.confirm;
global.fetch = async (url, init = {{}}) => {{
  const method = init.method || 'GET';
  const body = init.body ? JSON.parse(init.body) : null;
  calls.push({{ url, method, body }});
  let payload;
  if (method === 'GET' && url === '/api/v1/tasks/t_153/vulnerabilities/v002/reviews') {{
    payload = queued ? [pending, completed] : [completed];
  }} else if (method === 'POST' && url === '/api/v1/tasks/t_153/vulnerabilities/v002/reviews') {{
    queued = true;
    payload = pending;
  }} else if (method === 'POST' && url === '/api/v1/tasks/t_153/vulnerabilities/v002/external-report') {{
    payload = {{ attempt: {{ status:'succeeded' }} }};
  }} else {{
    throw new Error(`unexpected request: ${{method}} ${{url}}`);
  }}
  return {{ status:200, ok:true, text:async () => JSON.stringify(payload) }};
}};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
const methods = Object.assign(
  {{}},
  window.AxiomModules.projects.methods,
  window.AxiomModules.files.methods,
);
const ctx = Object.assign({{}}, methods, {{
  vulnerabilityReviews:[], vulnerabilityReviewsLoading:false, vulnerabilityReviewsError:'',
  vulnerabilityReviewSubmitting:false, _vulnerabilityReviewRequestId:0,
  vulnerabilityItems:[vulnerability], selectedVulnerabilityKey:'t_153:v002',
  selectedVulnerability:() => vulnerability,
  vulnerabilityKey:item => `${{item.project_id || item.task_id}}:${{item.id}}`,
  actorName:() => '人工',
  loadVulnerabilityPage:async () => calls.push({{ action:'reload-page' }}),
  showToast:(message, type='success') => toasts.push({{ message, type }}),
  artifactCenterSelectedTask:'', artifactCenterFolderKey:'', artifactCenterRouteArtifactId:'',
  artifactCenterSearch:'', artifactCenterType:'all', artifactCenterPhase:'all', artifactCenterSource:'all',
  artifactCenterPage:0, artifactCenterFolderPage:0, artifactCenterSelected:null,
}});
(async () => {{
  await ctx.loadVulnerabilityReviews(vulnerability);
  const loaded = {{
    latest:ctx.latestVulnerabilityReview().id,
    decision:ctx.vulnerabilityReviewDecisionLabel(ctx.latestVulnerabilityReview().decision),
    retry:ctx.vulnerabilityReviewCanRetry(),
    artifactReviewRun:ctx.latestVulnerabilityReview().artifacts[0].review_run_id,
  }};
  ctx.openReviewDuplicate(ctx.latestVulnerabilityReview());
  const duplicateHash = location.hash;
  await ctx.openArtifactLocation('t_153', ctx.latestVulnerabilityReview().artifacts[0]);
  const artifactHash = location.hash;
  await ctx.triggerVulnerabilityReview(vulnerability);
  const afterTrigger = {{
    latest:ctx.latestVulnerabilityReview().id,
    state:ctx.vulnerabilityReviewStateLabel(ctx.latestVulnerabilityReview()),
    retry:ctx.vulnerabilityReviewCanRetry(),
    submitting:ctx.vulnerabilityReviewSubmitting,
  }};
  await ctx.reportExternalVulnerability(vulnerability);
  const blockedExternalPosts = calls.filter(item => item.url?.endsWith('/external-report')).length;
  vulnerability.status = '已验证';
  await ctx.reportExternalVulnerability(vulnerability);
  const successfulExternalPosts = calls.filter(item => item.url?.endsWith('/external-report')).length;
  process.stdout.write(JSON.stringify({{
    loaded, duplicateHash, artifactHash, afterTrigger, blockedExternalPosts,
    successfulExternalPosts, calls, toasts,
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


def _run_skill_refresh_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    skills = ROOT / "src/axiom/static/js/skills.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(skills))}, 'utf8'));
let listed = [{{
  name:'miniapp', description:'新版说明', content:'---\\nname: miniapp\\ndescription: 新版说明\\n---\\n\\n新版正文\\n',
  builtin:false, enabled:true, source:'managed', bundled_hash:'bundled-2', effective_hash:'effective-2',
  sync_status:'synced', bundled_update_available:false,
}}];
const calls = [];
const ctx = Object.assign({{
  skills: [],
  skillFiles: [{{ path:'SKILL.md', size:1 }}],
  skillFileTree: [],
  skillExpandedDirs: {{}},
  skillEditor: {{
    name:'miniapp', description:'旧版说明', content:'旧版正文', isNew:false, builtin:false,
    enabled:true, source:'managed', bundledHash:'bundled-1', effectiveHash:'effective-1',
    sync_status:'synced', bundledUpdateAvailable:false, activeFile:'SKILL.md',
    fileContent:'旧版正文', fileDirty:false, fileTruncated:false,
  }},
  api: async (method, path) => {{
    calls.push({{ method, path }});
    if (method === 'GET' && path === '/skills') return listed;
    if (method === 'GET' && path === '/skills/miniapp/files') return [{{ path:'SKILL.md', size:100 }}];
    throw new Error(`unexpected ${{method}} ${{path}}`);
  }},
  loadPhaseCapabilities: async () => {{}},
  showToast: () => {{}},
}}, window.AxiomModules.skills.methods);
(async () => {{
  await ctx.loadSkills();
  const clean = {{
    description:ctx.skillEditor.description,
    fileContent:ctx.skillEditor.fileContent,
    bundledHash:ctx.skillEditor.bundledHash,
    effectiveHash:ctx.skillEditor.effectiveHash,
    syncStatus:ctx.skillEditor.sync_status,
  }};
  ctx.skillEditor.fileContent = '本地未保存修改';
  ctx.skillEditor.fileDirty = true;
  listed = [{{ ...listed[0], description:'第三版说明', content:'---\\nname: miniapp\\ndescription: 第三版说明\\n---\\n\\n第三版正文\\n' }}];
  await ctx.loadSkills();
  process.stdout.write(JSON.stringify({{
    clean,
    dirty:{{ description:ctx.skillEditor.description, fileContent:ctx.skillEditor.fileContent, fileDirty:ctx.skillEditor.fileDirty }},
    calls,
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



def _run_skill_large_file_preview_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    skills = ROOT / "src/axiom/static/js/skills.js"
    program = f"""
const fs = require('fs');
global.confirm = () => true;
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(skills))}, 'utf8'));
const calls = [];
const toasts = [];
const ctx = Object.assign({{}}, window.AxiomModules.skills.methods, {{
  skillFiles: [
    {{ path:'small.txt', size:12 }},
    {{ path:'large.txt', size:600000 }},
  ],
  skillEditor: {{ name:'ffuf', isNew:false, activeFile:'', fileContent:'', fileDirty:false, fileTruncated:false }},
  api: async (method, path) => {{
    calls.push({{ method, path }});
    if (path.includes('large.txt')) return {{ path:'large.txt', content:'A'.repeat(524288), truncated:true, size:600000 }};
    return {{ path:'small.txt', content:'small', truncated:false, size:12 }};
  }},
  showToast: (message, type='info') => toasts.push({{ message, type }}),
}});
(async () => {{
  await ctx.openSkillFile('large.txt');
  const large = {{ contentLength:ctx.skillEditor.fileContent.length, truncated:ctx.skillEditor.fileTruncated }};
  ctx.skillEditor.fileDirty = true;
  await ctx.saveSkillFile();
  await ctx.openSkillFile('small.txt');
  process.stdout.write(JSON.stringify({{
    calls,
    large,
    saveCalls:calls.filter(call => call.method === 'PUT').length,
    smallTruncated:ctx.skillEditor.fileTruncated,
    toasts,
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)

def _run_skill_file_tree_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    skills = ROOT / "src/axiom/static/js/skills.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(skills))}, 'utf8'));
const ctx = Object.assign({{
  skillFiles: [],
  skillFileTree: [],
  skillExpandedDirs: {{}},
}}, window.AxiomModules.skills.methods);
ctx.buildSkillFileTree([
  {{ path: 'SKILL.md', size: 120 }},
  {{ path: 'references/Web-Fuzzing-Box/README_CN.md', size: 2048 }},
  {{ path: 'references/Web-Fuzzing-Box/Brute/Username/top.txt', size: 4096 }},
  {{ path: 'references/Web-Fuzzing-Box/Web/Dir/common.txt', size: 8192 }},
  {{ path: 'assets/sample.xlsx', size: 1024 }},
]);
const initial = ctx.skillFileTreeRows().map(item => [item.type, item.path, item.depth]);
ctx.toggleSkillDirectory('references/Web-Fuzzing-Box/Brute');
const expanded = ctx.skillFileTreeRows().map(item => item.path);
process.stdout.write(JSON.stringify({{
  initial,
  expanded,
  binaryPreviewable: ctx.skillFilePreviewable('assets/sample.xlsx'),
  textPreviewable: ctx.skillFilePreviewable('references/Web-Fuzzing-Box/README_CN.md'),
}}));
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


def _run_human_overview_cycle_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    scripts = [
        ROOT / "src/axiom/static/js/helpers.js",
        ROOT / "src/axiom/static/js/human.js",
    ]
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
for (const script of {json.dumps([str(path) for path in scripts])}) eval(fs.readFileSync(script, 'utf8'));
const methods = Object.assign({{}}, window.AxiomModules.helpers.methods, window.AxiomModules.human.methods);
const makeEvent = (id, type, run, at, payload = {{}}, extra = {{}}) => ({{ id, event_type:type, reason_run_id:run, created_at:at, payload, ...extra }});
const ctx = Object.assign({{}}, methods, {{
  project: {{
    project: {{ id:'t_150', phase:'waiting', status:'active' }},
    intents: [
      {{ id:'i001', creator:'dispatcher.bootstrap', description:'bootstrap', status:'completed', created_at:'2026-07-28T00:00:00Z' }},
      {{ id:'i112', description:'旧批次', status:'completed', created_at:'2026-07-29T01:00:00Z' }},
      {{ id:'i113', description:'最新批次', status:'completed', created_at:'2026-07-29T03:00:00Z' }},
      {{ id:'i114', description:'等待领取', status:'completed', created_at:'2026-07-29T02:00:00Z' }},
      {{ id:'i115', description:'正在执行', status:'completed', created_at:'2026-07-29T01:30:00Z' }},
      {{ id:'i116', description:'待执行', status:'completed', created_at:'2026-07-29T04:00:00Z' }},
    ],
  }},
  seeEvents: [
    makeEvent('e1','reason_run.created','rr0001','2026-07-29T01:00:00Z',{{trigger:'scheduler'}}),
    makeEvent('e2','reason_workspace.opened','rr0001','2026-07-29T01:01:00Z',{{}},{{workspace_id:'rw1',worker:'reason-a'}}),
    makeEvent('e3','reason_workspace.abandoned','rr0001','2026-07-29T01:02:00Z',{{failure_reason:'第一次失败'}},{{workspace_id:'rw1'}}),
    makeEvent('e4','reason_workspace.opened','rr0001','2026-07-29T01:03:00Z',{{}},{{workspace_id:'rw2',worker:'reason-a'}}),
    makeEvent('e5','reason.committed','rr0001','2026-07-29T01:04:00Z',{{created_intent_ids:['i112'],created_todo_ids:[]}},{{workspace_id:'rw2'}}),
    makeEvent('e6','reason_run.created','rr0002','2026-07-29T03:00:00Z',{{trigger:'manual'}}),
    makeEvent('e7','reason.committed','rr0002','2026-07-29T03:01:00Z',{{created_intent_ids:['i113'],created_todo_ids:[]}}),
  ],
}});
const first = ctx.humanCycleModel();
const firstDisplay = ctx.humanCycleDisplayIntents().map(item => item.id);
ctx.project.intents.forEach(item => {{ if (['i114','i115','i116'].includes(item.id)) item.status = ({{i114:'claimed',i115:'running',i116:'pending'}})[item.id]; }});
const activeOrder = ctx.humanCurrentIntents().map(item => item.id);
ctx.project.intents.forEach(item => {{ if (['i114','i115','i116'].includes(item.id)) item.status = 'completed'; }});
ctx.seeEvents.push(makeEvent('e8','reason_run.created','rr0003','2026-07-29T05:00:00Z',{{trigger:'scheduler'}}));
ctx.seeEvents.push(makeEvent('e9','reason.committed','rr0003','2026-07-29T05:01:00Z',{{created_intent_ids:[],created_todo_ids:[]}}));
const zero = ctx.humanCycleModel();
process.stdout.write(JSON.stringify({{
  count:first.reasonCount,
  current:first.current.reasonRunId,
  history:first.history.map(item => ({{ id:item.reasonRunId, attempts:item.workspaceAttempts, intents:item.intents.map(intent => intent.id), reason:item.failureReason }})),
  display:firstDisplay,
  activeOrder,
  zeroCurrent:zero.current.reasonRunId,
  zeroDisplay:ctx.humanCycleDisplayIntents().map(item => item.id),
}}));
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _run_human_overview_content_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    scripts = [
        ROOT / "src/axiom/static/js/helpers.js",
        ROOT / "src/axiom/static/js/human.js",
    ]
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
for (const script of {json.dumps([str(path) for path in scripts])}) eval(fs.readFileSync(script, 'utf8'));
const calls = [];
const ctx = Object.assign({{}}, window.AxiomModules.helpers.methods, window.AxiomModules.human.methods, {{
  selectedProjectId:'t_150',
  project: {{
    project: {{ id:'t_150', artifact_count:99, vulnerability_count:7 }},
    intents: [
      {{ id:'i113', description:'验证最新方向', status:'completed', to:'f113' }},
      {{ id:'i114', description:'另一方向', status:'completed', to:'f112' }},
    ],
    facts: [
      {{ id:'f112', description:'较旧事实', created_at:'2026-07-29T01:00:00Z' }},
      {{ id:'f113', description:'已确认事实：artifact:a679', created_at:'2026-07-29T03:00:00Z' }},
    ],
    artifacts: [
      {{ id:'a679', intent_id:'i113', artifact_type:'explore.findings', path:'i113/findings.md', created_at:'2026-07-29T03:10:00Z' }},
      {{ id:'a685', intent_id:'i113', artifact_type:'explore.evidence', path:'i113/evidence/x.txt', created_at:'2026-07-29T03:20:00Z' }},
      {{ id:'a686', intent_id:'i113', artifact_type:'explore.findings', path:'i113/findings-latest.md', created_at:'2026-07-29T03:30:00Z' }},
    ],
    lead_artifacts: [
      {{ id:'a680', intent_id:'i113', artifact_type:'explore.leads', path:'i113/leads.md', created_at:'2026-07-29T03:11:00Z' }},
      {{ id:'a681', intent_id:'i114', artifact_type:'explore.leads', path:'i114/leads.md', created_at:'2026-07-29T02:11:00Z' }},
      {{ id:'a001', intent_id:'i001', artifact_type:'bootstrap.initial_leads', path:'i001/initial_leads.md', created_at:'2026-07-28T00:10:00Z' }},
    ],
  }},
  openArtifactLocation: (taskId, target) => calls.push({{ taskId, target }}),
  navigateArtifactCenter: (taskId, folder) => calls.push({{ taskId, folder }}),
  showToast: (message, type) => calls.push({{ message, type }}),
}});
(async () => {{
  await ctx.openHumanFact(ctx.project.facts[1]);
  ctx.openHumanArtifact(ctx.project.lead_artifacts[0]);
  const leads = ctx.humanLeadArtifacts(3).map(item => item.id);
  const source = ctx.humanSourceLabel(ctx.project.lead_artifacts[0]);
  ctx.project.lead_artifacts = ctx.project.lead_artifacts.filter(item => item.artifact_type === 'bootstrap.initial_leads');
  process.stdout.write(JSON.stringify({{
    facts:ctx.humanRecentFacts(3).map(item => item.id),
    leads,
    bootstrapLeads:ctx.humanLeadArtifacts(3).map(item => item.id),
    artifacts:ctx.humanRecentArtifacts(3).map(item => item.id),
    source,
    counts:[ctx.humanFactCount(),ctx.humanArtifactCount(),ctx.humanVulnerabilityCount()],
    calls,
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _run_human_event_initial_load_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    dispatch = ROOT / "src/axiom/static/js/dispatch.js"
    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{
  state: {{ state: {{}} }}, core: {{ methods: {{}} }}, projects: {{ methods: {{}} }},
  dashboard: {{ methods: {{}} }}, graph: {{ methods: {{}} }}, files: {{ methods: {{}} }},
}} }};
global.location = {{ origin:'http://testserver', hash:'#/tasks/t_150' }};
const calls = [];
global.fetch = async (url, init = {{}}) => {{
  calls.push(url);
  if (url.includes('after_id=e002')) return {{ ok:true, status:200, text:async () => JSON.stringify([{{id:'e003', event_type:'reason_run.created', created_at:'2026-08-03T03:00:00Z', payload:{{}}, task_id:'t_150'}}]) }};
  if (url.includes('/audit-events?')) return {{ ok:true, status:200, text:async () => JSON.stringify([
    {{id:'e002', event_type:'reason_run.started', created_at:'2026-08-03T02:00:00Z', payload:{{}}, task_id:'t_150'}},
    {{id:'e001', event_type:'reason_run.created', created_at:'2026-08-03T01:00:00Z', payload:{{}}, task_id:'t_150'}},
  ]) }};
  throw new Error(`unexpected ${{url}}`);
}};
for (const script of [{json.dumps(str(dispatch))}, {json.dumps(str(adapter))}]) eval(fs.readFileSync(script, 'utf8'));
const ctx = Object.assign({{}}, window.AxiomModules.core.methods, window.AxiomModules.dispatch.methods, {{
  selectedProjectId:'t_150', seeEvents:[], seeLastEventId:0,
  workerStreams:() => [], $nextTick:fn => fn(),
}});
(async () => {{
  await ctx.loadSeeEvents(true);
  const first = ctx.seeEvents.map(item => item.id);
  const cursor = ctx.seeLastEventId;
  await ctx.loadSeeEvents(false);
  process.stdout.write(JSON.stringify({{ first, cursor, after:ctx.seeEvents.map(item => item.id), calls }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _run_worker_capability_drawer_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    worker_tools = ROOT / "src/axiom/static/js/worker-tools.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
eval(fs.readFileSync({json.dumps(str(worker_tools))}, 'utf8'));
const methods = window.AxiomModules.workerTools.methods;
const items = [
  {{ capability_kind:'skill', capability_key:'all-skill', title:'All Skill', enabled:true, ready:true, status:'ready', supported_phases:['bootstrap','reason','explore','review'], scopes:['common'], locked_scopes:[], inactive_reasons:{{}}, metadata:{{}} }},
  {{ capability_kind:'skill', capability_key:'locked-skill', title:'Locked Skill', enabled:true, ready:true, status:'ready', supported_phases:['bootstrap','reason','explore','review'], scopes:['common'], locked_scopes:['common'], inactive_reasons:{{}}, metadata:{{}} }},
  {{ capability_kind:'skill', capability_key:'disabled-new', title:'Disabled', enabled:false, ready:true, status:'ready', supported_phases:['bootstrap','reason','explore','review'], scopes:[], locked_scopes:[], inactive_reasons:{{}}, metadata:{{}} }},
  {{ capability_kind:'skill', capability_key:'disabled-old', title:'Disabled Existing', enabled:false, ready:true, status:'ready', supported_phases:['bootstrap','reason','explore','review'], scopes:['reason'], locked_scopes:[], inactive_reasons:{{reason:'Skill 已全局停用'}}, metadata:{{}} }},
  {{ capability_kind:'mcp_tool', capability_key:'mcp-1', title:'One', description:'first', enabled:true, ready:true, status:'ready', server_id:'server-1', server_name:'Browser', supported_phases:['bootstrap','reason','explore','review'], scopes:['reason'], locked_scopes:[], inactive_reasons:{{}}, metadata:{{tool_name:'one'}} }},
  {{ capability_kind:'mcp_tool', capability_key:'mcp-2', title:'Two', description:'second', enabled:true, ready:true, status:'ready', server_id:'server-1', server_name:'Browser', supported_phases:['bootstrap','reason','explore','review'], scopes:[], locked_scopes:[], inactive_reasons:{{}}, metadata:{{tool_name:'two'}} }},
  {{ capability_kind:'axiom_tool', capability_key:'partial-tool', title:'Partial', enabled:true, ready:true, status:'ready', supported_phases:['bootstrap','explore'], scopes:['bootstrap'], locked_scopes:[], inactive_reasons:{{}}, metadata:{{}} }},
];
const calls = [];
const matrix = {{ scopes:{{}}, items, builtin_tools:[] }};
const ctx = Object.assign({{}}, methods, {{
  phaseCapabilities:matrix,
  phaseCapabilityDraft:{{}},
  phaseCapabilitySaving:{{}},
  workerCapabilityDraft:{{}},
  workerCapabilityBaseline:{{}},
  workerCapabilityDrawerPhase:'reason',
  workerCapabilityDrawerTab:'mcp',
  workerCapabilitySearch:'',
  workerCapabilityFilter:'all',
  workerCapabilitySaving:false,
  workerCapabilityMcpExpanded:{{}},
  axiomToolsCatalog:[],
  selectedBuiltinTool:'',
  showToast:(message, type) => calls.push({{kind:'toast', message, type}}),
  api:async (method, path, body) => {{ calls.push({{kind:'api', method, path, body}}); return matrix; }},
}});
ctx.resetWorkerCapabilityDraft();
const allSkill = ctx.phaseCapabilityRecord('skill', 'all-skill');
ctx.toggleWorkerCapabilityPhase(allSkill, 'explore');
const subsetPayload = ctx.workerCapabilityMatrixPayload();
ctx.toggleWorkerCapabilityPhase(allSkill, 'explore');
const commonPayload = ctx.workerCapabilityMatrixPayload();
const disabledNew = ctx.phaseCapabilityRecord('skill', 'disabled-new');
ctx.toggleWorkerCapabilityPhase(disabledNew, 'reason');
const disabledNewPhases = [...ctx.workerCapabilityDraftPhases(disabledNew)];
const disabledOld = ctx.phaseCapabilityRecord('skill', 'disabled-old');
ctx.toggleWorkerCapabilityPhase(disabledOld, 'reason');
const disabledOldPhases = [...ctx.workerCapabilityDraftPhases(disabledOld)];
const partial = ctx.phaseCapabilityRecord('axiom_tool', 'partial-tool');
const partialReasonDisabled = ctx.workerCapabilityPhaseDisabled(partial, 'reason');
const group = ctx.workerCapabilityMcpGroups()[0];
const groupBefore = ctx.workerCapabilityMcpGroupPhaseState(group, 'reason');
ctx.toggleWorkerCapabilityMcpGroupPhase(group, 'reason');
const groupAfter = ctx.workerCapabilityMcpGroupPhaseState(group, 'reason');
(async () => {{
  const changesBeforeSave = ctx.workerCapabilityChangeCount();
  await ctx.saveWorkerCapabilityDraft();
  process.stdout.write(JSON.stringify({{
    subsetPayload,
    commonPayload,
    disabledNewPhases,
    disabledOldPhases,
    partialReasonDisabled,
    groupBefore,
    groupAfter,
    groups:ctx.workerCapabilityMcpGroups().map(item => [item.id, item.tools.length]),
    changesBeforeSave,
    apiCalls:calls.filter(item => item.kind === 'api'),
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _run_worker_capability_deep_link_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    core = ROOT / "src/axiom/static/js/core.js"
    worker_tools = ROOT / "src/axiom/static/js/worker-tools.js"
    program = f"""
const fs = require('fs');
global.window = {{ AxiomModules: {{}} }};
global.location = {{ hash:'#/workers?mode=manage&phase=reason&tab=skill&capability=agent-mail' }};
for (const script of [{json.dumps(str(core))}, {json.dumps(str(worker_tools))}]) {{
  eval(fs.readFileSync(script, 'utf8'));
}}
const methods = Object.assign(
  {{}},
  window.AxiomModules.core.methods,
  window.AxiomModules.workerTools.methods,
);
const calls = [];
const ctx = Object.assign({{}}, methods, {{
  activePage:'dashboard', view:'list', mountedPageTemplate:'', pageHistory:[],
  mobileSidebarOpen:false, _navigatingBack:false,
  workerPageMode:'manage', selectedWorkerPhase:'bootstrap', workerCapabilityRouteTarget:null,
  workerCapabilityDrawerOpen:false, workerCapabilityDrawerPhase:'bootstrap',
  workerCapabilityDrawerTab:'overview', workerCapabilityFocusKey:'',
  workerCapabilitySearch:'', workerCapabilityFilter:'all', workerCapabilityMcpExpanded:{{}},
  workerCapabilityDraft:{{}}, workerCapabilityBaseline:{{}},
  phaseCapabilities:{{ scopes:{{}}, items:[{{
    capability_kind:'skill', capability_key:'agent-mail', title:'Agent-Mail',
    enabled:true, ready:true, status:'ready', supported_phases:['bootstrap','reason','explore','review'],
    scopes:['reason'], locked_scopes:[], inactive_reasons:{{}}, metadata:{{}},
  }}], builtin_tools:[] }},
  phaseCapabilityDraft:{{}}, axiomToolsCatalog:[], selectedBuiltinTool:'',
  workerPhases:[
    {{phase:'bootstrap', title:'Bootstrap Worker', backends:[]}},
    {{phase:'reason', title:'Reason Worker', backends:[]}},
    {{phase:'explore', title:'Explore Worker', backends:[]}},
    {{phase:'review', title:'Reviewer Worker', backends:[]}},
  ],
  selectedAxiomWorker:'',
  loadDispatchSettings:async () => calls.push('dispatch'),
  loadAxiomWorkersOverview:async () => calls.push('overview'),
  loadWorkerPhases:async () => calls.push('phases'),
  loadPhaseCapabilities:async () => calls.push('capabilities'),
  backToList:() => {{}},
  $nextTick:fn => fn(),
}});
ctx.mountCurrentPage = () => {{
  ctx._mountPromise = ctx.loadAxiomWorkersPage();
  return ctx._mountPromise;
}};
(async () => {{
  ctx.handleRoute();
  await ctx._mountPromise;
  const deepLink = {{
    activePage:ctx.activePage,
    mode:ctx.workerPageMode,
    selectedPhase:ctx.selectedWorkerPhase,
    drawerOpen:ctx.workerCapabilityDrawerOpen,
    drawerPhase:ctx.workerCapabilityDrawerPhase,
    drawerTab:ctx.workerCapabilityDrawerTab,
    focusKey:ctx.workerCapabilityFocusKey,
    routeTarget:ctx.workerCapabilityRouteTarget,
  }};
  global.location.hash = '#/workers';
  ctx.handleRoute();
  await ctx._mountPromise;
  process.stdout.write(JSON.stringify({{
    deepLink,
    plainRoute:{{
      drawerOpen:ctx.workerCapabilityDrawerOpen,
      focusKey:ctx.workerCapabilityFocusKey,
      routeTarget:ctx.workerCapabilityRouteTarget,
    }},
    calls,
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_frontend_and_static_assets_are_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>axiom</title>" in response.text
    # AXIOM_AUTH_DISABLED=1 等价于已登录模式：HTML 包含完整业务脚本列表
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

    worker_asset_versions = set()
    for script in ("state", "core", "skills", "mcp", "worker-tools"):
        match = re.search(
            rf'/static/js/{re.escape(script)}\.js\?v=([a-zA-Z0-9._-]+)',
            response.text,
        )
        assert match is not None
        worker_asset_versions.add(match.group(1))
    # Worker/capability assets are versioned together with the current
    # five-stage Plan runtime bundle.  Keep this assertion tied to the
    # manifest served by the app rather than the pre-Plan cache marker.
    assert worker_asset_versions == {"20260821-clean-bootstrap-edge-v1"}

    asset = client.get("/static/js/v2-adapter.js")
    assert asset.status_code == 200
    assert "Backend V2 adapter" in asset.text

    state = client.get("/static/js/state.js")
    assert state.status_code == 200
    assert "projectDetailsLoaded: false" in state.text
    assert "projectsLoaded: false" in state.text

    dashboard = client.get("/static/templates/dashboard.html")
    assert dashboard.status_code == 200
    assert "LLM Token 消耗" in dashboard.text
    assert "dashboardTokenModel" in dashboard.text
    assert "tokenChartRows()" in dashboard.text
    for label in ("输入（未命中）", "缓存命中", "缓存写入", "输出", "任务占比"):
        assert label in dashboard.text
    assert "Skill 调用统计" not in dashboard.text
    assert "globalSkillUsage" not in dashboard.text

    vulnerabilities = client.get("/static/templates/vulnerabilities.html")
    assert vulnerabilities.status_code == 200
    assert 'x-model="vulnEditor.reproduction_steps"' not in vulnerabilities.text
    assert 'x-model="vulnEditor.curl_command"' not in vulnerabilities.text
    assert 'x-model="vulnEditor.evidence"' not in vulnerabilities.text
    assert "finding_artifact" in vulnerabilities.text
    assert "evidence_artifacts" in vulnerabilities.text
    assert "Reviewer 审核" in vulnerabilities.text
    assert "重新审核" in vulnerabilities.text
    assert "vulnerabilityReviewDecisionLabel" in vulnerabilities.text
    assert "只有已验证漏洞可以外部上报" in vulnerabilities.text
    assert "duplicate_of_vulnerability_id" in vulnerabilities.text
    assert "看板视图" not in vulnerabilities.text
    assert "vulnerabilityBoard" not in vulnerabilities.text
    assert "导出报告" not in vulnerabilities.text
    assert "导出 MD" not in vulnerabilities.text

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
    assert "单 Task Reviewer 并发" in settings.text
    assert "per_task_review_concurrency" in settings.text
    assert settings.text.count("1（固定）") >= 2
    assert "Worker 管理已迁移" not in settings.text

    workers = client.get("/static/templates/workers.html")
    assert workers.status_code == 200
    assert "五阶段 Worker" in workers.text
    assert "系统有五个逻辑 Worker" in workers.text
    assert "Bootstrap、Reason、Explore、Plan 或 Reviewer" in workers.text
    assert "模型执行后端" in workers.text
    assert "workerPhases" in workers.text
    assert "能力管理" in workers.text
    assert "后端配置" in workers.text
    assert "内置 Tool" in workers.text
    assert "Skill" in workers.text
    assert "MCP Tool" in workers.text
    assert "扩展 Tool" in workers.text
    assert "worker-capability-drawer" in workers.text
    assert "全部阶段" in workers.text
    assert "saveWorkerCapabilityDraft()" in workers.text
    assert "toggleWorkerCapabilityAll(item)" in workers.text
    assert "workerCapabilityMcpGroups()" in workers.text
    assert "当前结果分配给本阶段" in workers.text
    assert "由通用生效" not in workers.text
    assert "toggleAndSavePhaseCapabilityScope" not in workers.text
    assert "border-brand-300 bg-brand-50/60" not in workers.text
    assert "workerPhaseBackendRunning(selectedWorkerPhase, backend.name)" in workers.text
    assert "backend.shared_running" not in workers.text
    assert "axiomWorkerEditor?.test_result?.ok" in workers.text
    assert "axiomWorkerEditor.test_result.ok" not in workers.text
    assert "saveWorkerPhaseBackends" in workers.text
    assert "Plan 队列等待可用后端" in workers.text
    assert "saveWorkerAxiomToolBindings" not in workers.text
    assert "saveMcpWorkerBinding" not in workers.text
    assert "思考模式" in workers.text
    assert "pi_thinking_enabled" in workers.text
    assert "pi_thinking_level" in workers.text
    assert '<option value="max">max</option>' in workers.text
    assert "thinkingBadgeClass" not in settings.text

    skills = client.get("/static/templates/skills.html")
    assert skills.status_code == 200
    assert "阶段分配由 Worker 统一管理" in skills.text
    assert "Worker 可见性" in skills.text
    assert "navigateToWorkerCapability('skill'" in skills.text
    assert "skillFileTreeRows()" in skills.text
    assert "toggleSkillDirectory(f.path)" in skills.text
    assert "skillDirectoryExpanded(f.path)" in skills.text
    assert "saveSelectedSkillScopes" not in skills.text
    assert "togglePhaseCapabilityScope" not in skills.text

    mcp = client.get("/static/templates/mcp.html")
    assert mcp.status_code == 200
    assert "五阶段 MCP 可见性" in mcp.text
    assert "阶段分配由 Worker 统一管理" in mcp.text
    assert "Worker 可见性" in mcp.text
    assert "navigateToWorkerCapability('mcp_tool'" in mcp.text
    assert "saveMcpToolScopes" not in mcp.text
    assert "togglePhaseCapabilityScope" not in mcp.text
    assert "saveMcpWorkerBinding" not in mcp.text

    tools = client.get("/static/templates/tools.html")
    assert tools.status_code == 200
    assert "内置 Tool" in tools.text
    assert "扩展 Tool" in tools.text
    assert "系统锁定" in tools.text
    assert "Worker 可见性" in tools.text
    assert "navigateToWorkerCapability('axiom_tool'" in tools.text
    assert "Explore Conclude 不加载扩展 Tool" in tools.text
    assert "saveAxiomToolScopes" not in tools.text
    assert "togglePhaseCapabilityScope" not in tools.text
    assert "border-brand-300 bg-brand-50/60" not in tools.text
    for retired in (
        "search_text",
        "find_files",
        "list_directory",
        "http_request",
        "run_python",
    ):
        assert retired not in tools.text

    graph = client.get("/static/templates/graph-view.html")
    assert graph.status_code == 200
    assert "ReasonRun" in graph.text
    assert "配置快照" in graph.text
    assert "triggerManualReason()" in graph.text
    assert "立即 Reason" in graph.text
    assert "toggleProjectGuardById" in graph.text
    assert "开启守卫" in graph.text
    assert "关闭守卫" in graph.text
    assert "漏洞报告" not in graph.text
    assert "showLocalPrefs" not in graph.text
    assert "task-view-switcher" in graph.text
    assert 'class="app-page-header shrink-0 bg-white/90 backdrop-blur border-b border-slate-200/70 z-[60]"' in graph.text
    assert "openTaskContinuation(project.project)" in graph.text
    assert "showGoalHistory" in graph.text
    assert "selectedNode.type === 'goal_history'" in graph.text
    assert "selectedGoalEvaluationRecord()" in graph.text
    assert "达成证据 Fact" in graph.text
    assert "v.prerequisites || '未填写'" in graph.text
    assert "v.remediation || '未填写'" in graph.text

    human = client.get("/static/templates/human-view.html")
    assert human.status_code == 200
    assert "最近 Plan 决策" in human.text
    assert "planDecisionLabel(project.project.last_plan_decision)" in human.text
    assert 'x-if="humanTab === \'workspace\'"' in human.text
    assert 'x-if="humanTab === \'artifacts\'"' in human.text
    assert 'x-if="humanTab === \'conversation\'"' in human.text
    assert "sendExploreIntervention()" in human.text
    assert "exploreConversationIntents()" in human.text
    assert "jumpExploreConversationToLatest()" in human.text
    assert 'x-ref="exploreConversationScroll"' in human.text
    assert "event.tool" in human.text
    assert "humanArtifactPageCount" in human.text
    assert "triggerManualReason()" not in human.text
    assert "manual-reason-trigger" not in human.text
    assert "task-view-switcher" in human.text
    assert "Goal 历史" in human.text
    assert "openTaskContinuation(project.project)" in human.text
    assert "toggleProjectGuardById" in human.text
    assert "开启守卫" in human.text
    assert "关闭守卫" in human.text
    assert "本任务 Token 消耗" in human.text
    assert "taskTokenModel" in human.text
    assert "taskTokenRange" in human.text

    archive = client.get("/static/templates/archive-view.html")
    assert archive.status_code == 200
    assert "triggerManualReason()" not in archive.text
    assert "manual-reason-trigger" not in archive.text
    assert "task-view-switcher" in archive.text
    assert "openTaskContinuation(project.project)" in archive.text

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
    open_project = projects[
        projects.index("async openProject(") : projects.index("backToList(", projects.index("async openProject("))
    ]
    assert "await this.loadSeeEvents(true" not in open_project
    assert open_project.index("await this.mountCurrentPage()") < open_project.index(
        "void this.loadSeeEvents(true"
    )
    assert "_globalSearchAbortController?.abort()" in core
    assert "`/audit-events?task_id=${encodeURIComponent(taskId)}&limit=100`" not in adapter
    assert "const offset = append ? currentLength : 0" not in adapter
    assert "kind: intent.kind || ''" in adapter
    assert "`/tasks/${taskId}/reason-runs/trigger`" in adapter
    assert "`/tasks/${form.projectId}/continuations`" in adapter
    assert "goal_history: graph.goal_history || []" in adapter
    assert "queued_plan_count: allQueuedPlans.length" in adapter
    assert "applyVulnerabilityRouteParams" in adapter
    assert "openVulnerabilityLocation" in adapter
    assert "applyWorkerRouteParams" in core
    assert "new URLSearchParams(routeQuery)" in core
    assert "explore-conversation" in adapter
    assert "interventions" in adapter
    assert "thinking_enabled" in adapter
    assert "thinking_level" in adapter
    assert "token_range: this.dashboardTokenRange" in adapter
    assert "model: this.dashboardTokenModel" in adapter
    assert "loadGlobalSkillUsage" not in core
    assert "loadGlobalSkillUsage" not in projects
    assert "loadGlobalSkillUsage" not in adapter
    assert "loadVulnerabilityBoard" not in adapter
    assert "toggleVulnerabilityView" not in adapter
    assert "stopAllActiveProjects" not in projects
    assert "manualReasonTriggering" in state.text
    assert "showLocalPrefs" not in response.text
    assert "switchExportTab" not in response.text

    task_list = client.get("/static/templates/task-list.html")
    assert task_list.status_code == 200
    assert "暂停运行中" not in task_list.text
    assert "stopAllActiveProjects" not in task_list.text
    assert "showLocalPrefs" not in task_list.text
    assert "查看漏洞报告" not in task_list.text
    assert "Bootstrap、Explore、Reason、Plan、Reviewer" in task_list.text
    assert "['bootstrap', 'explore', 'reason', 'plan', 'review']" in task_list.text

    apple_css = client.get("/static/apple.css")
    assert apple_css.status_code == 200
    assert "factDescriptionSegments" in graph_js
    assert "openFactReference" in graph_js
    assert "x-html" not in graph.text
    assert "Plan 排队列表（FIFO）" in graph.text
    assert "planDecisionLabel(project.project.last_plan_decision)" in graph.text
    assert "selectedVulnerability().prerequisites || '未填写'" in vulnerabilities.text
    assert "selectedVulnerability().remediation || '未填写'" in vulnerabilities.text
    assert "按列表查看证据、影响和修复建议" in vulnerabilities.text
    assert "按卡片查看证据、影响和修复建议" not in vulnerabilities.text

    css = client.get("/static/apple.css").text
    assert "@media (prefers-color-scheme: dark)" not in css
    assert "#cy {\n  background: #f8fafc" not in css
    assert "background-color: var(--surface-graph)" in css
    assert ".task-view-switcher button.is-active" in css
    assert ".phase-plan" in css
    assert "@media (max-width: 640px)" in css
    custom_css = client.get("/static/custom.css").text
    assert ".worker-capability-drawer" in custom_css
    assert ".worker-capability-scope-button.is-selected" in custom_css
    assert "applyGraphTheme()" in graph_js
    assert "edgeLabelBackground" in graph_js
    assert "goal_evidence_history" in graph_js
    assert "goalGold" in graph_js
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


def test_skill_file_tree_expands_nested_directories_without_flattening() -> None:
    result = _run_skill_file_tree_fixture()

    assert result["initial"] == [
        ["directory", "assets", 0],
        ["directory", "references", 0],
        ["directory", "references/Web-Fuzzing-Box", 1],
        ["directory", "references/Web-Fuzzing-Box/Brute", 2],
        ["directory", "references/Web-Fuzzing-Box/Web", 2],
        ["file", "references/Web-Fuzzing-Box/README_CN.md", 2],
        ["file", "SKILL.md", 0],
    ]
    assert result["expanded"] == [
        "assets",
        "references",
        "references/Web-Fuzzing-Box",
        "references/Web-Fuzzing-Box/Brute",
        "references/Web-Fuzzing-Box/Brute/Username",
        "references/Web-Fuzzing-Box/Web",
        "references/Web-Fuzzing-Box/README_CN.md",
        "SKILL.md",
    ]
    assert result["binaryPreviewable"] is False
    assert result["textPreviewable"] is True



def test_skill_large_file_uses_truncated_preview_and_blocks_save() -> None:
    result = _run_skill_large_file_preview_fixture()

    assert result["large"] == {"contentLength": 524288, "truncated": True}
    assert result["calls"][:1] == [
        {"method": "GET", "path": "/skills/ffuf/files/large.txt?preview_bytes=524288"}
    ]
    assert result["saveCalls"] == 0
    assert result["smallTruncated"] is False
    assert any("文件过大，仅显示前 512 KB" in item["message"] for item in result["toasts"])


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


def test_skill_refresh_updates_open_clean_editor_but_preserves_unsaved_text() -> None:
    result = _run_skill_refresh_fixture()

    assert result["clean"] == {
        "description": "新版说明",
        "fileContent": "新版正文\n",
        "bundledHash": "bundled-2",
        "effectiveHash": "effective-2",
        "syncStatus": "synced",
    }
    assert result["dirty"] == {
        "description": "第三版说明",
        "fileContent": "本地未保存修改",
        "fileDirty": True,
    }
    assert result["calls"] == [
        {"method": "GET", "path": "/skills"},
        {"method": "GET", "path": "/skills/miniapp/files"},
        {"method": "GET", "path": "/skills"},
        {"method": "GET", "path": "/skills/miniapp/files"},
    ]


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


def test_vulnerability_review_history_retry_deep_links_and_external_gate() -> None:
    result = _run_vulnerability_review_ui_fixture()

    assert result["loaded"] == {
        "latest": "vr000002",
        "decision": "重复",
        "retry": True,
        "artifactReviewRun": "vr000002",
    }
    assert result["duplicateHash"] == "#/vulnerabilities?task=t_153&vulnerability=v001"
    assert result["artifactHash"] == "#/files?task=t_153&folder=vr000002&artifact=a_review"
    assert result["afterTrigger"] == {
        "latest": "vr000003",
        "state": "等待审核",
        "retry": False,
        "submitting": False,
    }
    assert result["blockedExternalPosts"] == 0
    assert result["successfulExternalPosts"] == 1
    review_posts = [
        item
        for item in result["calls"]
        if item.get("method") == "POST" and item.get("url", "").endswith("/reviews")
    ]
    assert review_posts == [
        {
            "url": "/api/v1/tasks/t_153/vulnerabilities/v002/reviews",
            "method": "POST",
            "body": {"actor": "人工", "reason": "人工要求重新审核"},
        }
    ]
    assert {item["message"] for item in result["toasts"]} >= {
        "已创建 Reviewer 审核任务",
        "只有 Reviewer 或人工裁决为已验证的漏洞才能外部上报",
        "外部上报成功",
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


def test_goal_history_node_selects_cycle_details_instead_of_virtual_fact() -> None:
    result = _run_goal_history_selection_fixture()

    assert result["selectedNode"] == {"type": "goal_history", "id": "gc001"}
    assert result["selectedFacts"] == []
    assert result["selectedTimelineEntryId"] is None
    assert result["history"]["goal"] == "第一轮目标"
    assert result["cycle"]["continuation_reason"] == "结果需要继续深入"
    assert result["cycle"]["focus"] == "越权链路"
    assert result["evaluation"]["reason"] == "证据已经证明目标达成"
    assert result["evidenceFactIds"] == ["f001", "f002"]
    assert result["highlighted"] == "gc001"
    assert result["factSelections"] == 0
    assert result["nodeGoalCycleId"] == "gc001"


def test_fact_references_are_safe_inline_links_with_precise_targets() -> None:
    result = _run_fact_reference_fixture()
    linked = [
        (segment["kind"], segment["id"], segment["text"])
        for segment in result["segments"]
        if segment["kind"] != "text"
    ]

    assert linked == [
        ("artifact", "a016", "artifact:a016"),
        ("artifact", "a017", "artifact:a017"),
        ("vulnerability", "v001", "vulnerability:v001"),
    ]
    assert "artifact:x001" in "".join(segment["text"] for segment in result["segments"])
    assert result["hasHtmlSegment"] is True
    assert result["calls"] == [
        {"kind": "artifact", "taskId": "t_153", "artifactId": "a016"},
        {"kind": "artifact", "taskId": "t_153", "artifactId": "a017"},
        {"kind": "vulnerability", "taskId": "t_153", "vulnerabilityId": "v001"},
    ]



def test_vulnerability_page_waits_for_projects_before_mapping_project_ownership() -> None:
    result = _run_vulnerability_project_load_order_fixture()

    assert result == {
        "events": [
            "groups:start",
            "tasks:start",
            "tasks:end",
            "groups:end",
            "vulnerabilities:fetch",
        ],
        "projectTitle": "通用小程序",
        "parentProjectId": "prj_026",
    }


def test_task_management_deep_link_selects_exact_task_and_resets_filters() -> None:
    result = _run_task_management_deep_link_fixture(task_id="t_161")

    assert result["hash"] == "#/tasks?task=t_161"
    assert result["search"] == ""
    assert result["status"] == "all"
    assert result["selectedId"] == "t_161"
    assert result["selected"] == "t_161"
    assert result["blocked"] is False
    assert result["calls"] == [
        {"scroll": {"block": "nearest", "behavior": "smooth"}},
    ]


def test_missing_task_management_deep_link_does_not_select_another_task() -> None:
    result = _run_task_management_deep_link_fixture(task_id="t_404")

    assert result["hash"] == "#/tasks?task=t_404"
    assert result["search"] == ""
    assert result["status"] == "all"
    assert result["selectedId"] == ""
    assert result["selected"] is None
    assert result["blocked"] is True
    assert result["calls"] == [
        {"toast": "任务不存在或无法打开", "type": "error"},
    ]

def test_vulnerability_deep_link_selects_an_item_outside_the_current_page() -> None:
    result = _run_vulnerability_deep_link_fixture()

    assert result == {
        "hash": "#/vulnerabilities?task=t_153&vulnerability=v001",
        "taskFilter": "t_153",
        "selected": "t_153:v001",
        "first": "v001",
        "detailFetch": True,
    }


@pytest.mark.parametrize(
    ("archived", "message"),
    [
        (False, "漏洞不存在或无法打开"),
        (True, "漏洞已归档，无法在当前列表中打开"),
    ],
)
def test_broken_vulnerability_deep_link_never_selects_a_different_record(
    archived: bool,
    message: str,
) -> None:
    result = _run_vulnerability_deep_link_error_fixture(archived=archived)

    assert result == {
        "hash": "#/vulnerabilities?task=t_153",
        "selectedKey": "",
        "selectedRecord": None,
        "blocked": True,
        "routeId": "",
        "toast": message,
    }


def test_graph_visual_updates_do_not_trigger_layout() -> None:
    result = _run_graph_visual_update_fixture()

    assert result == {
        "layouts": 0,
        "decorations": 1,
        "nodeLabel": "新状态",
        "edgeStatus": "in_progress",
    }


def test_human_overview_groups_reason_retries_into_one_cycle_and_keeps_latest_batch() -> None:
    result = _run_human_overview_cycle_fixture()

    assert result["count"] == 2
    assert result["current"] == "rr0002"
    assert result["history"] == [{
        "id": "rr0001",
        "attempts": 2,
        "intents": ["i112"],
        "reason": "第一次失败",
    }]
    assert result["display"] == ["i113"]
    assert result["activeOrder"][:3] == ["i115", "i114", "i116"]
    assert result["zeroCurrent"] == "rr0003"
    assert result["zeroDisplay"] == ["i113"]


def test_human_overview_sorts_recent_items_and_resolves_fact_and_source_deep_links() -> None:
    result = _run_human_overview_content_fixture()

    assert result["facts"] == ["f113", "f112"]
    assert result["leads"] == ["a680", "a681"]
    assert result["bootstrapLeads"] == ["a001"]
    assert result["artifacts"] == ["a686", "a685", "a679"]
    assert result["source"].startswith("来源 i113")
    assert result["counts"] == [2, 99, 7]
    assert result["calls"] == [
        {"taskId": "t_150", "target": "a679"},
        {"taskId": "t_150", "target": {"id": "a680", "intent_id": "i113", "artifact_type": "explore.leads", "path": "i113/leads.md", "created_at": "2026-07-29T03:11:00Z"}},
    ]


def test_human_overview_initial_audit_load_uses_recent_window_then_cursor_increment() -> None:
    result = _run_human_event_initial_load_fixture()

    assert result["first"] == ["e001", "e002"]
    assert result["cursor"] == "e002"
    assert result["after"] == ["e001", "e002", "e003"]
    assert "limit=2000" in result["calls"][0]
    assert "after_id=e002" in result["calls"][1]


def test_worker_capability_drawer_normalizes_scopes_and_saves_one_matrix() -> None:
    result = _run_worker_capability_drawer_fixture()

    subset = result["subsetPayload"]
    assert "all-skill" not in subset["common"]["skill"]
    assert "all-skill" in subset["bootstrap"]["skill"]
    assert "all-skill" in subset["reason"]["skill"]
    assert "all-skill" not in subset["explore"]["skill"]
    assert "all-skill" in subset["review"]["skill"]

    common = result["commonPayload"]
    assert "all-skill" in common["common"]["skill"]
    assert "all-skill" not in common["bootstrap"]["skill"]
    assert result["disabledNewPhases"] == []
    assert result["disabledOldPhases"] == []
    assert result["partialReasonDisabled"] is True
    assert result["groupBefore"] == "some"
    assert result["groupAfter"] == "all"
    assert result["groups"] == [["server-1", 2]]
    assert result["changesBeforeSave"] >= 2
    assert len(result["apiCalls"]) == 1
    assert result["apiCalls"][0]["method"] == "PUT"
    assert result["apiCalls"][0]["path"] == "/phase-capabilities"


def test_worker_capability_deep_link_opens_after_cold_page_load() -> None:
    result = _run_worker_capability_deep_link_fixture()

    assert result["deepLink"] == {
        "activePage": "workers",
        "mode": "manage",
        "selectedPhase": "reason",
        "drawerOpen": True,
        "drawerPhase": "reason",
        "drawerTab": "skill",
        "focusKey": "agent-mail",
        "routeTarget": None,
    }
    assert result["plainRoute"] == {
        "drawerOpen": False,
        "focusKey": "",
        "routeTarget": None,
    }
    assert result["calls"] == [
        "dispatch",
        "overview",
        "phases",
        "capabilities",
        "dispatch",
        "overview",
        "phases",
        "capabilities",
    ]


def test_mcp_tool_toggle_is_not_overridden_by_legacy_dispatch_module() -> None:
    mcp = (ROOT / "src/axiom/static/js/mcp.js").read_text(encoding="utf-8")
    dispatch = (ROOT / "src/axiom/static/js/dispatch.js").read_text(encoding="utf-8")

    assert "async toggleMcpTool(server, tool)" in mcp
    assert "this.api('PATCH', `/mcp/servers/${server.id}/tools/${tool.id}`" in mcp
    assert "async toggleMcpTool(server, tool)" not in dispatch


def test_human_intervention_frontend_contract_is_fact_scoped_and_escaped(client) -> None:
    graph = client.get("/static/js/graph.js")
    assert graph.status_code == 200
    graph_js = graph.text
    adapter = client.get("/static/js/v2-adapter.js")
    assert adapter.status_code == 200
    adapter_js = adapter.text
    template = client.get("/static/templates/graph-view.html")
    assert template.status_code == 200
    css = client.get("/static/custom.css")
    assert css.status_code == 200

    for field in (
        "human_intervention_required",
        "human_intervention_reason",
        "human_intervention_marked_at",
    ):
        assert field in adapter_js
    assert "this.getProducingIntent(f.id)" in graph_js
    assert "['fact', 'depth_fact'].includes(nodeType)" in graph_js
    assert "['origin', 'goal'].includes(factId)" in graph_js
    assert "node-card__human-dot" in graph_js
    assert "title=\"${interventionLabel}\"" in graph_js
    assert "aria-label=\"${interventionLabel}\"" in graph_js
    assert "replace(/&/g, '&amp;')" in graph_js
    assert "replace(/</g, '&lt;')" in graph_js
    assert "replace(/'/g, '&#39;')" in graph_js
    assert "humanInterventionForFact(fact.id)" in template.text
    assert "humanInterventionForFact(selectedFactId())" in template.text
    assert 'x-text="humanInterventionForFact(fact.id).human_intervention_reason"' in template.text
    assert 'x-text="humanInterventionForFact(selectedFactId()).human_intervention_reason"' in template.text
    assert "var(--danger)" in css.text
    assert "var(--status-danger-bg)" in css.text


def _run_task_detail_loading_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
const calls = [];
const sections = [];
const task = {{
  id:'t_164', project_id:'prj_001', title:'任务 164', goal:'验证目标',
  origin:{{ original_input:'AgentPark 小程序' }}, status:'running', phase:'reason',
  archived:false, worker_pool:[], scope:{{ enabled:false, targets:[], exclude:[] }},
  fact_count:2, intent_count:1, todo_count:1, vulnerability_count:1, artifact_count:1,
}};
const payloads = {{
  '/api/v1/tasks/t_164': task,
  '/api/v1/projects/prj_001': {{ id:'prj_001', title:'测试项目' }},
  '/api/v1/tasks/t_164/graph': {{
    facts:[{{ id:'origin', description:'AgentPark 小程序' }}, {{ id:'f001', description:'事实' }}],
    intents:[{{ id:'i001', from_facts:['origin'], description:'Bootstrap', status:'completed', kind:'bootstrap' }}],
    goal_from_fact_ids:['f001'], goal_history:[],
  }},
  '/api/v1/tasks/t_164/hints': [{{ id:'h001', content:'hint' }}],
  '/api/v1/tasks/t_164/todos?limit=200': [{{ id:'td001', title:'todo' }}],
  '/api/v1/tasks/t_164/vulnerabilities?limit=200': [{{ id:'v001', task_id:'t_164', title:'vuln', evidence_artifacts:[] }}],
  '/api/v1/tasks/t_164/artifacts?limit=12': [{{ id:'a001', path:'artifacts/a.md' }}],
  '/api/v1/tasks/t_164/artifacts?artifact_type=explore.leads&limit=20': [{{ id:'a002', path:'artifacts/leads.md' }}],
  '/api/v1/tasks/t_164/artifacts?artifact_type=bootstrap.initial_leads&limit=20': [],
  '/api/v1/tasks/t_164/goal-cycles': [{{ id:'gc001' }}],
  '/api/v1/tasks/t_164/goal-evaluations': [{{ id:'ge001' }}],
}};
global.window = {{ AxiomModules: {{
  state:{{ state:{{}} }}, core:{{ methods:{{}} }}, projects:{{ methods:{{}} }},
  dashboard:{{ methods:{{}} }}, graph:{{ methods:{{}} }}, files:{{ methods:{{}} }},
}} }};
global.location = {{ origin:'http://testserver', hash:'#/tasks/t_164' }};
global.fetch = async (url) => {{
  const path = String(url).replace('http://testserver', '');
  calls.push(path);
  const body = payloads[path];
  return {{
    ok:body !== undefined, status:body !== undefined ? 200 : 404,
    headers:{{ get:() => null }},
    text:async () => JSON.stringify(body !== undefined ? body : {{ detail:'missing mock' }}),
  }};
}};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
const api = window.AxiomModules.core.methods.api;
(async () => {{
  const summary = await api('GET', '/projects/t_164', null, {{ detailMode:'summary' }});
  const summaryCalls = [...calls];
  calls.length = 0;
  const incremental = await api('GET', '/projects/t_164', null, {{
    detailMode:'incremental',
    baseDetail:summary,
    onTaskDetailSection:section => sections.push({{
      name:section.name,
      error:section.error ? String(section.error.message || section.error) : '',
    }}),
  }});
  process.stdout.write(JSON.stringify({{
    summaryCalls,
    summaryShape:{{
      title:summary.project.title,
      facts:summary.facts.length,
      intents:summary.intents.length,
      archivedLocked:summary.archived_locked,
    }},
    detailCalls:calls,
    sections,
    finalShape:{{
      facts:incremental.facts.length,
      intents:incremental.intents.length,
      todos:incremental.todos.length,
      vulns:incremental.vulns.length,
      artifacts:incremental.artifacts.length,
      leads:incremental.lead_artifacts.length,
      cycles:incremental.goal_cycles.length,
      evaluations:incremental.goal_evaluations.length,
    }},
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _run_task_detail_open_order_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    projects = ROOT / "src/axiom/static/js/projects.js"
    program = f"""
const fs = require('fs');
const events = [];
let resolveHydration;
const hydration = new Promise(resolve => {{ resolveHydration = resolve; }});
global.window = {{ AxiomModules: {{ projects:{{ methods:{{}} }} }} }};
global.location = {{ hash:'#/tasks/t_164' }};
global.history = {{ pushState:() => {{}} }};
global.document = {{ hidden:false }};
eval(fs.readFileSync({json.dumps(str(projects))}, 'utf8'));
const methods = window.AxiomModules.projects.methods;
const oldProject = {{ project:{{ id:'t_old', title:'old' }}, facts:[{{ id:'old' }}] }};
const ctx = Object.assign({{}}, methods, {{
  project:oldProject,
  projectDetailsCache:{{}},
  taskDetailLoad:{{ taskId:'', summary:'idle', hydrating:false, sections:{{}}, errors:{{}} }},
  taskDiagnosticsRequested:{{}},
  taskDiagnostics:{{}}, taskDiagnosticsLoading:{{}},
  _projectOpenGeneration:0,
  _pageAbortController:{{ signal:{{ aborted:false }} }},
  replay:{{ active:false }}, polling:true,
  rememberProjectListScroll:() => {{}}, cleanupMountedPage:() => {{}}, showPageSkeleton:() => events.push('skeleton'),
  loadTaskSummary:async id => {{
    events.push('summary');
    ctx.project = {{ project:{{ id, title:'任务 164', archived:false }}, facts:[], intents:[], todos:[], vulns:[], artifacts:[], lead_artifacts:[] }};
    ctx.taskDetailLoad = {{ taskId:id, summary:'ready', hydrating:false, sections:{{}}, errors:{{}} }};
    return ctx.project;
  }},
  mountCurrentPage:async () => {{
    events.push('mount');
    ctx._pageAbortController = {{ signal:{{ aborted:false, mounted:true }} }};
  }},
  hydrateTaskDetails:(id, options) => {{ events.push(options.signal.mounted ? 'hydrate:new-signal' : 'hydrate:old-signal'); return hydration; }},
  loadSeeEvents:async () => {{ events.push('events'); return []; }},
  scheduleTaskDiagnostics:() => events.push('diagnostics:scheduled'),
  clampPanelWidth:value => value, sidePanelWidth:320,
  $nextTick:fn => fn(), initGraph:() => events.push('graph:init'), cy:null,
  view:'human', activePage:'task-detail', selectedProjectId:'',
  seeEvents:[], selectedFacts:[], humanArtifactItems:[],
}});
(async () => {{
  await ctx.openProject('t_164', true, 'human');
  const beforeHydration = [...events];
  resolveHydration();
  await hydration;
  ctx.project = {{ project:{{ id:'t_new' }}, facts:[{{ id:'new' }}] }};
  ctx.selectedProjectId = 't_new';
  ctx._projectOpenGeneration += 1;
  ctx.applyTaskDetailSection?.('t_164', ctx._projectOpenGeneration - 1, {{ name:'graph', data:{{ facts:[{{ id:'late' }}], intents:[] }} }});
  process.stdout.write(JSON.stringify({{
    beforeHydration,
    selectedProjectId:ctx.selectedProjectId,
    factId:ctx.project.facts[0].id,
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _run_task_diagnostics_idle_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    projects = ROOT / "src/axiom/static/js/projects.js"
    program = f"""
const fs = require('fs');
const calls = [];
let idleCallback = null;
global.window = {{
  AxiomModules:{{ projects:{{ methods:{{}} }} }},
  requestIdleCallback:callback => {{ idleCallback = callback; return 7; }},
  cancelIdleCallback:() => {{}},
}};
global.document = {{ hidden:false }};
eval(fs.readFileSync({json.dumps(str(projects))}, 'utf8'));
const methods = window.AxiomModules.projects.methods;
const ctx = Object.assign({{}}, methods, {{
  activePage:'task-detail', selectedProjectId:'t_164', _projectOpenGeneration:3,
  taskDiagnostics:{{}}, taskDiagnosticsLoading:{{}}, taskDiagnosticsRequested:{{}},
  loadTaskDiagnostics:async id => {{ calls.push(id); ctx.taskDiagnostics[id] = {{ ok:true }}; }},
}});
const first = ctx.selectedTaskDiagnostics();
const beforeIdle = calls.length;
(async () => {{
  await idleCallback({{ didTimeout:false, timeRemaining:() => 20 }});
  process.stdout.write(JSON.stringify({{
    first,
    beforeIdle,
    afterIdle:calls.length,
    requested:ctx.taskDiagnosticsRequested.t_164,
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)



def _run_task_detail_edge_cases_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    adapter = ROOT / "src/axiom/static/js/v2-adapter.js"
    program = f"""
const fs = require('fs');
const calls = [];
const sections = [];
let failTodos = true;
const normalTask = {{
  id:'t_165', project_id:'prj_001', title:'任务 165', goal:'验证目标',
  origin:{{ original_input:'AgentPark 小程序' }}, status:'running', phase:'reason',
  archived:false, worker_pool:[], scope:{{ enabled:false, targets:[], exclude:[] }},
}};
const archivedTask = {{ ...normalTask, id:'t_archived', title:'归档任务', archived:true }};
const payloads = {{
  '/api/v1/tasks/t_165': normalTask,
  '/api/v1/tasks/t_archived': archivedTask,
  '/api/v1/projects/prj_001': {{ id:'prj_001', title:'测试项目' }},
  '/api/v1/tasks/t_165/graph': {{ facts:[{{ id:'origin', description:'origin' }}], intents:[] }},
  '/api/v1/tasks/t_165/hints': [],
  '/api/v1/tasks/t_165/todos?limit=200': [{{ id:'td001' }}],
  '/api/v1/tasks/t_165/vulnerabilities?limit=200': [],
  '/api/v1/tasks/t_165/artifacts?limit=12': [],
  '/api/v1/tasks/t_165/artifacts?artifact_type=explore.leads&limit=20': [],
  '/api/v1/tasks/t_165/artifacts?artifact_type=bootstrap.initial_leads&limit=20': [],
  '/api/v1/tasks/t_165/goal-cycles': [],
  '/api/v1/tasks/t_165/goal-evaluations': [],
}};
global.window = {{ AxiomModules: {{
  state:{{ state:{{}} }}, core:{{ methods:{{}} }}, projects:{{ methods:{{}} }},
  dashboard:{{ methods:{{}} }}, graph:{{ methods:{{}} }}, files:{{ methods:{{}} }},
}} }};
global.location = {{ origin:'http://testserver', hash:'#/tasks/t_165' }};
global.fetch = async (url) => {{
  const path = String(url).replace('http://testserver', '');
  calls.push(path);
  if (path === '/api/v1/tasks/t_165/todos?limit=200' && failTodos) {{
    return {{ ok:false, status:503, headers:{{ get:() => null }}, text:async () => JSON.stringify({{ detail:'Todo unavailable' }}) }};
  }}
  const body = payloads[path];
  return {{
    ok:body !== undefined, status:body !== undefined ? 200 : 404,
    headers:{{ get:() => null }},
    text:async () => JSON.stringify(body !== undefined ? body : {{ detail:'missing mock' }}),
  }};
}};
eval(fs.readFileSync({json.dumps(str(adapter))}, 'utf8'));
const api = window.AxiomModules.core.methods.api;
(async () => {{
  const archivedSummary = await api('GET', '/projects/t_archived', null, {{ detailMode:'summary' }});
  calls.length = 0;
  const archivedDetail = await api('GET', '/projects/t_archived', null, {{
    detailMode:'incremental', baseDetail:archivedSummary,
    onTaskDetailSection:section => sections.push({{ archived: true, name:section.name }}),
  }});
  const archivedCalls = [...calls];
  calls.length = 0;
  const summary = await api('GET', '/projects/t_165', null, {{ detailMode:'summary' }});
  const failedDetail = await api('GET', '/projects/t_165', null, {{
    detailMode:'incremental', baseDetail:summary,
    onTaskDetailSection:section => sections.push({{
      archived: false, name:section.name,
      error:section.error ? String(section.error.message || section.error) : '',
    }}),
  }});
  const failedCalls = [...calls];
  failTodos = false;
  calls.length = 0;
  const retried = await api('GET', '/projects/t_165', null, {{
    detailMode:'incremental', baseDetail:failedDetail, sections:['todos'],
    onTaskDetailSection:section => sections.push({{ retry:true, name:section.name }}),
  }});
  process.stdout.write(JSON.stringify({{
    archivedCalls,
    archivedLocked:archivedDetail.archived_locked,
    archivedCallbacks:sections.filter(item => item.archived),
    failedCalls,
    failedTodo:sections.find(item => item.name === 'todos' && !item.retry)?.error || '',
    failedFacts:failedDetail.facts.length,
    retryCalls:calls,
    retryTodo:retried.todos.length,
    retryCallbacks:sections.filter(item => item.retry).map(item => item.name),
  }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _run_task_diagnostics_switch_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    projects = ROOT / "src/axiom/static/js/projects.js"
    program = f"""
const fs = require('fs');
const callbacks = {{}};
const cancelled = [];
const loaded = [];
let nextHandle = 1;
global.window = {{
  AxiomModules:{{ projects:{{ methods:{{}} }} }},
  requestIdleCallback:callback => {{ const handle = nextHandle++; callbacks[handle] = callback; return handle; }},
  cancelIdleCallback:handle => {{ cancelled.push(handle); }},
}};
global.document = {{ hidden:false }};
eval(fs.readFileSync({json.dumps(str(projects))}, 'utf8'));
const methods = window.AxiomModules.projects.methods;
const ctx = Object.assign({{}}, methods, {{
  activePage:'task-detail', selectedProjectId:'t_a', _projectOpenGeneration:1,
  taskDiagnostics:{{}}, taskDiagnosticsLoading:{{}}, taskDiagnosticsRequested:{{}},
  loadTaskDiagnostics:async id => {{ loaded.push(id); ctx.taskDiagnostics[id] = {{ ok:true }}; }},
}});
(async () => {{
  ctx.scheduleTaskDiagnostics('t_a', {{ generation:1 }});
  const afterA = {{ ...ctx.taskDiagnosticsRequested }};
  ctx.selectedProjectId = 't_b';
  ctx._projectOpenGeneration = 2;
  ctx.scheduleTaskDiagnostics('t_b', {{ generation:2 }});
  const afterB = {{ ...ctx.taskDiagnosticsRequested }};
  callbacks[2]({{ didTimeout:false, timeRemaining:() => 20 }});
  await Promise.resolve();
  ctx.selectedProjectId = 't_a';
  ctx._projectOpenGeneration = 3;
  ctx.scheduleTaskDiagnostics('t_a', {{ generation:3 }});
  callbacks[3]({{ didTimeout:false, timeRemaining:() => 20 }});
  await Promise.resolve();
  process.stdout.write(JSON.stringify({{ afterA, afterB, cancelled, loaded, requested:ctx.taskDiagnosticsRequested }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _run_task_view_switch_hydration_fixture() -> dict[str, object]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for frontend behavior tests")

    human = ROOT / "src/axiom/static/js/human.js"
    program = f"""
const fs = require('fs');
const events = [];
let resolveOld;
global.window = {{ AxiomModules:{{ human:{{ methods:{{}} }} }} }};
eval(fs.readFileSync({json.dumps(str(human))}, 'utf8'));
const methods = window.AxiomModules.human.methods;
let ctx;
const oldHydration = new Promise(resolve => {{ resolveOld = resolve; }}).finally(() => {{
  ctx._taskDetailHydrationPromise = null;
  ctx.taskDetailLoad = {{ ...ctx.taskDetailLoad, hydrating:false }};
}});
ctx = Object.assign({{}}, methods, {{
  selectedProjectId:'t_164', _projectOpenGeneration:4, view:'human', project:{{ project:{{ id:'t_164', archived:false }}, archived_locked:false }},
  taskDetailLoad:{{ hydrating:true, sections:{{ graph:'loading', todos:'idle', hints:'ready' }}, errors:{{}} }},
  _taskDetailHydrationPromise:oldHydration,
  showPageSkeleton:() => events.push('skeleton'),
  mountCurrentPage:async () => {{ events.push('mount'); ctx._pageAbortController = {{ signal:{{ name:'new' }} }}; }},
  taskDetailSectionReady:name => ctx.taskDetailLoad.sections[name] === 'ready',
  hydrateTaskDetails:async (id, options) => {{ events.push({{ id, signal:options.signal.name, sections:options.sections }}); }},
  initGraph:() => events.push('graph:init'),
}});
(async () => {{
  const switching = ctx.switchTaskView('graph');
  await Promise.resolve();
  const beforeRelease = [...events];
  resolveOld();
  await switching;
  process.stdout.write(JSON.stringify({{ beforeRelease, after:events }}));
}})();
"""
    result = subprocess.run([node, "-e", program], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)

def test_task_detail_adapter_supports_summary_then_incremental_sections() -> None:
    result = _run_task_detail_loading_fixture()

    assert result["summaryCalls"] == ["/api/v1/tasks/t_164"]
    assert result["summaryShape"] == {
        "title": "任务 164",
        "facts": 0,
        "intents": 0,
        "archivedLocked": False,
    }
    assert "/api/v1/tasks/t_164" not in result["detailCalls"]
    assert {item["name"] for item in result["sections"]} == {
        "project_group",
        "graph",
        "hints",
        "todos",
        "vulnerabilities",
        "artifacts",
        "lead_artifacts",
        "goal_cycles",
        "goal_evaluations",
    }
    assert all(not item["error"] for item in result["sections"])
    assert result["finalShape"] == {
        "facts": 2,
        "intents": 1,
        "todos": 1,
        "vulns": 1,
        "artifacts": 1,
        "leads": 1,
        "cycles": 1,
        "evaluations": 1,
    }


def test_open_project_mounts_after_summary_and_ignores_late_detail_results() -> None:
    result = _run_task_detail_open_order_fixture()

    assert result["beforeHydration"][:4] == [
        "skeleton",
        "summary",
        "mount",
        "hydrate:new-signal",
    ]
    assert "events" in result["beforeHydration"]
    assert "diagnostics:scheduled" in result["beforeHydration"]
    assert result["selectedProjectId"] == "t_new"
    assert result["factId"] == "new"


def test_task_diagnostics_are_requested_on_idle_not_template_evaluation() -> None:
    result = _run_task_diagnostics_idle_fixture()

    assert result["first"] is None
    assert result["beforeIdle"] == 0
    assert result["afterIdle"] == 1
    assert result["requested"] is True


def test_task_detail_incremental_skips_archived_and_isolates_section_failures() -> None:
    result = _run_task_detail_edge_cases_fixture()

    assert result["archivedCalls"] == []
    assert result["archivedLocked"] is True
    assert result["archivedCallbacks"] == []
    assert "Todo unavailable" in result["failedTodo"]
    assert result["failedFacts"] == 1
    assert "/api/v1/tasks/t_165/graph" in result["failedCalls"]
    assert "/api/v1/tasks/t_165/artifacts?limit=12" in result["failedCalls"]


def test_task_detail_retry_requests_only_the_failed_section() -> None:
    result = _run_task_detail_edge_cases_fixture()

    assert result["retryCalls"] == ["/api/v1/tasks/t_165/todos?limit=200"]
    assert result["retryCallbacks"] == ["todos"]
    assert result["retryTodo"] == 1


def test_task_diagnostics_idle_schedule_cleans_cancelled_task_and_can_reschedule() -> None:
    result = _run_task_diagnostics_switch_fixture()

    assert result["afterA"] == {"t_a": True}
    assert result["afterB"] == {"t_b": True}
    assert result["cancelled"] == [1]
    assert result["loaded"] == ["t_b", "t_a"]
    assert result["requested"] == {"t_b": True, "t_a": True}


def test_task_view_switch_restarts_incomplete_hydration_with_new_page_signal() -> None:
    result = _run_task_view_switch_hydration_fixture()

    assert result["beforeRelease"] == ["skeleton", "mount"]
    assert result["after"] == [
        "skeleton",
        "mount",
        {"id": "t_164", "signal": "new", "sections": ["graph", "todos"]},
    ]
