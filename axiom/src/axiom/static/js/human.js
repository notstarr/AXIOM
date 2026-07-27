window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.human = {
  methods: {
    switchTaskView(mode) {
      this.view = mode === 'graph' ? 'graph' : 'human';
      if (this.view === 'graph') this.$nextTick(() => { if (!this.cy) this.initGraph(); });
    },

    switchHumanTab(tab) {
      const allowed = ['workspace', 'todos', 'intents', 'facts', 'artifacts'];
      this.humanTab = allowed.includes(tab) ? tab : 'workspace';
    },

    humanTodoGroups() {
      const groups = {};
      for (const todo of (this.project?.todos || []).filter(item => !item.archived)) {
        const key = todo.category || todo.theme || '其他';
        if (!groups[key]) groups[key] = { name: key, total: 0, pending: 0, active: 0, completed: 0, items: [] };
        const group = groups[key];
        group.total++;
        group.items.push(todo);
        if (todo.status === 'completed') group.completed++;
        else if (todo.status === 'active') group.active++;
        else group.pending++;
      }
      return Object.values(groups).sort((a, b) => b.total - a.total || a.name.localeCompare(b.name));
    },

    humanTodoProgress() {
      const todos = (this.project?.todos || []).filter(item => !item.archived && !['rejected', 'obsolete'].includes(item.status));
      const completed = todos.filter(item => item.status === 'completed').length;
      return { total: todos.length, completed, percent: todos.length ? Math.round(completed / todos.length * 100) : 0 };
    },

    humanTodoLabel(status) {
      return ({ pending: '待处理', active: '进行中', completed: '已完成', rejected: '已拒绝', obsolete: '已失效' }[status] || status || '待处理');
    },

    humanTodoClass(status) {
      if (status === 'completed') return 'bg-emerald-50 text-emerald-700 border-emerald-100';
      if (status === 'active') return 'bg-amber-50 text-amber-700 border-amber-100';
      if (status === 'rejected' || status === 'obsolete') return 'bg-slate-100 text-slate-500 border-slate-200';
      return 'bg-blue-50 text-blue-700 border-blue-100';
    },

    humanBootstrapIntent() {
      return (this.project?.intents || []).find(intent => intent.creator === 'dispatcher.bootstrap' || String(intent.description || '').trim().toLowerCase() === 'bootstrap') || null;
    },

    humanBootstrapStatus() {
      const intent = this.humanBootstrapIntent();
      if (!intent) return { key: 'pending', label: '尚未开始' };
      const key = this.humanIntentStatusKey(intent);
      if (key === 'completed') return { key, label: '已完成' };
      if (key === 'working') return { key, label: '运行中' };
      if (key === 'failed') return { key, label: '执行失败' };
      return { key: 'queued', label: '等待执行' };
    },

    humanTimelineEvents() {
      const events = (this.project?.events?.length ? this.project.events : (this.seeEvents || [])).slice();
      return events.sort((a, b) => new Date(a.timestamp || a.created_at || 0) - new Date(b.timestamp || b.created_at || 0));
    },

    humanEventType(event) {
      return event?.event_type || event?.event || '';
    },

    humanCycleModel() {
      const allIntents = (this.project?.intents || []).filter(intent => !intent.archived);
      const bootstrap = this.humanBootstrapIntent();
      const exploreIntents = allIntents.filter(intent => !bootstrap || intent.id !== bootstrap.id);
      const intentMap = new Map(exploreIntents.map(intent => [intent.id, intent]));
      const used = new Set();
      const events = this.humanTimelineEvents();
      const commits = events.filter(event => this.humanEventType(event) === 'reason_workspace.committed');
      const cycles = commits.map((event, index) => {
        const ids = Array.isArray(event.payload?.created_intent_ids) ? event.payload.created_intent_ids : [];
        const intents = ids.map(id => intentMap.get(id)).filter(Boolean);
        ids.forEach(id => used.add(id));
        return {
          number: index + 1,
          workspaceId: event.workspace_id || event.payload?.workspace_id || '',
          reasonWorker: event.worker || '',
          createdAt: event.timestamp || event.created_at || '',
          todoIds: Array.isArray(event.payload?.created_todo_ids) ? event.payload.created_todo_ids : [],
          intents,
          status: this.humanCycleStatus(intents, ids.length === 0),
        };
      });

      const fallback = exploreIntents.filter(intent => !used.has(intent.id));
      if (fallback.length) {
        cycles.unshift({
          number: 1,
          workspaceId: '',
          reasonWorker: fallback[0]?.creator || '',
          createdAt: fallback[0]?.created_at || '',
          todoIds: fallback.map(intent => intent.todo_id).filter(Boolean),
          intents: fallback,
          status: this.humanCycleStatus(fallback, false),
          inferred: true,
        });
        cycles.forEach((cycle, index) => { cycle.number = index + 1; });
      }

      const latestTerminal = [...events].reverse().find(event => ['reason_workspace.committed', 'reason_workspace.abandoned'].includes(this.humanEventType(event)));
      const latestOpened = [...events].reverse().find(event => this.humanEventType(event) === 'reason_workspace.opened');
      const reasonRunning = this.project?.project?.phase === 'reason'
        && latestOpened
        && (!latestTerminal || new Date(latestOpened.timestamp || latestOpened.created_at || 0) > new Date(latestTerminal.timestamp || latestTerminal.created_at || 0));
      if (reasonRunning) {
        cycles.push({
          number: cycles.length + 1,
          workspaceId: latestOpened.workspace_id || '',
          reasonWorker: latestOpened.worker || this.project?.project?.reason?.worker || '',
          createdAt: latestOpened.timestamp || latestOpened.created_at || '',
          todoIds: [],
          intents: [],
          status: 'reason_running',
        });
      }

      if (!cycles.length && exploreIntents.length === 0 && this.project?.project?.phase === 'reason') {
        cycles.push({ number: 1, workspaceId: '', reasonWorker: this.project?.project?.reason?.worker || '', createdAt: '', todoIds: [], intents: [], status: 'reason_running' });
      }

      const current = cycles.length ? cycles[cycles.length - 1] : null;
      return { cycles, current, history: current ? cycles.slice(0, -1).reverse() : [] };
    },

    humanCycleStatus(intents, emptyCommit = false) {
      if (emptyCommit) return this.project?.project?.status === 'completed' ? 'goal_complete' : 'reason_complete';
      if (!intents.length) return 'reason_running';
      const keys = intents.map(intent => this.humanIntentStatusKey(intent));
      if (keys.some(key => key === 'working')) return 'exploring';
      if (keys.some(key => key === 'queued')) return 'queued';
      if (keys.some(key => ['failed', 'retry_exhausted'].includes(key))) return 'attention';
      if (keys.every(key => key === 'completed')) return 'completed';
      return 'mixed';
    },

    humanCycleStatusLabel(status) {
      return ({
        reason_running: 'Reason 处理中',
        reason_complete: 'Reason 已完成',
        exploring: '并发执行中',
        queued: '等待 Worker',
        attention: '需要重新规划',
        completed: '本轮已完成',
        goal_complete: 'Goal 已确认',
        mixed: '处理中',
      }[status] || status || '等待中');
    },

    humanCycleStatusClass(status) {
      if (status === 'goal_complete' || status === 'completed') return 'bg-emerald-50 text-emerald-700 border-emerald-100';
      if (status === 'exploring' || status === 'reason_running') return 'bg-blue-50 text-blue-700 border-blue-100';
      if (status === 'queued' || status === 'reason_complete') return 'bg-amber-50 text-amber-700 border-amber-100';
      if (status === 'attention') return 'bg-rose-50 text-rose-700 border-rose-100';
      return 'bg-slate-100 text-slate-600 border-slate-200';
    },

    humanIntentStatusKey(intent) {
      const state = this.intentLifecycleState(intent);
      if (state === 'completed') return 'completed';
      if (state === 'running') return 'working';
      if (state === 'cancelled') return 'cancelled';
      if (state === 'failed' && intent?.last_failure_kind === 'retry_exhausted') return 'retry_exhausted';
      if (state === 'failed') return 'failed';
      return 'queued';
    },

    humanIntentStatusLabel(intent) {
      const key = this.humanIntentStatusKey(intent);
      if (key === 'completed') return '已完成';
      if (key === 'working') return '执行中';
      if (key === 'cancelled') return '已取消';
      if (key === 'retry_exhausted') return '失败 · 待 Reason 重规划';
      if (key === 'failed') return '执行失败';
      if (intent?.next_retry_at || intent?.last_failure_kind || (intent?.retry_count || 0) > 0 || (intent?.infrastructure_retry_count || 0) > 0) {
        const execution = Number(intent?.retry_count || 0);
        const infrastructure = Number(intent?.infrastructure_retry_count || 0);
        const counts = [
          execution ? `执行 ${execution}` : '',
          infrastructure ? `基础设施 ${infrastructure}` : '',
        ].filter(Boolean).join(' · ');
        return counts ? `等待重试 · ${counts}` : '等待重试';
      }
      return intent?.worker ? '已领取' : '等待 Worker';
    },

    humanIntentStatusClass(intent) {
      const key = this.humanIntentStatusKey(intent);
      if (key === 'completed') return 'bg-emerald-50 text-emerald-700 border-emerald-100';
      if (key === 'working') return 'bg-blue-50 text-blue-700 border-blue-100';
      if (key === 'retry_exhausted' || key === 'failed') return 'bg-rose-50 text-rose-700 border-rose-100';
      if (key === 'cancelled') return 'bg-slate-100 text-slate-500 border-slate-200';
      return 'bg-amber-50 text-amber-700 border-amber-100';
    },

    humanCurrentIntents() {
      return (this.project?.intents || []).filter(intent => {
        const key = this.humanIntentStatusKey(intent);
        return key === 'working' || key === 'queued';
      }).filter(intent => intent !== this.humanBootstrapIntent()).slice(0, 12);
    },

    humanCycleDisplayIntents() {
      const active = this.humanCurrentIntents();
      if (active.length) return active;
      const current = this.humanCycleModel().current;
      if (current?.intents?.length) return current.intents.slice(0, 8);
      return (this.project?.intents || []).filter(intent => intent !== this.humanBootstrapIntent()).slice(-6).reverse();
    },

    humanNextActionLabel() {
      if (this.project?.project?.status === 'completed') return 'Goal 已达成，运行循环已经结束。';
      if (this.project?.project?.status === 'stopped') return '任务已暂停，恢复后将从当前状态继续。';
      if (this.project?.project?.status === 'failed') return '任务失败，需要人工检查失败原因。';
      if (this.project?.project?.phase === 'reason') return 'Reason 正在汇总 Facts、更新 Todo 并决定下一批 Intent。';
      const active = this.humanCurrentIntents();
      if (active.length) return `等待 ${active.length} 个 Intent 返回结果，之后进入下一轮 Reason。`;
      return '等待调度器进入下一轮 Reason。';
    },

    humanGoalEvent() {
      return [...this.humanTimelineEvents()].reverse().find(event => this.humanEventType(event) === 'task.completed') || null;
    },

    humanGoalStatusLabel() {
      const status = this.project?.project?.status;
      if (status === 'completed') return 'Goal 已达成';
      if (status === 'failed') return '任务失败';
      if (status === 'stopped') return '任务已暂停';
      return 'Goal 验证中';
    },

    humanGoalStatusClass() {
      const status = this.project?.project?.status;
      if (status === 'completed') return 'bg-emerald-50 text-emerald-700 border-emerald-100';
      if (status === 'failed') return 'bg-rose-50 text-rose-700 border-rose-100';
      if (status === 'stopped') return 'bg-amber-50 text-amber-700 border-amber-100';
      return 'bg-blue-50 text-blue-700 border-blue-100';
    },

    humanGoalExplanation() {
      const event = this.humanGoalEvent();
      if (event?.payload?.description) return event.payload.description;
      return this.humanNextActionLabel();
    },

    humanSeverityCounts() {
      const counts = { critical: 0, high: 0, medium: 0, low: 0 };
      for (const vuln of (this.project?.vulns || [])) {
        const raw = String(vuln.severity || '').toLowerCase();
        const key = ({ '严重': 'critical', '高危': 'high', '中危': 'medium', '低危': 'low' }[vuln.severity] || raw);
        if (counts[key] !== undefined) counts[key]++;
      }
      return counts;
    },

    openTaskVulnerabilities() {
      const taskId = this.selectedProjectId || this.project?.project?.id || '';
      this.vulnFilter = { ...(this.vulnFilter || {}), projectId: taskId, keyword: '' };
      this.selectedVulnerabilityKey = '';
      this.navigateTo('vulnerabilities');
    },

    humanRecentFacts(limit = 5) {
      return (this.project?.facts || []).filter(fact => !fact.archived && !['goal', 'origin'].includes(fact.id)).slice(-limit).reverse();
    },

    humanLeadArtifacts(limit = 5) {
      return (this.project?.artifacts || []).filter(item => String(item.artifact_type || '').toLowerCase().includes('lead')).slice(-limit).reverse();
    },

    humanArtifactLabel(item) {
      return String(item?.path || '').split('/').pop() || item?.id || '产物';
    },

    humanArtifactPhase(item) {
      const path = String(item?.path || '').toLowerCase();
      if (path.includes('/bootstrap-')) return 'Bootstrap';
      if (path.includes('/explore-')) return 'Explore';
      if (path.includes('/reason-')) return 'Reason';
      return item?.intent_id ? 'Worker' : '任务';
    },

    humanArtifactTypeLabel(item) {
      const type = String(item?.artifact_type || '').toLowerCase();
      if (type.includes('lead')) return 'Leads';
      if (type.includes('finding')) return 'Findings';
      if (type.includes('asset')) return '攻击面';
      return item?.artifact_type || 'Artifact';
    },
  },
};
