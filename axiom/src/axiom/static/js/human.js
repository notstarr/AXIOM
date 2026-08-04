window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.human = {
  methods: {
    switchTaskView(mode) {
      this.view = ['graph', 'archive'].includes(mode) ? mode : 'human';
      this.showPageSkeleton();
      this.mountCurrentPage();
    },

    switchHumanTab(tab) {
      const allowed = ['workspace', 'conversation', 'todos', 'intents', 'facts', 'artifacts'];
      this.humanTab = allowed.includes(tab) ? tab : 'workspace';
      if (this.humanTab === 'intents') this.humanIntentLimit = 50;
      if (this.humanTab === 'facts') this.humanFactLimit = 50;
      if (this.humanTab === 'artifacts') {
        this.humanArtifactPage = 0;
        this.loadHumanArtifactPage?.();
      }
      if (this.humanTab === 'conversation') {
        this.selectExploreConversationIntent(this.exploreConversationIntentId || this.exploreConversationIntents()[0]?.id || '');
      } else if (this.exploreConversationTimer) {
        clearTimeout(this.exploreConversationTimer);
        this.exploreConversationTimer = null;
      }
    },

    exploreConversationIntents() {
      return this.humanSortedIntents((this.project?.intents || []).filter(intent => (
        !intent.archived && intent.kind === 'explore'
      )));
    },

    exploreConversationIntentStatus(intent) {
      const status = String(intent?.status || '').toLowerCase();
      if (['running', 'concluding'].includes(status)) return '运行中';
      if (status === 'claimed') return '已领取';
      if (status === 'pending') return '等待执行';
      if (status === 'completed') return '已完成';
      if (status === 'failed') return '执行失败';
      if (status === 'cancelled') return '已取消';
      return status || '未知';
    },

    exploreConversationIntentClass(intent) {
      const status = String(intent?.status || '').toLowerCase();
      if (['running', 'concluding'].includes(status)) return 'border-blue-300 bg-blue-50 text-blue-800 shadow-sm';
      if (status === 'claimed') return 'border-blue-100 bg-blue-50/50 text-blue-700';
      if (status === 'pending') return 'border-amber-100 bg-amber-50/50 text-amber-700';
      if (status === 'failed') return 'border-rose-100 bg-rose-50/50 text-rose-700';
      return 'border-slate-200 bg-slate-50/60 text-slate-500';
    },

    exploreConversationEventClass(event) {
      if (event?.source === 'intervention') return 'border-violet-200 bg-violet-50/70';
      if (event?.kind === 'tool_call') return 'border-blue-100 bg-blue-50/50';
      if (event?.kind === 'tool_result') return 'border-slate-200 bg-slate-50';
      if (event?.kind === 'action_summary') return 'border-emerald-100 bg-emerald-50/50';
      return 'border-amber-100 bg-amber-50/50';
    },

    async selectExploreConversationIntent(intentId) {
      if (!intentId) return;
      if (this.exploreConversationTimer) clearTimeout(this.exploreConversationTimer);
      this.exploreConversationTimer = null;
      this.exploreConversationIntentId = intentId;
      this.exploreConversationEvents = [];
      this.exploreConversationCursor = '0';
      await this.loadExploreConversation(true);
    },

    async loadExploreConversation(reset = false) {
      const taskId = this.selectedProjectId || this.project?.project?.id || '';
      const intentId = this.exploreConversationIntentId;
      if (!taskId || !intentId || this.humanTab !== 'conversation') return;
      if (this.exploreConversationLoading) return;
      this.exploreConversationLoading = true;
      try {
        const params = new URLSearchParams({ intent_id: intentId, limit: '300' });
        if (!reset && this.exploreConversationCursor && this.exploreConversationCursor !== '0') params.set('after_cursor', this.exploreConversationCursor);
        const payload = await this.api('GET', `/projects/${taskId}/explore-conversation?${params.toString()}`);
        if (Array.isArray(payload?.intents) && this.project) {
          const fresh = new Map(payload.intents.map(item => [item.id, item]));
          this.project.intents = (this.project.intents || []).map(item => (
            fresh.has(item.id) ? { ...item, ...fresh.get(item.id) } : item
          ));
        }
        if (reset) this.exploreConversationEvents = [];
        const seen = new Set((this.exploreConversationEvents || []).map(item => `${item.kind}:${item.created_at}:${item.detail}`));
        for (const item of (payload?.events || [])) {
          const key = `${item.kind}:${item.created_at}:${item.detail}`;
          if (!seen.has(key)) { this.exploreConversationEvents.push(item); seen.add(key); }
        }
        this.exploreConversationCursor = payload?.cursor || this.exploreConversationCursor || '0';
        this.exploreConversationIntent = payload?.intent || this.exploreConversationIntent;
      } catch (error) {
        if (!reset) this.showToast?.(error?.message || '读取 Explore 对话失败', 'error');
      } finally {
        this.exploreConversationLoading = false;
        if (this.humanTab === 'conversation' && this.exploreConversationIntentId === intentId) {
          this.exploreConversationTimer = setTimeout(() => this.loadExploreConversation(false), 1_000);
        }
      }
    },

    async sendExploreIntervention() {
      const taskId = this.selectedProjectId || this.project?.project?.id || '';
      const intentId = this.exploreConversationIntentId;
      const message = String(this.exploreConversationDraft || '').trim();
      const intent = this.exploreConversationIntents().find(item => item.id === intentId);
      if (!taskId || !intentId || !message || this.exploreConversationSending) return;
      if (!['claimed', 'running', 'concluding'].includes(String(intent?.status || '').toLowerCase())) {
        this.showToast?.('只有存在活动 Lease 的 Explore 才能插话', 'error');
        return;
      }
      this.exploreConversationSending = true;
      try {
        await this.api('POST', `/projects/${taskId}/intents/${intentId}/interventions`, {
          message,
          client_message_id: `web-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`,
          actor: this.actorName?.() || 'human.frontend',
        });
        this.exploreConversationDraft = '';
        await this.loadExploreConversation(true);
        this.showToast?.('指导消息已排队，将在下一轮模型调用前发送');
      } catch (error) {
        this.showToast?.(error?.message || '发送指导消息失败', 'error');
      } finally {
        this.exploreConversationSending = false;
      }
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
      return `human-stage-icon human-stage-${state}`;
    },

    humanStageLabel(stage) {
      return this.humanStageState(stage)?.label || '等待中';
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
      if (this.humanStageState('reason').state === 'running') return 'Reason 正在汇总 Facts、更新 Todo 并决定下一批 Intent。';
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

    humanFactCount() {
      return (this.project?.facts || []).filter(fact => !fact.archived && !['goal', 'origin'].includes(fact.id)).length;
    },

    humanIntentCount() {
      return (this.project?.intents || []).filter(intent => !intent.archived && intent !== this.humanBootstrapIntent()).length;
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
        ...(this.humanArtifactItems || []),
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

    humanSourceLabel(item, fallback = '任务级') {
      const intent = item?.id?.startsWith?.('f') ? this.humanFactProducer(item) : this.humanSourceIntent(item);
      if (!intent) return fallback;
      return `来源 ${intent.id} · ${String(intent.description || '未命名 Intent').slice(0, 64)}`;
    },

    humanFactReferenceIds(fact) {
      const text = String(fact?.description || '');
      return [...text.matchAll(/\bartifact:(a[0-9A-Za-z_-]+)\b/g)].map(match => match[1]);
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

    async openHumanFact(fact) {
      const taskId = this.selectedProjectId || this.project?.project?.id || '';
      if (!taskId || !fact?.id) return;
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
      if (type.includes('asset')) return '攻击面';
      return item?.artifact_type || 'Artifact';
    },
  },
};
