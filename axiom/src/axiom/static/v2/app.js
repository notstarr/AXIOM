(() => {
  const state = {
    page: 'dashboard', loading: true, projects: [], tasks: [], workers: [], runtime: null,
    selectedTask: null, selectedProject: null, taskData: null, projectData: null,
    vulnerabilities: [], settingsTab: 'runtime', graph: null,
  };

  const main = document.querySelector('#app-main');
  const modalRoot = document.querySelector('#modal-root');
  const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const lines = value => String(value || '').split(/[\n,]+/).map(x => x.trim()).filter(Boolean);
  const fmt = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '—';
  const badge = value => `<span class="badge ${escapeHtml(value)}">${escapeHtml(value)}</span>`;
  const projectById = id => state.projects.find(item => item.id === id);
  const taskById = id => state.tasks.find(item => item.id === id);

  async function api(path, options = {}) {
    const response = await fetch(`/api/v1${path}`, {
      headers: {'Content-Type':'application/json', ...(options.headers || {})},
      ...options,
      body: options.body && typeof options.body !== 'string' ? JSON.stringify(options.body) : options.body,
    });
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return response.json();
  }

  function toast(message, type = '') {
    const node = document.createElement('div');
    node.className = `toast ${type}`;
    node.textContent = message;
    document.querySelector('#toast-region').append(node);
    setTimeout(() => node.remove(), 3600);
  }

  function setApiStatus(ok, text) {
    document.querySelector('#api-dot').classList.toggle('ok', ok);
    document.querySelector('#api-status').textContent = text;
  }

  async function loadCore({quiet = false} = {}) {
    if (!quiet) { state.loading = true; render(); }
    try {
      const [projects, tasks, workers, runtime] = await Promise.all([
        api('/projects'), api('/tasks'), api('/settings/workers'), api('/settings/runtime'),
      ]);
      Object.assign(state, {projects, tasks, workers, runtime, loading: false});
      setApiStatus(true, 'API v2');
      if (state.page === 'vulnerabilities') await loadAllVulnerabilities();
      render();
    } catch (error) {
      state.loading = false;
      setApiStatus(false, '连接失败');
      renderError(error);
    }
  }

  function renderError(error) {
    main.innerHTML = `<section class="panel"><div class="panel-body"><div class="error-box">${escapeHtml(error.message)}</div><div class="form-actions"><button class="button" data-action="refresh">重试</button></div></div></section>`;
  }

  function nav(page) {
    state.page = page;
    state.selectedTask = null;
    state.selectedProject = null;
    document.querySelectorAll('[data-nav]').forEach(btn => btn.classList.toggle('active', btn.dataset.nav === page));
    if (page === 'vulnerabilities') loadAllVulnerabilities().then(render).catch(error => toast(error.message, 'error'));
    render();
  }

  function header(eyebrow, title, description, actions = '') {
    return `<header class="page-head"><div><p class="eyebrow">${escapeHtml(eyebrow)}</p><h1>${escapeHtml(title)}</h1><p>${escapeHtml(description)}</p></div><div class="head-actions">${actions}</div></header>`;
  }

  function loadingView() {
    return `${header('system', '正在同步', '从数据库加载 Project、Task、Worker 与运行配置。')}<div class="metric-grid"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>`;
  }

  function render() {
    if (state.loading) { main.innerHTML = loadingView(); return; }
    document.querySelectorAll('[data-nav]').forEach(btn => btn.classList.toggle('active', btn.dataset.nav === state.page || (state.page === 'task-detail' && btn.dataset.nav === 'tasks') || (state.page === 'project-detail' && btn.dataset.nav === 'projects')));
    const views = {
      dashboard: renderDashboard, tasks: renderTasks, projects: renderProjects,
      'task-detail': renderTaskDetail, 'project-detail': renderProjectDetail,
      vulnerabilities: renderVulnerabilities, settings: renderSettings,
    };
    main.innerHTML = (views[state.page] || renderDashboard)();
    if (state.page === 'task-detail' && state.taskData) setTimeout(drawGraph, 0);
  }

  function renderDashboard() {
    const active = state.tasks.filter(t => t.status === 'running');
    const completed = state.tasks.filter(t => t.status === 'completed');
    const legacy = state.tasks.filter(t => t.legacy_readonly);
    const recent = [...state.tasks].sort((a,b) => String(b.updated_at).localeCompare(String(a.updated_at))).slice(0, 8);
    return `${header('control plane', '测试态势', '以 Goal 为完成条件，Bootstrap、Reason 与 Explore 的状态全部来自 Server。', '<button class="button primary" data-action="new-task">新建 Task</button>')}
      <section class="metric-grid">
        <article class="metric featured"><div class="metric-label">运行中的 Task</div><div class="metric-value">${active.length}</div><div class="metric-note">${active.map(t => `${t.id} · ${t.phase}`).slice(0,3).join(' / ') || '当前没有执行中的 Task'}</div></article>
        <article class="metric"><div class="metric-label">Project</div><div class="metric-value">${state.projects.length}</div><div class="metric-note">知识与凭证的持久容器</div></article>
        <article class="metric"><div class="metric-label">已完成</div><div class="metric-value">${completed.length}</div><div class="metric-note">仅在 Goal 已被证明时完成</div></article>
        <article class="metric"><div class="metric-label">历史只读</div><div class="metric-value">${legacy.length}</div><div class="metric-note">迁移记录不可恢复执行</div></article>
      </section>
      <section class="split">
        <article class="panel flush"><div class="panel-header"><h2>最近 Task</h2><button class="button small" data-nav="tasks">查看全部</button></div>${taskTable(recent)}</article>
        <article class="panel"><div class="panel-header"><h2>Dispatcher</h2>${badge(`${state.workers.filter(w=>!w.disabled).length} workers`)}</div><div class="panel-body">
          <div class="knowledge-list">${state.workers.slice(0,6).map(w => `<div class="knowledge-item"><h4>${escapeHtml(w.name)} ${w.disabled ? badge('disabled') : badge('active')}</h4><p>${escapeHtml(w.capabilities.join(' · '))} · 槽位 ${w.max_running}${w.cooldown_until ? ` · 冷却至 ${fmt(w.cooldown_until)}` : ''}</p></div>`).join('') || empty('尚未配置 Worker','在设置中添加 PI Worker。')}</div>
        </div></article>
      </section>`;
  }

  function taskTable(tasks) {
    if (!tasks.length) return empty('没有 Task', '创建 Task 后会从 Bootstrap 开始运行。');
    return `<div class="table-wrap"><table><thead><tr><th>ID</th><th>Task</th><th>Project</th><th>状态</th><th>阶段</th><th>Origin</th><th></th></tr></thead><tbody>${tasks.map(t => `<tr>
      <td class="mono">${escapeHtml(t.id)}</td><td><strong>${escapeHtml(t.title)}</strong>${t.legacy_readonly ? ' '+badge('legacy') : ''}</td>
      <td>${escapeHtml(projectById(t.project_id)?.title || t.project_id)}</td><td>${badge(t.status)}</td><td>${badge(t.phase)}</td>
      <td class="mono truncate">${escapeHtml(t.origin?.original_input || t.origin?.host || '')}</td>
      <td><button class="button small" data-action="open-task" data-id="${escapeHtml(t.id)}">打开</button></td></tr>`).join('')}</tbody></table></div>`;
  }

  function renderTasks() {
    const running = state.tasks.filter(t => t.status === 'running').length;
    return `${header('task orchestration', 'Task', '每次渗透测试是一条独立 Task；Scope 由显式黑白名单启用，并同时约束 Intent Target 与支持范围校验的网络工具。', '<button class="button primary" data-action="new-task">新建 Task</button>')}
      <section class="panel flush"><div class="panel-header"><h2>全部 Task</h2><span class="muted mono">${running} running / ${state.tasks.length} total</span></div>${taskTable(state.tasks)}</section>`;
  }

  function renderProjects() {
    return `${header('durable context', 'Project', 'Project 保存可复用的知识、明文凭证与多次 Task。这里不再保存资产库。', '<button class="button primary" data-action="new-project">新建 Project</button>')}
      <section class="cards">${state.projects.map(project => {
        const tasks = state.tasks.filter(t => t.project_id === project.id);
        return `<article class="project-card"><div>${badge(project.status)}</div><h3>${escapeHtml(project.title)}</h3><p>${escapeHtml(project.description || '暂无描述')}</p><footer><span class="mono muted">${project.id} · ${tasks.length} tasks</span><button class="button small" data-action="open-project" data-id="${project.id}">管理</button></footer></article>`;
      }).join('') || empty('没有 Project', '先创建 Project，再创建测试 Task。')}</section>`;
  }

  async function openTask(id) {
    state.page = 'task-detail'; state.selectedTask = taskById(id); state.taskData = null; render();
    try {
      const [graph, todos, artifacts, vulnerabilities, audit, hints] = await Promise.all([
        api(`/tasks/${id}/graph`), api(`/tasks/${id}/todos`), api(`/tasks/${id}/artifacts`),
        api(`/tasks/${id}/vulnerabilities`), api(`/tasks/${id}/audit-events?limit=100`), api(`/tasks/${id}/hints`),
      ]);
      state.taskData = {graph, todos, artifacts, vulnerabilities, audit, hints, tab:'graph'};
      render();
    } catch (error) { toast(error.message, 'error'); }
  }

  function renderTaskDetail() {
    const task = state.selectedTask;
    if (!task) return empty('未选择 Task', '返回 Task 列表选择一项。');
    if (!state.taskData) return `${header('task', task.title, `${task.id} · ${task.origin.original_input}`,'<button class="button" data-nav="tasks">返回</button>')}<div class="skeleton"></div>`;
    const d = state.taskData;
    return `${header('task detail', task.title, `${task.goal}`, `<button class="button" data-action="edit-scope">Scope</button>${!task.legacy_readonly && task.status === 'running' ? '<button class="button danger" data-action="stop-task">停止</button>' : ''}${!task.legacy_readonly && task.status === 'stopped' ? '<button class="button primary" data-action="resume-task">恢复</button>' : ''}<button class="button" data-nav="tasks">返回</button>`)}
      <div class="panel" style="margin-bottom:1rem"><div class="panel-body check-row"><span class="mono">${task.id}</span>${badge(task.status)}${badge(task.phase)}<span class="muted">Origin</span><span>${escapeHtml(task.origin.original_input)}</span><span class="muted">Scope</span><span>${task.scope.enabled ? escapeHtml([task.scope.targets.length ? `白名单：${task.scope.targets.join(', ')}` : '', task.scope.exclude.length ? `黑名单：${task.scope.exclude.join(', ')}` : ''].filter(Boolean).join('；')) : '未配置'}</span>${task.guard_enabled ? badge('guard') : ''}</div></div>
      <section class="task-layout">
        <article class="panel graph-panel"><div id="task-graph"></div><div class="graph-legend"><span>● Fact</span><span>◆ Intent</span><span>线表示 from/to</span></div></article>
        <aside class="panel todo-panel"><div class="panel-header"><h2>Todo list</h2>${!task.legacy_readonly ? '<button class="button small" data-action="new-todo">添加</button>' : ''}</div><div class="tabs"><button class="active" data-action="task-tab" data-tab="todo">Todo</button><button data-action="task-tab" data-tab="artifacts">Artifacts</button><button data-action="task-tab" data-tab="vulns">Vulns</button><button data-action="task-tab" data-tab="audit">Audit</button></div><div class="todo-list">${todoList(d.todos, task.legacy_readonly)}</div></aside>
      </section>`;
  }

  function todoList(todos, readonly) {
    if (!todos.length) return `<div class="todo-empty"><strong>尚无 Todo</strong><br>Reason 会持续维护攻击面与深入方向。</div>`;
    return [...todos].sort((a,b) => b.priority-a.priority).map(todo => `<article class="todo-item ${todo.status}"><div class="todo-top"><div class="todo-title">${escapeHtml(todo.title)}</div><span class="priority">P${todo.priority}</span></div><div class="todo-meta">${badge(todo.status)}${badge(todo.category)}${todo.theme ? badge(todo.theme) : ''}</div>${todo.status_reason ? `<p class="help">${escapeHtml(todo.status_reason)}</p>` : ''}${readonly ? '' : `<div class="todo-actions"><button class="button small" data-action="todo-status" data-id="${todo.id}" data-status="partial">部分</button><button class="button small" data-action="todo-status" data-id="${todo.id}" data-status="blocked">阻塞</button><button class="button small" data-action="todo-status" data-id="${todo.id}" data-status="completed">完成</button><button class="button small" data-action="todo-status" data-id="${todo.id}" data-status="open">开放</button></div>`}</article>`).join('');
  }

  function renderTaskTab(tab) {
    const d = state.taskData; if (!d) return;
    const list = document.querySelector('.todo-list');
    document.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab || (tab === 'todo' && !b.dataset.tab)));
    if (tab === 'artifacts') list.innerHTML = `<div class="artifact-list">${d.artifacts.map(a => `<article class="artifact"><h4>${escapeHtml(a.artifact_type)} · ${a.id}</h4><p>${escapeHtml(a.summary || a.path)}\n${escapeHtml(a.path)}</p></article>`).join('') || empty('没有 Artifact','Bootstrap/Explore 必须注册阶段产物。')}</div>`;
    else if (tab === 'vulns') list.innerHTML = d.vulnerabilities.map(v => `<article class="todo-item"><div class="todo-title">${escapeHtml(v.title)}</div><div class="todo-meta">${badge(v.severity)}${badge(v.status)}</div></article>`).join('') || empty('没有漏洞','只记录有证据的确认结果。');
    else if (tab === 'audit') list.innerHTML = d.audit.map(e => `<article class="todo-item"><div class="todo-title">${escapeHtml(e.message)}</div><div class="todo-meta">${badge(e.level)}${badge(e.event_type)}</div><p class="help">${fmt(e.created_at)} · ${escapeHtml(e.worker || 'system')}</p></article>`).join('') || empty('没有审计事件','Dispatcher 与 Guard 的历史会写入这里。');
    else list.innerHTML = todoList(d.todos, state.selectedTask.legacy_readonly);
  }

  function drawGraph() {
    const container = document.querySelector('#task-graph'); if (!container || !state.taskData) return;
    if (state.graph) state.graph.destroy();
    const {facts, intents} = state.taskData.graph;
    const nodes = [
      ...facts.map(f => ({data:{id:`f:${f.id}`, label:`${f.id}\n${f.description}`, kind:'fact'}})),
      ...intents.map(i => ({data:{id:`i:${i.id}`, label:`${i.id}\n${i.description}`, kind:'intent', status:i.status}})),
    ];
    const edges = [];
    intents.forEach(i => {
      i.from_facts.forEach(fid => edges.push({data:{id:`${fid}-${i.id}`,source:`f:${fid}`,target:`i:${i.id}`}}));
      if (i.to_fact_id) edges.push({data:{id:`${i.id}-${i.to_fact_id}`,source:`i:${i.id}`,target:`f:${i.to_fact_id}`}});
    });
    state.graph = cytoscape({container, elements:[...nodes,...edges], layout:{name:'dagre', rankDir:'LR', nodeSep:38, rankSep:90, padding:30}, style:[
      {selector:'node',style:{'label':'data(label)','text-wrap':'wrap','text-max-width':190,'font-size':10,'font-family':'ui-monospace, monospace','background-color':'#faf9f5','border-width':1.5,'border-color':'#aeb4aa','shape':'round-rectangle','width':210,'height':70,'padding':10,'color':'#30332e','text-valign':'center','text-halign':'center'}},
      {selector:'node[kind="intent"]',style:{'shape':'diamond','width':110,'height':110,'background-color':'#3f6754','border-color':'#315140','color':'#fff','text-max-width':95}},
      {selector:'node[status="failed"]',style:{'background-color':'#a54a43'}},{selector:'node[status="cancelled"]',style:{'opacity':.5}},
      {selector:'edge',style:{'width':1.2,'line-color':'#aeb4aa','target-arrow-color':'#aeb4aa','target-arrow-shape':'triangle','curve-style':'bezier'}}
    ]});
  }

  async function openProject(id) {
    state.page = 'project-detail'; state.selectedProject = projectById(id); state.projectData = null; render();
    try {
      const [knowledge, credentials] = await Promise.all([api(`/projects/${id}/knowledge`), api(`/projects/${id}/credentials`)]);
      state.projectData = {knowledge, credentials}; render();
    } catch (error) { toast(error.message, 'error'); }
  }

  function renderProjectDetail() {
    const p = state.selectedProject; if (!p) return empty('未选择 Project','返回 Project 列表。');
    if (!state.projectData) return `${header('project',p.title,p.description,'<button class="button" data-nav="projects">返回</button>')}<div class="skeleton"></div>`;
    const tasks = state.tasks.filter(t => t.project_id === p.id);
    return `${header('project context', p.title, p.description || 'Project 级知识与凭证会被该 Project 下的 Worker 按需读取。', '<button class="button primary" data-action="new-project-task">新建 Task</button><button class="button" data-nav="projects">返回</button>')}
      <section class="split"><div>
        <article class="panel flush" style="margin-bottom:1rem"><div class="panel-header"><h2>Task</h2><span class="mono muted">${tasks.length}</span></div>${taskTable(tasks)}</article>
        <article class="panel"><div class="panel-header"><h2>Project Knowledge</h2><button class="button small" data-action="new-knowledge">添加</button></div><div class="panel-body knowledge-list">${state.projectData.knowledge.map(k => `<article class="knowledge-item"><h4>${escapeHtml(k.id)} · ${escapeHtml(k.title || 'Knowledge')}</h4><p>${escapeHtml(k.content)}${k.hint ? `\nHint: ${escapeHtml(k.hint)}` : ''}</p></article>`).join('') || empty('没有知识','保存跨 Task 复用的背景与提示。')}</div></article>
      </div><aside class="panel"><div class="panel-header"><h2>Credentials</h2><button class="button small" data-action="new-credential">添加</button></div><div class="panel-body knowledge-list">${state.projectData.credentials.map(c => `<article class="knowledge-item"><h4>${escapeHtml(c.name)} ${badge(c.enabled ? 'enabled' : 'disabled')}</h4><p>${escapeHtml(c.notes || '暂无备注')}\n${escapeHtml(c.refresh_id ? `刷新方式：${c.refresh_id}` : '未配置刷新方式')}</p></article>`).join('') || empty('没有凭证','凭证明文保存在数据库，Worker 通过 CLI 渐进读取。')}</div></aside></section>`;
  }

  async function loadAllVulnerabilities() {
    const groups = await Promise.all(state.tasks.map(async task => {
      try { return (await api(`/tasks/${task.id}/vulnerabilities`)).map(v => ({...v, task})); } catch (_) { return []; }
    }));
    state.vulnerabilities = groups.flat();
  }

  function renderVulnerabilities() {
    return `${header('evidence first', 'Vulnerabilities', '内部使用通用漏洞模型，证据优先记录与审核。', '<button class="button primary" data-action="new-vulnerability">手动记录</button>')}
      <section class="panel flush"><div class="panel-header"><h2>漏洞记录</h2><span class="mono muted">${state.vulnerabilities.length}</span></div>${state.vulnerabilities.length ? `<div class="table-wrap"><table><thead><tr><th>ID</th><th>标题</th><th>严重度</th><th>Task</th><th>状态</th></tr></thead><tbody>${state.vulnerabilities.map(v => `<tr><td class="mono">${v.id}</td><td><strong>${escapeHtml(v.title)}</strong><div class="muted truncate">${escapeHtml(v.affected_target || v.description)}</div></td><td><span class="severity ${v.severity}">${escapeHtml(v.severity)}</span></td><td class="mono">${escapeHtml(v.task_id)}</td><td>${badge(v.status)}</td></tr>`).join('')}</tbody></table></div>` : empty('没有漏洞','Explore 或人工表单可以写入确认漏洞。')}</section>`;
  }

  function renderSettings() {
    const tabs = [['runtime','运行时'],['workers','Worker'],['guard','Guard'],['dingtalk','钉钉告警']];
    return `${header('database backed', 'Settings', '所有新运行配置以数据库为唯一来源；Worker 自定义 MCP 继续保存在 Worker config 中。')}
      <section class="settings-grid"><nav class="settings-nav">${tabs.map(([id,label]) => `<button class="${state.settingsTab===id?'active':''}" data-action="settings-tab" data-tab="${id}">${label}</button>`).join('')}</nav><div>${renderSettingsContent()}</div></section>`;
  }

  function renderSettingsContent() {
    const cfg = state.runtime?.config || {};
    if (state.settingsTab === 'workers') return `<article class="panel"><div class="panel-header"><h2>Worker</h2><button class="button small primary" data-action="new-worker">添加 Worker</button></div><div class="panel-body">${state.workers.map(w => `<div class="worker-row"><div><strong>${escapeHtml(w.name)}</strong> ${w.disabled?badge('disabled'):badge('active')}<p class="help">${escapeHtml(w.capabilities.join(' · '))} · max ${w.max_running} · ${escapeHtml(w.worker_type)}</p></div><button class="button small" data-action="edit-worker" data-id="${escapeHtml(w.name)}">编辑</button></div>`).join('') || empty('没有 Worker','Dispatcher 无法执行阶段，先添加 Worker。')}</div></article>`;
    if (state.settingsTab === 'guard') return settingsForm('guard-settings-form', 'Guard', [
      ['guard_mode','模式','select',['off','observe','guarded','enforce']],['guard_model_base_url','模型 Base URL'],['guard_model_name','模型'],['guard_api_key','API Key','password'],['guard_timeout_seconds','超时秒数','number']
    ], cfg, 'Guard 只监管外部操作工具；硬规则阻断，模型错误或超时 fail-open。');
    if (state.settingsTab === 'dingtalk') return `<article class="panel"><div class="panel-header"><h2>钉钉告警与指令交互</h2><span class="mono muted">v${state.runtime?.version || 1}</span></div><form id="dingtalk-settings-form" class="panel-body"><p class="help">配置钉钉自定义机器人 Webhook 与加签 Secret。漏洞发现时将实时推送初报，评审完毕时推送裁决理由；支持在钉钉群发送 /help 进行状态查询与启停控制。</p><div class="form-grid" style="margin-top:1rem">
      <div class="field full"><label class="check"><input type="checkbox" name="dingtalk_enabled" ${cfg.dingtalk_enabled?'checked':''}> 启用钉钉通知与指令交互</label></div>
      <div class="field full"><label>Webhook URL</label><input name="dingtalk_webhook_url" type="text" placeholder="https://oapi.dingtalk.com/robot/send?access_token=..." value="${escapeHtml(cfg.dingtalk_webhook_url || '')}"></div>
      <div class="field full"><label>加签 Secret (可选)</label><input name="dingtalk_secret" type="password" placeholder="SEC..." value="${escapeHtml(cfg.dingtalk_secret || '')}"></div>
    </div><div class="form-actions" style="display:flex;gap:0.75rem"><button class="button primary" type="submit">保存配置</button><button class="button" type="button" data-action="test-dingtalk">发送测试通知</button></div></form></article>`;
    return settingsForm('runtime-settings-form','Dispatcher runtime', [
      ['interval_seconds','调度间隔（秒）','number'],['max_workers','全局 Worker 槽位','number'],['max_running_tasks','并行 Task 数','number'],['max_task_workers','单 Task Worker 数','number'],['bootstrap_limit','Bootstrap 并发','number'],['reason_limit','Reason 并发','number'],['explore_limit','Explore 并发','number'],['heartbeat_interval_seconds','心跳间隔','number'],['lease_timeout_seconds','租约超时','number'],['retry_base_seconds','重试基准秒数','number'],['bootstrap_execute_seconds','Bootstrap 探索预算','number'],['reason_execute_seconds','Reason 规划预算','number'],['reason_conclude_seconds','Reason 收口预算','number'],['plan_execute_seconds','Plan 全景预算','number'],['plan_conclude_seconds','Plan 收口预算','number'],['reason_batch_size','Reason 结果批量数','number'],['reason_low_watermark','Reason 队列低水位','number'],['reason_max_wait_seconds','Reason 最大等待秒数','number'],['reason_min_interval_seconds','Reason 最短间隔秒数','number'],['reason_target_open_intents','目标未完成 Intent','number'],['reason_queue_limit','Reason 最大排队数','number'],['plan_queue_limit','Plan 最大排队数','number']
    ], cfg, '并发槽位从 Task/Intent 租约推导；每个 Explore Intent 的时间预算由 Reason 派发时单独指定。');
  }

  function settingsForm(id, title, fields, cfg, helpText) {
    const controls = fields.map(([key, label, type = 'text', options]) => {
      const control = type === 'select'
        ? `<select name="${key}">${options.map(option => `<option ${cfg[key] === option ? 'selected' : ''}>${escapeHtml(option)}</option>`).join('')}</select>`
        : `<input name="${key}" type="${type}" value="${escapeHtml(cfg[key] ?? '')}">`;
      return `<div class="field"><label>${escapeHtml(label)}</label>${control}</div>`;
    }).join('');
    return `<article class="panel"><div class="panel-header"><h2>${escapeHtml(title)}</h2><span class="mono muted">v${state.runtime?.version || 1}</span></div><form id="${id}" class="panel-body"><p class="help">${escapeHtml(helpText)}</p><div class="form-grid" style="margin-top:1rem">${controls}</div><div class="form-actions"><button class="button primary" type="submit">保存</button></div></form></article>`;
  }

  function empty(title, text) { return `<div class="empty"><strong>${escapeHtml(title)}</strong>${escapeHtml(text)}</div>`; }

  function showModal(title, content) {
    modalRoot.innerHTML = `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}" onclick="event.stopPropagation()"><header class="modal-head"><h2>${escapeHtml(title)}</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">×</button></header><div class="modal-body">${content}</div></section></div>`;
    setTimeout(() => modalRoot.querySelector('input,textarea,select')?.focus(), 0);
  }
  const closeModal = () => { modalRoot.innerHTML = ''; };

  function newProjectModal() {
    showModal('新建 Project', `<form id="project-form"><div class="form-grid"><div class="field full"><label>名称</label><input name="title" required></div><div class="field full"><label>描述</label><textarea name="description"></textarea></div></div><div class="form-actions"><button class="button" type="button" data-action="close-modal">取消</button><button class="button primary">创建</button></div></form>`);
  }

  function newTaskModal(projectId = '') {
    const workerChecks = state.workers.map(w => `<label class="check"><input type="checkbox" name="worker_pool" value="${escapeHtml(w.name)}">${escapeHtml(w.name)}</label>`).join('');
    showModal('新建 Task', `<form id="task-form"><div class="form-grid">
      <div class="field"><label>Project</label><select name="project_id" required><option value="">请选择</option>${state.projects.map(p=>`<option value="${p.id}" ${p.id===projectId?'selected':''}>${escapeHtml(p.title)} · ${p.id}</option>`).join('')}</select></div>
      <div class="field"><label>Task 名称</label><input name="title" required></div>
      <div class="field full"><label>测试入口 / 初始信息</label><textarea name="origin" placeholder="网站 URL、小程序 AppID 与业务说明、App 安装包或 HAR 文件信息" required></textarea></div>
      <div class="field full"><label>Goal</label><textarea name="goal" required></textarea></div>
      <div class="field full"><label>Hints（每行一条）</label><textarea name="hints" placeholder="补充业务背景、禁止事项或优先方向"></textarea></div>
      <div class="field full"><label>Task Scope 白名单（可选；填写任一名单即启用）</label><textarea name="scope_targets" placeholder="每行或逗号分隔；子域可写 *.example.com"></textarea></div>
      <div class="field full"><label>Task Scope 黑名单（可选；黑名单优先）</label><textarea name="scope_exclude"></textarea></div>
      <div class="field full"><label>Scope 备注</label><input name="scope_notes"></div>
      <div class="field full"><label>Worker pool</label><div class="check-row">${workerChecks || '<span class="help">未配置 Worker 时留空</span>'}</div></div>
      <div class="field full"><div class="check-row"><label class="check"><input type="checkbox" name="guard_enabled">启用 Guard</label></div></div>
    </div><div class="form-actions"><button class="button" type="button" data-action="close-modal">取消</button><button class="button primary">创建并运行</button></div></form>`);
  }

  function todoModal() {
    showModal('添加 Todo', `<form id="todo-form"><div class="form-grid"><div class="field full"><label>标题</label><input name="title" required></div><div class="field"><label>Category</label><select name="category">${['recon','authentication','authorization','injection','business_logic','file','network','configuration','client_side','other'].map(x=>`<option>${x}</option>`).join('')}</select></div><div class="field"><label>Priority 0–100</label><input type="number" min="0" max="100" name="priority" value="50"></div><div class="field full"><label>Theme</label><input name="theme"></div></div><p class="help">人工 Todo 默认引用 Origin；Reason 后续可继续更新状态或派发 Intent。</p><div class="form-actions"><button class="button" type="button" data-action="close-modal">取消</button><button class="button primary">添加</button></div></form>`);
  }

  function scopeModal() {
    const s = state.selectedTask.scope;
    showModal('Task Scope', `<form id="scope-form"><div class="form-grid"><div class="field full"><p class="help">填写任一名单即启用；两者都为空时无 Scope。</p></div><div class="field full"><label>白名单</label><textarea name="targets">${escapeHtml(s.targets.join('\n'))}</textarea></div><div class="field full"><label>黑名单</label><textarea name="exclude">${escapeHtml(s.exclude.join('\n'))}</textarea></div><div class="field full"><label>备注</label><textarea name="notes">${escapeHtml(s.notes)}</textarea></div></div><div class="form-actions"><button class="button primary">保存</button></div></form>`);
  }

  function knowledgeModal() { showModal('添加 Project Knowledge', `<form id="knowledge-form"><div class="form-grid"><div class="field full"><label>标题</label><input name="title"></div><div class="field full"><label>内容</label><textarea name="content" required></textarea></div><div class="field full"><label>给模型的提示</label><textarea name="hint"></textarea></div></div><div class="form-actions"><button class="button primary">保存</button></div></form>`); }
  function credentialModal() { showModal('添加 Credential', `<form id="credential-form"><div class="form-grid"><div class="field full"><label>名称</label><input name="name" maxlength="128" required></div><div class="field full"><label>凭证内容（明文，格式不限）</label><textarea name="content" required></textarea></div><div class="field full"><label>备注（可选）</label><textarea name="notes"></textarea></div><div class="field full"><label>凭证如何刷新（可选，格式不限）</label><textarea name="refresh_content"></textarea></div><div class="field full"><label class="check"><input type="checkbox" name="enabled" checked>启用此凭证</label></div></div><div class="form-actions"><button class="button primary">保存</button></div></form>`); }

  function workerModal(worker = null) {
    const env = worker?.config?.env || {};
    const defaultCapabilities = ['bootstrap', 'reason', 'explore'];
    const capabilities = ['bootstrap', 'reason', 'explore', 'plan', 'review'];
    showModal(worker ? '编辑 Worker' : '添加 Worker', `<form id="worker-form"><input type="hidden" name="original_name" value="${escapeHtml(worker?.name || '')}"><div class="form-grid"><div class="field"><label>Name</label><input name="name" value="${escapeHtml(worker?.name || '')}" ${worker?'readonly':''} required></div><div class="field"><label>最大并发</label><input type="number" min="1" name="max_running" value="${worker?.max_running || 1}"></div><div class="field"><label>Priority</label><input type="number" min="0" name="priority" value="${worker?.priority || 0}"></div><div class="field"><label>Capabilities</label><div class="check-row">${capabilities.map(c=>`<label class="check"><input type="checkbox" name="capabilities" value="${c}" ${(worker ? worker.capabilities.includes(c) : defaultCapabilities.includes(c))?'checked':''}>${c}</label>`).join('')}</div></div><div class="field full"><label>Worker config JSON（env 与自定义 MCP 独立保存在这里）</label><textarea name="config" style="min-height:220px">${escapeHtml(JSON.stringify(worker?.config || {env:{PI_MODEL:'',PI_BASE_URL:'',PI_PROVIDER_API:'openai-completions'}}, null, 2))}</textarea></div><div class="field full"><label class="check"><input type="checkbox" name="disabled" ${worker?.disabled?'checked':''}>Disabled</label></div></div><div class="form-actions"><button class="button primary">保存</button></div></form>`);
  }

  function vulnerabilityModal() {
    showModal('手动记录漏洞', `<form id="vulnerability-form"><div class="form-grid"><div class="field"><label>Task</label><select name="task_id" required>${state.tasks.filter(t=>!t.legacy_readonly).map(t=>`<option value="${t.id}">${escapeHtml(t.title)} · ${t.id}</option>`).join('')}</select></div><div class="field"><label>Severity</label><select name="severity">${['critical','high','medium','low','info'].map(x=>`<option>${x}</option>`).join('')}</select></div><div class="field full"><label>标题</label><input name="title" required></div><div class="field"><label>类型</label><input name="type" value="其他" required></div><div class="field"><label>受影响目标</label><input name="affected_target"></div><div class="field full"><label>描述</label><textarea name="description" required></textarea></div><div class="field full"><label>影响</label><textarea name="impact" required></textarea></div><div class="field full"><label>利用前提</label><textarea name="prerequisites"></textarea></div><div class="field full"><label>修复建议</label><textarea name="remediation"></textarea></div></div><div class="form-actions"><button class="button primary">记录</button></div></form>`);
  }

  async function submitForm(form) {
    const data = new FormData(form);
    if (form.id === 'project-form') { await api('/projects',{method:'POST',body:{title:data.get('title'),description:data.get('description')}}); closeModal(); await loadCore({quiet:true}); toast('Project 已创建'); }
    else if (form.id === 'task-form') { const origin = data.get('origin'); const targets=lines(data.get('scope_targets')); const exclude=lines(data.get('scope_exclude')); await api('/tasks',{method:'POST',body:{project_id:data.get('project_id'),title:data.get('title'),origin,goal:data.get('goal'),hints:lines(data.get('hints')),guard_enabled:data.has('guard_enabled'),worker_pool:data.getAll('worker_pool'),scope:{enabled:targets.length>0||exclude.length>0,targets,exclude,notes:data.get('scope_notes')}}}); closeModal(); await loadCore({quiet:true}); toast('Task 已创建并进入 Bootstrap'); }
    else if (form.id === 'todo-form') { await api(`/tasks/${state.selectedTask.id}/todos`,{method:'POST',body:{title:data.get('title'),category:data.get('category'),theme:data.get('theme'),priority:Number(data.get('priority')),sources:[{type:'origin',id:'origin'}],actor:'human'}}); closeModal(); await openTask(state.selectedTask.id); toast('Todo 已添加'); }
    else if (form.id === 'scope-form') { const targets=lines(data.get('targets')); const exclude=lines(data.get('exclude')); const updated=await api(`/tasks/${state.selectedTask.id}/scope`,{method:'PATCH',body:{enabled:targets.length>0||exclude.length>0,targets,exclude,notes:data.get('notes')}}); state.selectedTask=updated; state.tasks=state.tasks.map(t=>t.id===updated.id?updated:t); closeModal(); render(); toast('Task Scope 已保存'); }
    else if (form.id === 'knowledge-form') { await api(`/projects/${state.selectedProject.id}/knowledge`,{method:'POST',body:{title:data.get('title'),content:data.get('content'),hint:data.get('hint'),actor:'human'}}); closeModal(); await openProject(state.selectedProject.id); toast('Project Knowledge 已保存'); }
    else if (form.id === 'credential-form') { await api(`/projects/${state.selectedProject.id}/credentials`,{method:'POST',body:{name:data.get('name'),content:data.get('content'),notes:data.get('notes') || null,refresh_content:data.get('refresh_content') || null,enabled:data.has('enabled')}}); closeModal(); await openProject(state.selectedProject.id); toast('Credential 已保存'); }
    else if (form.id === 'worker-form') { let config; try { config=JSON.parse(data.get('config')); } catch (_) { throw new Error('Worker config JSON 格式无效'); } const name=data.get('name'); await api(`/settings/workers/${encodeURIComponent(name)}`,{method:'PUT',body:{name,worker_type:'pi',capabilities:data.getAll('capabilities'),max_running:Number(data.get('max_running')),priority:Number(data.get('priority')),config,disabled:data.has('disabled')}}); closeModal(); await loadCore({quiet:true}); state.settingsTab='workers'; render(); toast('Worker 已保存'); }
    else if (form.id === 'vulnerability-form') { const taskId=data.get('task_id'); await api(`/tasks/${taskId}/vulnerabilities`,{method:'POST',body:{intent_id:null,title:data.get('title'),type:data.get('type'),severity:data.get('severity'),description:data.get('description'),prerequisites:data.get('prerequisites'),impact:data.get('impact'),remediation:data.get('remediation'),affected_target:data.get('affected_target'),status:'待验证',source:'human',reported_by:'human',finding_artifact_id:null,evidence_artifact_ids:[]}}); closeModal(); await loadAllVulnerabilities(); render(); toast('漏洞已记录'); }
    else if (form.id.endsWith('settings-form')) {
      const cfg={...state.runtime.config};
      for (const [key,val] of data.entries()) {
        if (form.elements[key]?.type==='number') cfg[key]=Number(val);
        else if (form.elements[key]?.type==='checkbox') cfg[key]=true;
        else cfg[key]=val;
      }
      // 显式处理 checkbox 未勾选情况
      if (form.id === 'dingtalk-settings-form') {
        cfg.dingtalk_enabled = form.elements['dingtalk_enabled']?.checked || false;
      }
      state.runtime=await api('/settings/runtime?actor=human',{method:'PUT',body:cfg});
      render();
      toast('设置已保存');
    }
  }

  document.addEventListener('click', async event => {
    const target = event.target.closest('[data-nav],[data-action]'); if (!target) return;
    if (target.dataset.nav) { nav(target.dataset.nav); return; }
    const action = target.dataset.action;
    try {
      if (action === 'refresh') await loadCore();
      else if (action === 'close-modal') closeModal();
      else if (action === 'new-project') newProjectModal();
      else if (action === 'new-task') newTaskModal();
      else if (action === 'new-project-task') newTaskModal(state.selectedProject.id);
      else if (action === 'open-task') await openTask(target.dataset.id);
      else if (action === 'open-project') await openProject(target.dataset.id);
      else if (action === 'new-todo') todoModal();
      else if (action === 'edit-scope') scopeModal();
      else if (action === 'new-knowledge') knowledgeModal();
      else if (action === 'new-credential') credentialModal();
      else if (action === 'new-worker') workerModal();
      else if (action === 'edit-worker') workerModal(state.workers.find(w=>w.name===target.dataset.id));
      else if (action === 'new-vulnerability') vulnerabilityModal();
      else if (action === 'settings-tab') { state.settingsTab=target.dataset.tab; render(); }
      else if (action === 'task-tab') renderTaskTab(target.dataset.tab);
      else if (action === 'todo-status') { await api(`/tasks/${state.selectedTask.id}/todos/${target.dataset.id}`,{method:'PATCH',body:{status:target.dataset.status,status_reason:`human marked ${target.dataset.status}`,actor:'human'}}); await openTask(state.selectedTask.id); }
      else if (action === 'test-dingtalk') {
        const res = await api('/integrations/dingtalk/test', { method: 'POST' });
        toast(res.message || '测试通知已发送');
      }
      else if (action === 'stop-task') { await api(`/tasks/${state.selectedTask.id}/stop`,{method:'POST'}); await loadCore({quiet:true}); await openTask(state.selectedTask.id); toast('Task 已停止'); }
      else if (action === 'resume-task') { await api(`/tasks/${state.selectedTask.id}/resume`,{method:'POST'}); await loadCore({quiet:true}); await openTask(state.selectedTask.id); toast('Task 已恢复'); }
    } catch (error) { toast(error.message,'error'); }
  });

  document.addEventListener('submit', async event => {
    if (!event.target.matches('form')) return;
    event.preventDefault();
    const button=event.target.querySelector('[type="submit"],button:not([type])'); if (button) button.disabled=true;
    try { await submitForm(event.target); } catch(error) { toast(error.message,'error'); }
    finally { if (button) button.disabled=false; }
  });

  document.querySelector('#refresh-button').addEventListener('click', () => loadCore());
  loadCore();
})();
