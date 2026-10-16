window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.human = {
  methods: {
    async switchTaskView(mode) {
      const taskId = this.selectedProjectId;
      const generation = this._projectOpenGeneration;
      const previousHydration = this._taskDetailHydrationPromise;
      this.view = ['graph', 'archive'].includes(mode) ? mode : 'human';
      if (taskId) {
        const targetHash = `#/tasks/${encodeURIComponent(taskId)}${this.view === 'graph' ? '?view=graph' : (this.view === 'archive' ? '?view=archive' : '')}`;
        if (typeof location !== 'undefined' && location.hash !== targetHash) {
          const historyApi = typeof history !== 'undefined' ? history : null;
          const replaceState = historyApi && (historyApi.replaceState || historyApi.pushState);
          if (typeof replaceState === 'function') replaceState.call(historyApi, null, '', targetHash);
        }
      }
      this.showPageSkeleton();
      await this.mountCurrentPage();
      if (previousHydration) await previousHydration;
      if (!taskId || taskId !== this.selectedProjectId || generation !== this._projectOpenGeneration) return;
      if (this.view !== 'archive' && this.project && !this.project.archived_locked) {
        const graphReady = this.taskDetailSectionReady?.('graph');
        if (this.view === 'graph' && graphReady && !this.cy) this.initGraph?.();
        const incompleteSections = Object.entries(this.taskDetailLoad?.sections || {})
          .filter(([, status]) => status !== 'ready')
          .map(([name]) => name);
        if (!this.taskDetailLoad?.hydrating && incompleteSections.length) {
          void this.hydrateTaskDetails?.(taskId, {
            generation,
            signal: this._pageAbortController?.signal,
            sections: incompleteSections,
          });
        }
      }
    },

    switchHumanTab(tab) {
      const allowed = ['workspace', 'todos'];
      this.humanTab = allowed.includes(tab) ? tab : 'workspace';
    },

    humanTodoGroups() {
      const groups = {};
      for (const todo of (this.project?.todos || []).filter(item => !item.archived)) {
        const key = todo.category || todo.theme || '其他';
        if (!groups[key]) {
          groups[key] = {
            name: key,
            total: 0,
            open: 0,
            partial: 0,
            blocked: 0,
            completed: 0,
            items: [],
          };
        }
        const group = groups[key];
        group.total++;
        group.items.push(todo);
        if (todo.status === 'completed') group.completed++;
        else if (todo.status === 'partial') group.partial++;
        else if (todo.status === 'blocked') group.blocked++;
        else group.open++;
      }
      return Object.values(groups).sort((a, b) => b.total - a.total || a.name.localeCompare(b.name));
    },

    // ── 时间轴模式：i/f 按创建时间交错排序，intent 汇聚产物数与产出 fact ──
    toggleHumanNavMode() {
      this.humanNavMode = this.humanNavMode === 'tree' ? 'timeline' : 'tree';
    },
    humanIntentArtifactCount(intentId) {
      return (this.humanAllKnownArtifacts() || []).filter(item => !item.archived && item.intent_id === intentId).length;
    },
    humanIntentFirstFact(intentId) {
      return this.intentProducedFacts({ id: intentId })[0] || null;
    },
    humanTimelineIntents() {
      // 按上下文版本缓存,避免每颗胶囊都重算 O(artifacts) 的产物计数
      const rev = this.project?._timelineRev || 0;
      if (this._tlCacheRev === rev && this._tlCache) return this._tlCache;
      const intents = (this.project?.intents || []).filter(item => !item.archived);
      const facts = (this.project?.facts || []).filter(f => !f.archived);
      const producedByIntent = {};
      for (const fact of facts) {
        const producer = this.graphEntityIndex().intentsByProduced.get(fact.id);
        if (producer?.id) (producedByIntent[producer.id] = producedByIntent[producer.id] || []).push(fact);
      }
      const artifactCount = {};
      for (const a of (this.project?.artifacts || [])) {
        if (a.archived || !a.intent_id) continue;
        artifactCount[a.intent_id] = (artifactCount[a.intent_id] || 0) + 1;
      }
      const num = (id) => parseInt(String(id).replace(/\D/g, ''), 10) || 0;
      const entries = intents.map(intent => ({
        intent,
        fact: (producedByIntent[intent.id] || [])[0] || null,
        artCount: artifactCount[intent.id] || 0,
        n: num(intent.id),
      }));
      entries.sort((a, b) => ((b.fact ? 1 : 0) - (a.fact ? 1 : 0)) || (a.n - b.n));
      this._tlCacheRev = rev;
      this._tlCache = entries;
      return entries;
    },
    humanTimelinePageSize() { return 50; },
    humanTimelinePageCount() {
      return Math.max(1, Math.ceil(this.humanTimelineIntents().length / this.humanTimelinePageSize()));
    },
    async setHumanTimelinePage(page) {
      const next = Math.max(0, Math.min(this.humanTimelinePageCount() - 1, Number(page) || 0));
      if (next === this.humanTimelinePage) return;
      this.humanTimelinePage = next;
    },
    humanTimelinePageEntries() {
      const all = this.humanTimelineIntents();
      const start = this.humanTimelinePage * this.humanTimelinePageSize();
      return all.slice(start, start + this.humanTimelinePageSize());
    },

    // Todo 页左侧总览排序：按子 Intent 数量从大到小
    isRedTeamBoard() {
      return this.project?.project?.engagement_mode === 'red_team';
    },

    humanBoardNoun() {
      return this.isRedTeamBoard() ? '攻击链' : 'Todo';
    },

    humanRedTeamPoints() {
      const awarded = (this.project?.red_team_results || [])
        .filter(item => item.status === 'verified')
        .reduce((sum, item) => sum + Number(item.points_awarded || 0), 0);
      const target = Number(this.project?.project?.red_team_objective?.target_points || 100);
      const percent = target > 0 ? Math.min(100, Math.round(awarded / target * 100)) : 0;
      return { awarded, target, percent };
    },

    humanSortedTodos() {
      const items = this.isRedTeamBoard()
        ? (this.project?.chains || [])
        : (this.project?.todos || []).filter(item => !item.archived);
      return [...items].sort((a, b) => this.getIntentsForTodo(b.id).length - this.getIntentsForTodo(a.id).length
        || String(a.id).localeCompare(String(b.id)));
    },

    // 漏洞模式按创建时间新到旧。红队链按创建顺序，第一步在上。
    navIntentsForTodo(todoId) {
      const items = this.getIntentsForTodo(todoId);
      if (this.isRedTeamBoard()) return items;
      return [...items].sort((a, b) => (
        String(b.created_at || '').localeCompare(String(a.created_at || ''))
      ));
    },

    intentParentNoun() {
      return this.isRedTeamBoard() ? '所属攻击链' : '所属 Todo';
    },

    intentParentLabel(intent) {
      if (!intent) return '未关联';
      if (this.isRedTeamBoard()) {
        const chain = (this.project?.chains || []).find(item => item.id === intent.chain_id);
        if (!chain) return intent.chain_id || '未关联';
        return `${chain.id} ${chain.title || ''}`.trim();
      }
      return intent.todo_id || '未关联';
    },

    intentFeedParent(intent) {
      if (this.isRedTeamBoard()) {
        return intent?.chain_id ? `链：${intent.chain_id}` : '未关联攻击链';
      }
      return intent?.todo_id ? `Todo：${intent.todo_id}` : '未关联 Todo';
    },

    // ── Todo 页主从布局：左侧总览树 + 右侧 Intent 详情 ──
    // Todo 行悬停浮窗：展示标题、子 Intent 进度与评估说明（status_reason）
    markTodoHover(event, todo) {
      this.todoHover = todo ? { d: todo, x: event.clientX, y: event.clientY } : null;
    },

    todoHoverStyle(el) {
      const h = this.todoHover;
      if (!h) return 'display:none';
      const W = 340;
      let left = h.x + 14;
      if (left + W > window.innerWidth - 8) left = Math.max(8, h.x - W - 14);
      let top = h.y + 14;
      const eh = (el && el.offsetHeight) || 200;
      if (eh >= window.innerHeight - 16) return `left:${left}px;top:8px;width:${W}px;`;
      if (top + eh > window.innerHeight - 8) top = Math.max(8, window.innerHeight - eh - 8);
      return `left:${left}px;top:${top}px;width:${W}px;`;
    },

    toggleTodoNav(todoId) {
      this.todoNavExpandedId = this.todoNavExpandedId === todoId ? '' : todoId;
    },

    selectTodoNavIntent(intentId) {
      this.todoNavIntentId = intentId;
    },

    todoNavIntentRecord() {
      return this.todoNavIntentId ? this.humanIntentRecord(this.todoNavIntentId) : null;
    },

    // Intent 的输入来源：from 指向的 Facts
    intentSourceFacts(intent) {
      const from = intent?.from || [];
      const factsById = this.graphEntityIndex().factsById;
      return from.map(id => factsById.get(id)).filter(Boolean);
    },

    // Intent 的产出物：由它生产出的 Facts（点击可跳转对应产物文件）
    intentProducedFacts(intent) {
      if (!intent?.id) return [];
      const { intentsByProduced } = this.graphEntityIndex();
      return (this.project?.facts || []).filter(fact => !fact.archived && intentsByProduced.get(fact.id)?.id === intent.id);
    },

    // Fact 引用的产物链接列表：引用了几个就给几个，各自独立跳转
    factArtifactLinks(fact) {
      if (!fact?.id) return [];
      const ids = this.humanFactReferenceIds(fact);
      if (ids.length) return ids.map(id => ({ id, label: id }));
      const artifact = this.humanFactArtifact(fact);
      return artifact?.id ? [{ id: artifact.id, label: artifact.id }] : [];
    },

    // 从 Todo 详情跳转到指定产物，记录返回页签
    async openFactArtifactLink(taskId, artifactId) {
      if (!taskId || !artifactId) return;
      this.markHumanTabReturn(taskId);
      await this.openArtifactLocation(taskId, artifactId);
    },

    // Todo 页顶部概览统计：按状态计数，填满页头右侧空间
    humanTodoStats() {
      const todos = (this.project?.todos || []).filter(item => !item.archived && !['rejected', 'obsolete'].includes(item.status));
      const count = (status) => todos.filter(item => item.status === status).length;
      const open = todos.filter(item => !['completed', 'partial', 'blocked'].includes(item.status)).length;
      return [
        { label: '全部 Todo', value: todos.length, dot: 'bg-slate-300', text: 'text-slate-800' },
        { label: '已完成', value: count('completed'), dot: 'bg-emerald-500', text: 'text-emerald-600' },
        { label: '部分完成', value: count('partial'), dot: 'bg-brand-500', text: 'text-brand-600' },
        { label: '阻塞', value: count('blocked'), dot: 'bg-amber-500', text: 'text-amber-600' },
        { label: '待执行', value: open, dot: 'bg-slate-400', text: 'text-slate-600' },
      ];
    },

    humanTodoProgress() {
      const items = this.isRedTeamBoard()
        ? (this.project?.chains || [])
        : (this.project?.todos || []).filter(item => !item.archived && !['rejected', 'obsolete'].includes(item.status));
      const completed = items.filter(item => item.status === 'completed' || item.status === 'abandoned').length;
      return { total: items.length, completed, percent: items.length ? Math.round(completed / items.length * 100) : 0 };
    },

    // 任务目标覆盖率：已完成 Intent / 全部 Intent（替代原先按 Todo 完成度计算）
    humanIntentCoverage() {
      const intents = (this.project?.intents || []).filter(item => !item.archived);
      const completed = intents.filter(item => item.status === 'completed').length;
      return { total: intents.length, completed, percent: intents.length ? Math.round(completed / intents.length * 100) : 0 };
    },

    humanTodoLabel(status) {
      return ({ open: '开放', partial: '部分', blocked: '阻塞', completed: '已完成' }[status] || status || '开放');
    },

    humanTodoClass(status) {
      if (status === 'completed') return 'bg-emerald-50 text-emerald-700 border-emerald-100';
      if (status === 'partial') return 'bg-amber-50 text-amber-700 border-amber-100';
      if (status === 'blocked') return 'bg-rose-50 text-rose-700 border-rose-100';
      return 'bg-blue-50 text-blue-700 border-blue-100';
    },

    humanBootstrapIntent() {
      return (this.project?.intents || []).find(intent => intent.creator === 'dispatcher.bootstrap' || String(intent.description || '').trim().toLowerCase() === 'bootstrap') || null;
    },

    humanBootstrapStatus() {
      const intent = this.humanBootstrapIntent();
      if (!intent) {
        const task = this.project?.project || {};
        if (task.phase === 'bootstrap' || task.bootstrap_status === 'running') {
          return { key: 'running', label: '运行中', icon: '↻' };
        }
        return { key: 'waiting', label: '尚未开始', icon: '•' };
      }
      const key = this.humanIntentStatusKey(intent);
      if (key === 'completed') return { key: 'completed', label: '已完成', icon: '✓' };
      if (key === 'working') return { key: 'running', label: '运行中', icon: '↻' };
      if (key === 'failed') return { key: 'failed', label: '执行失败', icon: '!' };
      return { key: 'waiting', label: '等待执行', icon: '•' };
    },

    humanTimelineEvents() {
      const source = this.project?.events?.length ? this.project.events : (this.seeEvents || []);
      const seen = new Set();
      return (Array.isArray(source) ? source : [])
        .filter(event => {
          const key = event?.id || `${event?.event_type || event?.event || ''}:${event?.created_at || event?.timestamp || ''}:${event?.workspace_id || ''}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .sort((a, b) => {
          const time = this.humanEventTime(a) - this.humanEventTime(b);
          return time || String(a?.id || '').localeCompare(String(b?.id || ''));
        });
    },

    humanEventType(event) {
      return event?.event_type || event?.event || '';
    },

    humanEventTime(event) {
      const parsed = Date.parse(event?.timestamp || event?.created_at || '');
      return Number.isFinite(parsed) ? parsed : 0;
    },

    humanEventPayload(event) {
      if (event?.payload && typeof event.payload === 'object') return event.payload;
      try { return JSON.parse(event?.payload || '{}'); } catch (_) { return {}; }
    },

    humanReasonEventType(event) {
      const type = String(this.humanEventType(event) || '');
      return ({
        'reason_workspace.opened': 'reason.workspace_opened',
        'reason_workspace.abandoned': 'reason.workspace_abandoned',
        'reason_workspace.committed': 'reason.committed',
      }[type] || type);
    },

    humanReasonRunId(event) {
      const payload = this.humanEventPayload(event);
      return String(event?.reason_run_id || payload.reason_run_id || payload.run_id || (event?.workspace_id ? `workspace:${event.workspace_id}` : '') || '');
    },

    humanCycleModel() {
      const allIntents = (this.project?.intents || []).filter(intent => !intent.archived);
      const bootstrap = this.humanBootstrapIntent();
      const exploreIntents = allIntents.filter(intent => !bootstrap || intent.id !== bootstrap.id);
      const intentMap = new Map(exploreIntents.map(intent => [intent.id, intent]));
      const events = this.humanTimelineEvents();
      const reasonEvents = events.filter(event => {
        const type = this.humanReasonEventType(event);
        return type.startsWith('reason_run.') || type.startsWith('reason.workspace_') || type === 'reason.committed';
      });
      const byId = new Map();
      const ensure = (reasonRunId, event) => {
        const id = reasonRunId || `event:${event?.id || this.humanEventTime(event)}`;
        if (!byId.has(id)) {
          byId.set(id, {
            reasonRunId: id,
            key: `reason-cycle-${id}`,
            createdAt: '',
            finishedAt: '',
            reasonWorker: '',
            trigger: 'scheduler',
            state: 'pending',
            failureReason: '',
            eventIds: new Set(),
            workspaceIds: new Set(),
            intentIds: new Set(),
            todoIds: new Set(),
            goalAchieved: false,
            committed: false,
            associationKnown: false,
          });
        }
        return byId.get(id);
      };
      for (const event of reasonEvents) {
        const cycle = ensure(this.humanReasonRunId(event), event);
        const type = this.humanReasonEventType(event);
        const payload = this.humanEventPayload(event);
        const timestamp = event?.timestamp || event?.created_at || '';
        cycle.eventIds.add(event?.id || `${type}:${timestamp}`);
        if (!cycle.createdAt || this.humanEventTime(event) < Date.parse(cycle.createdAt)) cycle.createdAt = timestamp;
        if (event?.worker || payload.worker) cycle.reasonWorker = event.worker || payload.worker;
        if (payload.trigger) cycle.trigger = payload.trigger;
        if (event?.workspace_id || payload.workspace_id) cycle.workspaceIds.add(event.workspace_id || payload.workspace_id);
        else if (type === 'reason.workspace_opened') cycle.workspaceIds.add(`event:${event?.id || timestamp}`);
        if (type === 'reason_run.created') {
          cycle.state = 'pending';
          cycle.finishedAt = '';
        } else if (type === 'reason_run.claimed') {
          cycle.state = 'claimed';
          cycle.finishedAt = '';
        } else if (type === 'reason_run.started' || type === 'reason.workspace_opened') {
          cycle.state = 'running';
          cycle.finishedAt = '';
        }
        else if (type === 'reason.workspace_abandoned') {
          cycle.state = 'abandoned';
          cycle.failureReason = payload.failure_reason || event.message || cycle.failureReason;
          cycle.finishedAt = timestamp;
        } else if (type === 'reason_run.released') {
          const terminal = payload.terminal === true || payload.terminal === 'true';
          cycle.state = terminal ? 'failed' : 'retrying';
          cycle.failureReason = payload.failure_kind || event.message || cycle.failureReason;
          cycle.finishedAt = terminal ? timestamp : '';
        } else if (type === 'reason_run.lease_expired') {
          const terminal = payload.terminal === true || payload.terminal === 'true';
          cycle.state = terminal ? 'failed' : 'retrying';
          cycle.failureReason = payload.failure_kind || 'lease_expired';
          cycle.finishedAt = terminal ? timestamp : '';
        } else if (type === 'reason.committed') {
          cycle.state = 'committed';
          cycle.committed = true;
          cycle.finishedAt = timestamp;
          cycle.goalAchieved = payload.goal_achieved === true;
          const intentIds = Array.isArray(payload.created_intent_ids) ? payload.created_intent_ids : [];
          const todoIds = Array.isArray(payload.created_todo_ids) ? payload.created_todo_ids : [];
          intentIds.forEach(id => cycle.intentIds.add(id));
          todoIds.forEach(id => cycle.todoIds.add(id));
          if (intentIds.length) cycle.associationKnown = true;
        }
      }

      // A task payload can expose an active ReasonRun before its first audit event
      // arrives. Keep a synthetic row so the UI does not jump back to old work.
      const activeReason = this.project?.project?.reason;
      if (activeReason?.id && !byId.has(activeReason.id)) {
        const cycle = ensure(activeReason.id, {});
        cycle.createdAt = activeReason.created_at || activeReason.activity_started_at || '';
        cycle.reasonWorker = activeReason.worker || '';
        cycle.trigger = activeReason.trigger || 'scheduler';
        cycle.state = activeReason.status || 'running';
      }

      const cycles = [...byId.values()].map(cycle => {
        cycle.intents = [...cycle.intentIds].map(id => intentMap.get(id)).filter(Boolean);
        cycle.todoIds = [...cycle.todoIds];
        cycle.workspaceIds = [...cycle.workspaceIds];
        cycle.eventIds = [...cycle.eventIds];
        cycle.workspaceAttempts = cycle.workspaceIds.length;
        cycle.status = this.humanCycleStatus(cycle);
        cycle.endAt = cycle.finishedAt || (cycle.state === 'committed' ? cycle.createdAt : '');
        return cycle;
      }).sort((a, b) => {
        const time = this.humanEventTime({ timestamp: a.createdAt }) - this.humanEventTime({ timestamp: b.createdAt });
        return time || String(a.reasonRunId).localeCompare(String(b.reasonRunId));
      });
      cycles.forEach((cycle, index) => { cycle.number = index + 1; });

      // If audit history predates ReasonRun IDs, only use the creation-time
      // fallback when no committed batch can be associated at all.
      const associationAvailable = cycles.some(cycle => cycle.associationKnown);
      if (!cycles.length && exploreIntents.length) {
        cycles.push({
          number: 1,
          reasonRunId: '',
          key: 'reason-cycle-inferred',
          workspaceId: '',
          workspaceAttempts: 0,
          reasonWorker: exploreIntents[0]?.creator || '',
          trigger: 'legacy',
          createdAt: exploreIntents[0]?.created_at || '',
          finishedAt: '',
          todoIds: [...new Set(exploreIntents.map(intent => intent.todo_id).filter(Boolean))],
          intents: this.humanSortedIntents(exploreIntents).slice(0, 8),
          eventIds: [],
          state: 'committed',
          status: this.humanCycleStatus({ state: 'committed', intents: exploreIntents, goalAchieved: false }),
          associationKnown: false,
          inferred: true,
          committed: true,
        });
      } else if (!associationAvailable && exploreIntents.length) {
        // Keep the real ReasonRun rows intact; the caller may use this only as
        // a last-resort display batch, never as a new cycle.
        cycles.creationFallbackIntents = this.humanSortedIntents(exploreIntents).slice(0, 8);
      }

      const current = cycles.length ? cycles[cycles.length - 1] : null;
      const history = current ? cycles.slice(0, -1).reverse() : [];
      return {
        cycles,
        current,
        history,
        reasonCount: cycles.length,
        exploreCount: exploreIntents.length,
        associationAvailable,
        fallbackIntents: cycles.creationFallbackIntents || [],
      };
    },

    humanCycleStatus(cycleOrIntents, emptyCommit = false) {
      // Keep the old call shape for other templates/tests while making the
      // ReasonRun state authoritative for the new cycle model.
      if (Array.isArray(cycleOrIntents)) {
        if (emptyCommit) return this.project?.project?.status === 'completed' ? 'goal_complete' : 'reason_complete';
        const keys = cycleOrIntents.map(intent => this.humanIntentStatusKey(intent));
        if (keys.some(key => key === 'working')) return 'exploring';
        if (keys.some(key => key === 'queued')) return 'queued';
        if (keys.some(key => ['failed', 'retry_exhausted'].includes(key))) return 'attention';
        if (keys.length && keys.every(key => key === 'completed')) return 'completed';
        return 'mixed';
      }
      const cycle = cycleOrIntents || {};
      if (cycle.state === 'failed' || cycle.state === 'abandoned') return 'attention';
      if (cycle.state === 'retrying') return 'retrying';
      if (cycle.state === 'pending' || cycle.state === 'claimed' || cycle.state === 'running') return 'reason_running';
      if (cycle.state === 'committed') {
        const intents = cycle.intents || [];
        if (cycle.goalAchieved) return 'goal_complete';
        if (intents.some(intent => ['running', 'concluding', 'claimed'].includes(String(intent.status)))) return 'exploring';
        if (intents.some(intent => intent.status === 'pending')) return 'queued';
        if (intents.some(intent => ['failed', 'cancelled'].includes(String(intent.status)))) return 'attention';
        if (intents.length && intents.every(intent => intent.status === 'completed')) return 'completed';
        return 'reason_complete';
      }
      return 'mixed';
    },

    humanCycleStatusLabel(status) {
      return ({
        reason_running: 'Reason 处理中',
        reason_complete: 'Reason 已完成',
        exploring: '并发执行中',
        queued: '等待 Worker',
        attention: '需要重新规划',
        retrying: '等待 Reason 重试',
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
      if (status === 'retrying') return 'bg-amber-50 text-amber-700 border-amber-100';
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
      if (intent?.activity_kind === 'retry_blocked') return '重试已暂停';
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
      if (intent?.activity_kind === 'retry_blocked') return 'bg-rose-50 text-rose-700 border-rose-100';
      if (key === 'cancelled') return 'bg-slate-100 text-slate-500 border-slate-200';
      return 'bg-amber-50 text-amber-700 border-amber-100';
    },

    humanIntentIconClass(intent) {
      const status = String(intent?.status || '').toLowerCase();
      if (['running', 'concluding', 'claimed'].includes(status)) return 'human-stage-icon human-stage-running';
      if (status === 'pending') return 'human-stage-icon human-stage-waiting';
      if (['failed', 'cancelled', 'canceled'].includes(status)) return 'human-stage-icon human-stage-failed';
      return 'human-stage-icon human-stage-completed';
    },

    humanIntentIcon(intent) {
      const status = String(intent?.status || '').toLowerCase();
      if (['running', 'concluding', 'claimed'].includes(status)) return '↻';
      if (status === 'pending') return '•';
      if (['failed', 'cancelled', 'canceled'].includes(status)) return '!';
      return '✓';
    },

    humanIntentSortPriority(intent) {
      const status = String(intent?.status || '').toLowerCase();
      return ({ running: 5, concluding: 5, claimed: 4, pending: 3, completed: 2, failed: 1, cancelled: 1, canceled: 1 }[status] || 0);
    },

    humanSortedIntents(intents) {
      return [...(intents || [])].sort((a, b) => {
        const priority = this.humanIntentSortPriority(b) - this.humanIntentSortPriority(a);
        if (priority) return priority;
        const created = (Date.parse(b?.created_at || '') || 0) - (Date.parse(a?.created_at || '') || 0);
        return created || String(b?.id || '').localeCompare(String(a?.id || ''));
      });
    },

    humanCurrentIntents() {
      const bootstrapId = this.humanBootstrapIntent()?.id;
      return this.humanSortedIntents((this.project?.intents || []).filter(intent => (
        !intent.archived
        && intent.id !== bootstrapId
        && ['pending', 'claimed', 'running', 'concluding'].includes(String(intent.status || '').toLowerCase())
      ))).slice(0, 12);
    },

    humanCycleDisplayIntents() {
      const active = this.humanCurrentIntents();
      if (active.length) return active;
      const model = this.humanCycleModel();
      const latestNonEmpty = [...model.cycles].reverse().find(cycle => cycle.intents?.length);
      if (latestNonEmpty) return this.humanSortedIntents(latestNonEmpty.intents).slice(0, 8);
      return model.fallbackIntents || [];
    },

    humanCycleDisplaySourceLabel() {
      if (this.humanCurrentIntents().length) return '当前开放 Intent';
      const latestNonEmpty = [...this.humanCycleModel().cycles].reverse().find(cycle => cycle.intents?.length);
      return latestNonEmpty ? `第 ${latestNonEmpty.number} 轮 · ${latestNonEmpty.reasonRunId}` : '暂无可关联批次';
    },

    humanStageState(stage) {
      const task = this.project?.project || {};
      if (stage === 'bootstrap') {
        return this.humanBootstrapStatus();
      }
      if (stage === 'reason') {
        const model = this.humanCycleModel();
        const current = model.current;
        if (['pending', 'claimed', 'running', 'concluding'].includes(current?.state)) {
          const state = current?.state;
          if (state === 'pending') return { state: 'waiting', label: '等待领取', icon: '•' };
          if (state === 'claimed' || state === 'running') return { state: 'running', label: '运行中', icon: '↻' };
          if (state === 'concluding') return { state: 'running', label: '收口中', icon: '↻' };
          return { state: 'running', label: '运行中', icon: '↻' };
        }
        if (task.phase === 'reason' && !current) return { state: 'running', label: '运行中', icon: '↻' };
        if (current?.state === 'failed' || current?.state === 'abandoned') return { state: 'failed', label: '执行失败', icon: '!' };
        if (current?.state === 'retrying') return { state: 'waiting', label: '等待重试', icon: '•' };
        if (current?.committed || ['waiting', 'completed'].includes(task.phase) || task.status === 'completed') return { state: 'completed', label: '已完成', icon: '✓' };
        return { state: 'waiting', label: '等待启动', icon: '•' };
      }
      if (stage === 'plan') {
        const current = task.active_plan_run;
        const state = String(current?.status || '').toLowerCase();
        if (['claimed', 'running', 'concluding'].includes(state)) {
          return { state: 'running', label: state === 'concluding' ? '收口中' : '全景复盘', icon: '✦' };
        }
        if (state === 'pending' || task.phase === 'plan') {
          return { state: 'waiting', label: '等待 Plan Worker', icon: '✦' };
        }
        if (Number(task.plan_run_count || 0) > 0) {
          return { state: 'completed', label: `已复盘 ${task.plan_run_count} 次`, icon: '✓' };
        }
        return { state: 'waiting', label: '待触发', icon: '✦' };
      }
      const currentIntents = this.humanCurrentIntents();
      if (currentIntents.some(intent => ['running', 'concluding', 'claimed'].includes(String(intent.status || '').toLowerCase()))) {
        return { state: 'running', label: '执行中', icon: '↻' };
      }
      if (currentIntents.some(intent => String(intent.status || '').toLowerCase() === 'pending')) {
        return { state: 'waiting', label: '等待 Worker', icon: '•' };
      }
      const model = this.humanCycleModel();
      if (model.current?.intents?.length) {
        const failed = model.current.intents.some(intent => ['failed', 'cancelled'].includes(String(intent.status || '').toLowerCase()));
        return failed ? { state: 'failed', label: '有失败 Intent', icon: '!' } : { state: 'completed', label: '本轮完成', icon: '✓' };
      }
      if (task.phase === 'explore') return { state: 'waiting', label: '等待 Intent', icon: '•' };
      return { state: task.status === 'completed' ? 'completed' : 'waiting', label: task.status === 'completed' ? '已结束' : '等待执行', icon: task.status === 'completed' ? '✓' : '•' };
    },

    humanStageStateClass(stage) {
      const state = this.humanStageState(stage)?.state || 'waiting';
      // Keep the phase identity on the status icon as well as on the label.
      // Plan uses an indigo palette even when it is waiting for a dedicated
      // backend, instead of inheriting Reason's generic blue/amber state
      // colors.
      return `human-stage-icon human-stage-${state} human-stage-${stage}`;
    },

    humanStageLabel(stage) {
      return this.humanStageState(stage)?.label || '等待中';
    },

    planDecisionLabel(decision) {
      const value = typeof decision === 'string' ? decision : decision?.decision;
      return ({
        dispatch_explore: '派发 Explore',
        explore: '回到 Explore',
        continue: '继续运行',
        needs_reason: '回到 Reason',
        reason: '回到 Reason',
        exhausted: '确认穷尽',
        waiting: '进入等待',
      }[value] || value || '尚未收口');
    },

    humanCycleCounts() {
      const model = this.humanCycleModel();
      return {
        reason: model.reasonCount || 0,
        explore: model.exploreCount || 0,
        active: this.humanCurrentIntents().length,
      };
    },

    humanNextActionLabel() {
      if (this.project?.project?.status === 'completed') return 'Goal 已达成，运行循环已经结束。';
      if (this.project?.project?.status === 'stopped') return '任务已暂停，恢复后将从当前状态继续。';
      if (this.project?.project?.status === 'failed') return '任务失败，需要人工检查失败原因。';
      if (this.humanStageState('plan').state === 'running') return 'Plan 正在复核全量历史图谱、覆盖缺口与跨模块攻击链。';
      if (this.humanStageState('reason').state === 'running') return 'Reason 正在汇总 Facts、更新 Todo 并决定下一批 Intent。';
      const active = this.humanCurrentIntents();
      if (active.length) return `等待 ${active.length} 个 Intent 返回结果，之后进入下一轮 Reason。`;
      const planDecision = this.project?.project?.last_plan_decision;
      if (['dispatch_explore', 'explore', 'continue'].includes(planDecision?.decision)) {
        return 'Plan 已完成全景复盘并派发新的 Explore，等待探测结果。';
      }
      if (['needs_reason', 'reason'].includes(planDecision?.decision)) {
        return 'Plan 已发现新的待评估输入，等待 Reason 增量结算。';
      }
      if (['waiting', 'exhausted'].includes(planDecision?.decision)) {
        return planDecision.exhaustion_reason || 'Plan 已完成全景复盘，当前没有材料性的新方向。';
      }
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

    humanAttentionItems() {
      const items = [];
      const p = this.project?.project;
      if (!p) return items;
      const status = String(p.status || '').toLowerCase();
      if (status === 'stopped') items.push({ level: 'info', label: '任务已暂停，恢复后将从当前状态继续' });
      for (const intent of (this.humanCycleDisplayIntents() || [])) {
        const st = String(intent.status || '').toLowerCase();
        const retries = (intent.retry_count || 0) + (intent.infrastructure_retry_count || 0);
        if (['failed', 'error'].includes(st)) {
          items.push({ level: 'danger', label: `${intent.id} 执行失败：${(intent.description || '未命名 Intent').slice(0, 48)}` });
        } else if (st === 'blocked') {
          items.push({ level: 'warn', label: `${intent.id} 被阻塞：${(intent.description || '').slice(0, 48)}` });
        } else if (retries > 0) {
          items.push({ level: 'warn', label: `${intent.id} 重试 ${intent.retry_count || 0} 次 · 基础设施重试 ${intent.infrastructure_retry_count || 0}` });
        }
      }
      const ts = this.taskTokenSummary ? this.taskTokenSummary() : null;
      if (ts && (ts.complete === false || ts.incomplete_sources > 0)) {
        items.push({ level: 'warn', label: `${ts.incomplete_sources || 0} 个 Session 来源不完整，Token 统计偏低` });
      }
      // 基础设施重试耗尽的条目会一直停在 pending 且不再被派发；不显式提示的话，
      // 界面上只表现为"再也没有进展"。
      if ((p.blocked_work_count || 0) > 0) {
        items.unshift({
          level: 'danger',
          label: `${p.blocked_work_count} 项工作因基础设施重试耗尽已暂停自动重试，需人工恢复`,
        });
      }
      return items.slice(0, 5);
    },

    taskTokenBreakdown() {
      const ts = this.taskTokenSummary ? this.taskTokenSummary() : {};
      const total = Number(ts.total_tokens ?? ts.total ?? 0) || 0;
      const read = Number(ts.cache_read_tokens ?? ts.cache_read ?? 0) || 0;
      const input = Number(ts.input_tokens ?? ts.input_uncached ?? 0) || 0;
      const write = Number(ts.cache_write_tokens ?? ts.cache_write ?? 0) || 0;
      const output = Number(ts.output_tokens ?? ts.output ?? 0) || 0;
      const pct = (v) => total > 0 ? Math.max(v / total * 100, v > 0 ? 1.5 : 0) : 0;
      return { total, read, input, write, output, readPct: pct(read), inputPct: pct(input), writePct: pct(write), outputPct: pct(output) };
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
      if (this.isRedTeamBoard()) {
        this.factViewMode = 'result';
        this.navigateTo('vulnerabilities');
        return;
      }
      this.factViewMode = 'vulnerability';
      this.vulnFilter = { ...(this.vulnFilter || {}), projectId: taskId, keyword: '' };
      this.selectedVulnerabilityKey = '';
      this.navigateTo('vulnerabilities');
    },

    humanFactCount() {
      return (this.project?.facts || []).filter(fact => !fact.archived && !['goal', 'origin'].includes(fact.id)).length;
    },

    humanArtifactCount() {
      const value = this.project?.project?.artifact_count;
      return Number.isFinite(Number(value)) ? Number(value) : (this.project?.artifacts || []).filter(item => !item.archived).length;
    },

    humanVulnerabilityCount() {
      const value = this.project?.project?.vulnerability_count;
      return Number.isFinite(Number(value)) ? Number(value) : (this.project?.vulns || []).filter(item => !item.archived).length;
    },

    humanSortedByCreated(items) {
      return [...(items || [])].sort((a, b) => {
        const created = (Date.parse(b?.created_at || '') || 0) - (Date.parse(a?.created_at || '') || 0);
        return created || String(b?.id || '').localeCompare(String(a?.id || ''));
      });
    },

    humanRecentFacts(limit = 3) {
      return this.humanSortedByCreated((this.project?.facts || []).filter(fact => (
        !fact.archived && !['goal', 'origin'].includes(fact.id)
      ))).slice(0, limit);
    },

    humanAllKnownArtifacts() {
      const items = [
        ...(this.project?.artifacts || []),
        ...(this.project?.lead_artifacts || []),
      ];
      return [...new Map(items.filter(item => item?.id).map(item => [item.id, item])).values()];
    },

    humanLeadArtifacts(limit = 3) {
      const known = (this.project?.lead_artifacts || []).filter(item => !item.archived);
      let items = known.filter(item => String(item.artifact_type || '').toLowerCase() === 'explore.leads');
      if (!items.length) {
        items = known.filter(item => {
          const type = String(item.artifact_type || '').toLowerCase();
          return type === 'bootstrap.initial_leads' || (type.startsWith('bootstrap.') && type.includes('lead'));
        });
      }
      if (!items.length) {
        items = (this.project?.artifacts || []).filter(item => (
          !item.archived && ['explore.leads', 'bootstrap.initial_leads'].includes(String(item.artifact_type || '').toLowerCase())
        ));
      }
      return this.humanSortedByCreated(items).slice(0, limit);
    },

    humanRecentArtifacts(limit = 6) {
      return this.humanSortedByCreated((this.project?.artifacts || []).filter(item => !item.archived)).slice(0, limit);
    },

    humanIntentRecord(intentId) {
      return (this.project?.intents || []).find(intent => intent.id === intentId) || null;
    },

    humanFactProducer(fact) {
      if (!fact?.id) return null;
      return (this.project?.intents || []).find(intent => (
        intent.to === fact.id || intent.to_fact_id === fact.id
      )) || null;
    },

    humanSourceIntent(item) {
      const intentId = item?.intent_id || item?.producer_intent_id || '';
      return intentId ? this.humanIntentRecord(intentId) : null;
    },

    humanNextStepIntents(intentId) {
      const intent = this.humanIntentRecord(intentId);
      if (!intent) return [];
      const producedIds = this.intentProducedFacts(intent).map(f => f.id);
      if (!producedIds.length) return [];
      return (this.project?.intents || []).filter(item =>
        !item.archived && item.id !== intentId &&
        (item.from || []).some(fid => producedIds.includes(fid))
      );
    },
    humanSourceLabel(item, fallback = '任务级') {
      const intent = item?.id?.startsWith?.('f') ? this.humanFactProducer(item) : this.humanSourceIntent(item);
      if (!intent) return fallback;
      return `来源 ${intent.id} · ${String(intent.description || '未命名 Intent').slice(0, 64)}`;
    },

    humanFactReferenceIds(fact) {
      const text = String(fact?.description || '');
      const ids = [...text.matchAll(/\bartifact:(a[0-9A-Za-z_-]+)\b/g)].map(match => match[1]);
      // 一行式格式：artifacts:a001/a002（复数引用逐个展开）。
      for (const match of text.matchAll(/\bartifacts:([a0-9][0-9A-Za-z_/-]*)\b/g)) {
        ids.push(...match[1].split('/'));
      }
      return ids;
    },

    humanArtifactForIntent(intentId, type = '') {
      return this.humanSortedByCreated(this.humanAllKnownArtifacts().filter(item => (
        !item.archived && item.intent_id === intentId && (!type || item.artifact_type === type)
      )))[0] || null;
    },

    humanFactArtifact(fact) {
      const artifacts = this.humanAllKnownArtifacts();
      const referenced = this.humanFactReferenceIds(fact)
        .map(id => artifacts.find(item => item.id === id))
        .find(Boolean);
      if (referenced) return referenced;
      const producer = this.humanFactProducer(fact);
      if (!producer) return null;
      const bootstrap = this.humanBootstrapIntent();
      if (producer.id === bootstrap?.id) return this.humanArtifactForIntent(producer.id, 'bootstrap.findings');
      return this.humanArtifactForIntent(producer.id, 'explore.findings');
    },

    // 从任务详情跳去文件中心前记住当前页签，返回时原样恢复
    markHumanTabReturn(taskId) {
      this._humanTabReturn = { taskId, tab: this.humanTab };
    },

    // 事实/线索卡片 hover：浮窗展示生产 Intent 详情（lite 模式，不带评估块）
    humanFactHover(event, fact) {
      const intent = this.humanFactProducer(fact);
      if (!intent) { this.intentHover = null; return; }
      this.intentHover = { d: intent, x: event.clientX, y: event.clientY, lite: true };
    },

    humanLeadHover(event, item) {
      const intent = this.humanSourceIntent(item);
      if (!intent) { this.intentHover = null; return; }
      this.intentHover = { d: intent, x: event.clientX, y: event.clientY, lite: true };
    },

    async openHumanFact(fact) {
      const taskId = this.selectedProjectId || this.project?.project?.id || '';
      if (!taskId || !fact?.id) return;
      this.markHumanTabReturn(taskId);
      const referencedIds = this.humanFactReferenceIds(fact);
      if (referencedIds.length) {
        await this.openArtifactLocation(taskId, referencedIds[0]);
        return;
      }
      const artifact = this.humanFactArtifact(fact);
      if (artifact?.id) {
        await this.openArtifactLocation(taskId, artifact.id);
        return;
      }
      const producer = this.humanFactProducer(fact);
      if (producer?.id) {
        this.navigateArtifactCenter(taskId, producer.id);
        this.showToast?.(`已打开 ${producer.id} 的产物目录`);
        return;
      }
      this.showToast?.('该事实没有可定位的产物或生产 Intent', 'error');
    },

    openHumanArtifact(item) {
      const taskId = this.selectedProjectId || this.project?.project?.id || item?.task_id || '';
      if (taskId) this.markHumanTabReturn(taskId);
      if (taskId && item?.id) this.openArtifactLocation(taskId, item);
    },

    humanArtifactLabel(item) {
      return String(item?.path || '').split('/').pop() || item?.id || '产物';
    },

    humanArtifactPhase(item) {
      const type = String(item?.artifact_type || '').toLowerCase();
      if (type.startsWith('bootstrap.')) return 'Bootstrap';
      if (type.startsWith('explore.')) return 'Explore';
      if (type.startsWith('reason.')) return 'Reason';
      if (type.startsWith('asm.')) return 'ASM';
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
      if (type.includes('evidence')) return 'Evidence';
      if (type.includes('asset')) return '攻击面';
      return item?.artifact_type || 'Artifact';
    },
  },
};
