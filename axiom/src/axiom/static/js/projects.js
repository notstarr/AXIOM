window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.projects = {
  methods: {
    projectIsActive() {
      return !this.replay.active && this.project?.project?.status === 'active';
    },

    countProjectsByStatus(status) {
      return this.projects.filter(project => project.status === status).length;
    },

    hasActiveProjects() {
      return this.projects.some(project => project.status === 'active');
    },

    projectCanWriteHints() {
      return !this.replay.active && ['active', 'stopped', 'completed'].includes(this.project?.project?.status);
    },

    canActOnSelectedFacts() {
      return this.projectIsActive() && this.selectedFacts.length > 0 && !this.selectedFacts.includes('goal');
    },

    async loadProjects() {
      this.projectsLoading = true;
      this.projectsError = '';
      try {
        this.projects = await this.api('GET', '/projects');
        this.projectsLoaded = true;
        this.pruneDeletedProjectCaches();
        if (!this.selectedTaskManagementId && this.projects.length > 0) {
          this.selectedTaskManagementId = this.projects[0].id;
        }
        if (!this.selectedProjectManagementTaskId && this.projects.length > 0) {
          this.selectedProjectManagementTaskId = this.tasksForGroup(this.selectedProjectManagementId)[0]?.id || '';
        }
      } catch(e) {
        this.projectsError = e?.message || String(e);
        console.error(e);
      } finally {
        this.projectsLoading = false;
      }
    },

    pruneDeletedProjectCaches() {
      const liveIds = new Set((this.projects || []).map(project => project.id));
      for (const cache of [this.projectDetailsCache, this.projectEventsCache, this.taskDiagnostics, this.taskDiagnosticsLoading]) {
        for (const id of Object.keys(cache || {})) {
          if (!liveIds.has(id)) delete cache[id];
        }
      }
      if (this.selectedProjectId && !liveIds.has(this.selectedProjectId)) {
        this.project = null;
        this.selectedProjectId = '';
        this.selectedNode = null;
        this.selectedFacts = [];
        this.seeEvents = [];
        this.seeLastEventId = 0;
      }
    },

    async loadProjectGroups() {
      try {
        this.projectGroups = await this.api('GET', '/project-groups');
        if (!this.selectedProjectManagementId && this.projectGroups.length > 0) {
          this.selectedProjectManagementId = this.projectGroups[0].id;
        }
        if (!this.newProject.parent_project_id && this.projectGroups.length > 0) {
          this.newProject.parent_project_id = this.projectGroups[0].id;
        }
      } catch(e) { console.error(e); }
    },

    async createProjectGroup() {
      if (!this.projectGroupForm.title.trim()) return;
      try {
        const group = await this.api('POST', '/project-groups', {
          title: this.projectGroupForm.title,
          description: this.projectGroupForm.description || '',
        });
        this.projectGroupForm = { title: '', description: '' };
        this.showGroupModal = false;
        await this.loadProjectGroups();
        this.selectManagedProject(group.id);
        this.newProject.parent_project_id = group.id;
        this.showToast('项目已创建');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    openGroupModal() {
      this.projectGroupForm = { title: '', description: '' };
      this.showGroupModal = true;
    },

    async openTaskComposer(preselectParentProjectId = '') {
      try {
        if (!this.dispatchSettings || !Array.isArray(this.dispatchSettings.workers)) {
          await this.loadDispatchSettings();
        }
        await this.loadJudgeSettings();
      } catch(e) { console.error('load settings failed', e); }
      const workers = (this.dispatchSettings && this.dispatchSettings.workers) || [];
      const defaults = workers
        .filter(w => w && w.name && !w.disabled)
        .map(w => w.name);
      if (preselectParentProjectId) {
        this.newProject.parent_project_id = preselectParentProjectId;
      } else if (!this.newProject.parent_project_id && this.projectGroups.length > 0) {
        this.newProject.parent_project_id = this.projectGroups[0].id;
      }
      this.newProject.worker_pool = defaults;
      if (!String(this.newProject.goal || '').trim()) {
        this.newProject.goal = '对该系统进行渗透测试，并挖掘10个高置信度的漏洞';
      }
      if (!this.newProject._guardTouched) this.newProject.guard_enabled = !!this.judgeSettings?.enabled_default;
      this.taskComposerStep = 1;
      this.showTaskComposer = true;
    },

    deleteProjectGroup(id, title) {
      this.deleteGroupConfirm = { id, title };
      this.showDeleteGroupModal = true;
    },

    async confirmDeleteProjectGroup() {
      const id = this.deleteGroupConfirm.id;
      if (!id) return;
      try {
        await this.api('DELETE', `/project-groups/${id}`);
        this.showDeleteGroupModal = false;
        this.deleteGroupConfirm = { id: '', title: '' };
        if (this.selectedProjectManagementId === id) {
          this.selectedProjectManagementId = '';
        }
        await this.loadProjectGroups();
        await this.loadProjects();
        this.showToast('项目已删除');
      } catch (e) {
        this.showToast('删除失败: ' + (e.message || e), 'error');
      }
    },

    async loadAllProjectDetails(force = false) {
      const ids = this.projects.map(project => project.id);
      await Promise.all(ids.map(async (id) => {
        if (!force && this.projectDetailsCache[id]) return;
        try {
          const detail = await this.api('GET', `/projects/${id}`);
          this.projectDetailsCache[id] = detail;
        } catch (e) {
          console.error(e);
        }
      }));
      if (this.activePage === 'dashboard' || this.activePage === 'task-detail') {
        await Promise.all(ids.map(async (id) => {
          // For task-detail only load the selected project; for dashboard load all
          if (this.activePage === 'task-detail' && id !== this.selectedProjectId) return;
          if (!force && this.projectEventsCache[id]) return;
          try {
            const events = await this.api('GET', `/projects/${id}/events?after_id=0&limit=200`);
            this.projectEventsCache[id] = events || [];
          } catch (e) {
            console.error(e);
          }
        }));
      }
    },

    async refreshDashboardPage(options = {}) {
      await this.loadProjectGroups();
      await this.loadProjects();
      await this.loadAllProjectDetails(true);
      await this.loadGlobalSkillUsage();
      if (!options.silent) this.showToast('仪表盘已刷新');
    },

    async loadGlobalSkillUsage() {
      try {
        const resp = await fetch('/skill-usage');
        if (resp.ok) {
          const data = await resp.json();
          this.globalSkillUsage = data.items || [];
        }
      } catch (e) { /* ignore */ }
    },

    allProjectDetails() {
      const liveIds = new Set((this.projects || []).map(project => project.id));
      const details = Object.values(this.projectDetailsCache || {})
        .filter(item => liveIds.has(item?.project?.id));
      if (this.project && this.selectedProjectId) {
        const withoutSelected = details.filter(item => item?.project?.id !== this.selectedProjectId);
        return [this.project, ...withoutSelected];
      }
      return details;
    },

    selectedTaskSummary() {
      const tasks = this.projects || [];
      if (!this.selectedTaskManagementId && tasks.length > 0) this.selectedTaskManagementId = tasks[0].id;
      const selected = tasks.find(task => task.id === this.selectedTaskManagementId) || tasks[0] || null;
      if (selected && this.taskWorkerPoolEditor.projectId !== selected.id) {
        this.syncTaskWorkerPoolEditor(selected);
      }
      return selected;
    },

    selectedTaskDiagnostics() {
      const selected = this.selectedTaskSummary();
      if (!selected) return null;
      return this.taskDiagnostics[selected.id] || null;
    },

    async loadTaskDiagnostics(projectId = '', silent = false) {
      const id = projectId || this.selectedTaskSummary()?.id || '';
      if (!id) return null;
      try {
        this.taskDiagnosticsLoading[id] = true;
        const data = await this.api('GET', `/projects/${id}/diagnostics`);
        this.taskDiagnostics = { ...(this.taskDiagnostics || {}), [id]: data };
        if (!silent) this.showToast('任务诊断已刷新');
        return data;
      } catch (e) {
        if (!silent) this.showToast(e.message || '加载任务诊断失败', 'error');
        return null;
      } finally {
        this.taskDiagnosticsLoading[id] = false;
      }
    },

    taskHealthClass(status) {
      if (status === 'danger') return 'bg-rose-50 text-rose-700 border-rose-100';
      if (status === 'warning') return 'bg-amber-50 text-amber-700 border-amber-100';
      return 'bg-emerald-50 text-emerald-700 border-emerald-100';
    },

    taskHealthLabel(status) {
      if (status === 'danger') return '异常';
      if (status === 'warning') return '需关注';
      return '正常';
    },

    configEffectiveLabel(diag = null) {
      const status = diag?.config_status || {};
      if (!status.configured_version) return '未加载配置版本';
      if (status.is_current) return `已生效 v${status.applied_version}`;
      return `待应用：配置 v${status.configured_version} / dispatcher v${status.applied_version || 0}`;
    },

    workerCapabilityLine(worker) {
      const phases = (worker.task_types || []).join('/') || '无阶段';
      const mcp = worker.mcp_enabled ? `MCP ${this.workerVisibleMcpToolCount(worker)} 工具` : 'MCP 关';
      const model = worker.model || '未配置模型';
      return `${phases} · ${model} · ${mcp}`;
    },

    workerVisibleMcpToolCount(worker) {
      return (worker.mcp_servers || []).reduce((total, server) => total + Number(server.visible_enabled_tool_count || 0), 0);
    },

    blockerLabel(blocker) {
      if (!blocker) return '';
      return `${blocker.label || blocker.code} · ${blocker.count || 0}`;
    },

    diagnosticPhaseLabel(phase) {
      const labels = {
        bootstrap: 'Bootstrap 榜',
        explore: 'Explore 榜',
        reason: 'Reason 榜',
      };
      return labels[phase] || phase || '未知阶段';
    },

    diagnosticPhaseBoard(phase) {
      const rankings = this.selectedTaskDiagnostics()?.worker_rankings || {};
      return rankings.boards?.[phase] || { task_type: phase, total: 0, success: 0, failed: 0, timed_out: 0, other: 0, workers: [], reasons: [] };
    },

    diagnosticCompletionRate(worker) {
      const rate = Number(worker?.completion_rate || 0) * 100;
      return `${rate.toFixed(rate === 100 || rate === 0 ? 0 : 1)}%`;
    },

    diagnosticFailureRate(worker) {
      const rate = Number(worker?.failure_timeout_rate || 0) * 100;
      return `${rate.toFixed(rate === 100 || rate === 0 ? 0 : 1)}%`;
    },

    diagnosticReasonText(reason) {
      if (!reason) return '无原因';
      return `${reason.label || reason.code || '未归类'} · ${reason.count || 0}`;
    },

    diagnosticWorkerTopReasons(worker) {
      const reasons = (worker?.reasons || []).slice(0, 2).map(reason => this.diagnosticReasonText(reason));
      return reasons.length ? reasons.join(' / ') : '暂无失败或超时';
    },

    tokenRankings() {
      return this.selectedTaskDiagnostics()?.token_rankings || { totals: {}, by_worker: [], by_phase: [] };
    },

    formatTokenCount(value) {
      const num = Number(value || 0);
      if (num >= 1000000) return `${(num / 1000000).toFixed(2)}M`;
      if (num >= 10000) return `${(num / 1000).toFixed(1)}K`;
      return `${Math.round(num)}`;
    },

    tokenWorkerMeta(row) {
      const phases = (row?.phases || []).map(phase => this.diagnosticPhaseLabel(phase).replace(' 榜', '')).join(' / ');
      return `${row?.calls || 0} 次调用${phases ? ` · ${phases}` : ''}`;
    },

    tokenPhaseMeta(row) {
      return `${row?.calls || 0} 次调用 · ${row?.worker_count || 0} 个 worker`;
    },

    tokenBreakdownText(row) {
      return `输入 ${this.formatTokenCount(row?.input_tokens)} · 输出 ${this.formatTokenCount(row?.output_tokens)} · 缓存 ${this.formatTokenCount((row?.cache_read_tokens || 0) + (row?.cache_write_tokens || 0))} · 推理 ${this.formatTokenCount(row?.reasoning_tokens)}`;
    },

    enabledDispatchWorkers() {
      return ((this.dispatchSettings && this.dispatchSettings.workers) || [])
        .filter(worker => worker && worker.name && !worker.disabled);
    },

    mcpEnabledDispatchWorkers() {
      return this.enabledDispatchWorkers().filter(worker => !!worker.mcp_enabled);
    },

    syncTaskWorkerPoolEditor(task = null) {
      const selected = task || this.selectedTaskSummary();
      if (!selected) {
        this.taskWorkerPoolEditor = { projectId: '', worker_pool: [], saving: false };
        return;
      }
      this.taskWorkerPoolEditor = {
        projectId: selected.id,
        worker_pool: Array.isArray(selected.worker_pool) ? [...selected.worker_pool] : [],
        saving: false,
      };
    },

    setTaskWorkerPool(mode) {
      if (mode === 'all') {
        this.taskWorkerPoolEditor.worker_pool = this.enabledDispatchWorkers().map(worker => worker.name);
      } else if (mode === 'mcp') {
        this.taskWorkerPoolEditor.worker_pool = this.mcpEnabledDispatchWorkers().map(worker => worker.name);
      } else if (mode === 'clear') {
        this.taskWorkerPoolEditor.worker_pool = [];
      }
    },

    taskWorkerPoolChanged(task = null) {
      const selected = task || this.selectedTaskSummary();
      if (!selected || this.taskWorkerPoolEditor.projectId !== selected.id) return false;
      const original = Array.isArray(selected.worker_pool) ? selected.worker_pool : [];
      const edited = Array.isArray(this.taskWorkerPoolEditor.worker_pool) ? this.taskWorkerPoolEditor.worker_pool : [];
      return original.join('\u0000') !== edited.join('\u0000');
    },

    async saveTaskWorkerPool() {
      const selected = this.selectedTaskSummary();
      if (!selected || this.taskWorkerPoolEditor.saving) return;
      try {
        this.taskWorkerPoolEditor.saving = true;
        const workerPool = Array.isArray(this.taskWorkerPoolEditor.worker_pool)
          ? this.taskWorkerPoolEditor.worker_pool
          : [];
        const response = await this.api('PUT', `/projects/${selected.id}/worker-pool`, {
          worker_pool: workerPool,
        });
        const idx = this.projects.findIndex(project => project.id === selected.id);
        if (idx >= 0) this.projects.splice(idx, 1, { ...this.projects[idx], ...response.project });
        if (this.project?.project?.id === selected.id) this.project.project = response.project;
        if (this.projectDetailsCache[selected.id]?.project) this.projectDetailsCache[selected.id].project = response.project;
        this.syncTaskWorkerPoolEditor(response.project);
        this.showToast(`Worker 配置已保存，已同步 ${response.updated_unclaimed_intents || 0} 个未领取意图`);
      } catch (e) {
        this.showToast(e.message || '保存 Worker 配置失败', 'error');
      } finally {
        this.taskWorkerPoolEditor.saving = false;
      }
    },

    selectedVulnerability() {
      const vulns = this.filteredVulnerabilities();
      if (vulns.length === 0) return null;
      const selected = vulns.find(vuln => this.vulnerabilityKey(vuln) === this.selectedVulnerabilityKey);
      if (selected) return selected;
      this.selectedVulnerabilityKey = this.vulnerabilityKey(vulns[0]);
      return vulns[0];
    },

    async loadProject(id) {
      try {
        this.project = await this.api('GET', `/projects/${id}`);
        this.projectDetailsCache[id] = this.project;
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async loadConversation(projectId = '', options = {}) {
      const id = projectId || this.selectedProjectId;
      if (!id) return null;
      const filter = options.reset ? { thread_type: '' } : (this.conversationFilter || {});
      if (options.reset) this.conversationFilter = filter;
      const params = new URLSearchParams();
      for (const key of ['thread_type']) {
        if (filter[key]) params.set(key, filter[key]);
      }
      try {
        this.conversationLoading = true;
        const suffix = params.toString() ? `?${params.toString()}` : '';
        const data = await this.api('GET', `/projects/${id}/conversation/index${suffix}`);
        this.conversation = {
          project_id: data.project_id || id,
          threads: data.threads || [],
          items: [],
          stats: data.stats || { workers: [], intents: [], thread_types: { intent: 0, reason: 0, maintainer: 0 }, timeout_count: 0, thread_count: 0, item_count: 0 },
          filters: data.filters || {},
        };
        this.conversationThreadItems = {};
        this.conversationThreadLoading = {};
        if (!this.conversationFilter.thread_type) {
          const firstProblem = (this.conversation.threads || []).find(t => t.status === 'timeout' || t.status === 'failed');
          if (firstProblem) {
            this.expandedConversationThreads[firstProblem.id] = true;
            this.loadConversationThread(firstProblem.id, { silent: true });
          }
        }
        return this.conversation;
      } catch (e) {
        if (!options.silent) this.showToast(e.message || '加载对话失败', 'error');
        return null;
      } finally {
        this.conversationLoading = false;
      }
    },

    async applyConversationFilter(patch = {}) {
      this.conversationFilter = { ...(this.conversationFilter || {}), ...patch };
      await this.loadConversation(this.selectedProjectId, { silent: true });
      this.sideTab = 'conversation';
    },

    clearConversationFilter() {
      this.conversationFilter = { thread_type: '' };
      return this.loadConversation(this.selectedProjectId, { silent: true });
    },

    conversationThreads() {
      return (this.conversation && this.conversation.threads) || [];
    },

    conversationItems() {
      return (this.conversation && this.conversation.items) || [];
    },

    conversationItemsForThread(threadId) {
      return this.conversationThreadItems?.[threadId] || this.conversationItems().filter(item => item.thread_id === threadId);
    },

    conversationItemsForWorker(threadId, workerName) {
      return this.conversationItemsForThread(threadId).filter(item => (item.worker || 'unknown') === workerName);
    },

    async toggleConversationThread(threadId) {
      const willExpand = !this.expandedConversationThreads?.[threadId];
      this.expandedConversationThreads = {
        ...(this.expandedConversationThreads || {}),
        [threadId]: willExpand,
      };
      if (willExpand) await this.loadConversationThread(threadId);
    },

    async loadConversationThread(threadId, options = {}) {
      const id = this.selectedProjectId;
      if (!id || !threadId) return null;
      if (!options.force && this.conversationThreadItems?.[threadId]) return this.conversationThreadItems[threadId];
      this.conversationThreadLoading = { ...(this.conversationThreadLoading || {}), [threadId]: true };
      try {
        const params = new URLSearchParams({ thread_id: threadId, limit: '3000' });
        const data = await this.api('GET', `/projects/${id}/conversation?${params.toString()}`);
        const loadedThread = (data.threads || []).find(t => t.id === threadId);
        if (loadedThread && this.conversation?.threads) {
          this.conversation.threads = this.conversation.threads.map(t => t.id === threadId ? { ...t, ...loadedThread } : t);
        }
        this.conversationThreadItems = { ...(this.conversationThreadItems || {}), [threadId]: data.items || [] };
        return data.items || [];
      } catch (e) {
        if (!options.silent) this.showToast(e.message || '加载对话内容失败', 'error');
        return null;
      } finally {
        this.conversationThreadLoading = { ...(this.conversationThreadLoading || {}), [threadId]: false };
      }
    },

    conversationThreadLoadingState(threadId) {
      return !!this.conversationThreadLoading?.[threadId];
    },

    conversationThreadExpanded(thread) {
      if (!thread) return false;
      return !!this.expandedConversationThreads?.[thread.id];
    },

    toggleConversationWorker(threadId, workerName) {
      const key = `${threadId}::${workerName || 'unknown'}`;
      this.expandedConversationWorkers = {
        ...(this.expandedConversationWorkers || {}),
        [key]: !this.expandedConversationWorkers?.[key],
      };
    },

    conversationWorkerExpanded(threadId, workerName) {
      return !!this.expandedConversationWorkers?.[`${threadId}::${workerName || 'unknown'}`];
    },

    conversationStatusClass(status) {
      if (status === 'success') return 'bg-emerald-50 text-emerald-700 border-emerald-100';
      if (status === 'recovered') return 'bg-sky-50 text-sky-700 border-sky-100';
      if (status === 'timeout') return 'bg-amber-50 text-amber-700 border-amber-100';
      if (status === 'failed') return 'bg-rose-50 text-rose-700 border-rose-100';
      if (status === 'running') return 'bg-violet-50 text-violet-700 border-violet-100';
      if (status === 'pending') return 'bg-slate-50 text-slate-500 border-slate-200';
      return 'bg-slate-100 text-slate-600 border-slate-200';
    },

    conversationKindClass(kind) {
      if (kind === 'thinking') return 'bg-violet-50 text-violet-700 border-violet-100';
      if (kind === 'assistant') return 'bg-brand-50 text-brand-700 border-brand-100';
      if (kind === 'tool') return 'bg-slate-50 text-slate-700 border-slate-200';
      if (kind === 'timeout') return 'bg-amber-50 text-amber-700 border-amber-100';
      if (kind === 'token') return 'bg-fuchsia-50 text-fuchsia-700 border-fuchsia-100';
      return 'bg-white text-slate-600 border-slate-200';
    },

    conversationKindLabel(kind) {
      return ({ thinking: '思考', assistant: '回复', tool: '工具', timeout: '超时', token: 'Token', system: '系统' })[kind] || kind || '事件';
    },

    conversationStatusLabel(status) {
      return ({ success: '成功', recovered: '已接替', timeout: '超时', failed: '失败', running: '运行中', cancelled: '取消', pending: '待执行' })[status] || status || '未知';
    },

    conversationActiveFilterLabel() {
      const f = this.conversationFilter || {};
      const parts = [];
      if (f.thread_type) parts.push(this.conversationThreadTypeLabel(f.thread_type));
      return parts.join(' · ');
    },

    conversationThreadTypeLabel(type) {
      return ({ intent: 'Intent', reason: 'Reason', maintainer: 'Maintainer' })[type] || type || '';
    },

    conversationItemPreview(item) {
      const text = item?.content || item?.payload?.stderr || item?.payload?.stdout || '';
      return String(text || '').slice(0, 600);
    },

    conversationTokenLabel(item) {
      const usage = item?.payload?.token_usage || {};
      const total = usage.total_tokens || usage.totalTokens || usage.total || 0;
      const reasoning = usage.reasoning_tokens || usage.reasoningTokens || usage.reasoning || 0;
      if (!total && !reasoning) return '';
      return `total ${Number(total || 0).toLocaleString()}${reasoning ? ` · reasoning ${Number(reasoning).toLocaleString()}` : ''}`;
    },

    conversationTimeoutSuggestions(item) {
      return item?.diagnostic?.suggestions || [];
    },

    async createProject() {
      try {
        if (this.projectGroups.length === 0) await this.loadProjectGroups();
        const parentProjectId = this.newProject.parent_project_id || this.projectGroups[0]?.id || '';
        if (!parentProjectId) {
          this.showToast('请先创建项目，再创建任务', 'error');
          return;
        }

        const actor = this.actorName();
        const body = {
          parent_project_id: parentProjectId,
          title: this.newProject.title,
          origin: this.newProject.origin,
          goal: this.newProject.goal,
          kk_report_enabled: !!this.newProject.kk_report_enabled,
          guard_enabled: !!this.newProject.guard_enabled,
          hints: this.newProject.hints.trim()
            ? [{ content: this.newProject.hints.trim(), creator: actor }]
            : [],
          worker_pool: Array.isArray(this.newProject.worker_pool) ? this.newProject.worker_pool : [],
          task_scope: {
            enabled: !!this.newProject.task_scope_enabled,
            targets: String(this.newProject.task_scope_targets || '').split(/\n|,/).map(x => x.trim()).filter(Boolean),
            exclude: String(this.newProject.task_scope_exclude || '').split(/\n|,/).map(x => x.trim()).filter(Boolean),
            notes: this.newProject.task_scope_notes || '',
          },
        };
        const data = await this.api('POST', '/projects', body);
        const pid = data.project.id;
        this.showTaskComposer = false;
        this.taskComposerStep = 1;
        this.newProject = {
          parent_project_id: parentProjectId || this.projectGroups[0]?.id || '',
          title:'', origin:'', goal:'对该系统进行渗透测试，并挖掘10个高置信度的漏洞',
          bootstrap: true, kk_report_enabled: false, depth_mode: false, guard_enabled: !!this.judgeSettings?.enabled_default, hints: '',
          task_scope_enabled: false, task_scope_targets: '', task_scope_exclude: '', task_scope_notes: '',
          depthInputMode: 'har', crawlerTaskId: '', worker_pool: [], _guardTouched: false,
        };
        await this.loadProjectGroups();
        await this.loadProjects();
        await this.openProject(pid);
        this.showToast('任务已创建');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openCreateIntent() {
      if (!this.canActOnSelectedFacts()) return;
      const scope = this.project?.project?.task_scope || {};
      const allow = scope.targets || [];
      const deny = scope.exclude || [];
      let targetsDefault = '';
      if (allow.length > 0) targetsDefault = allow.join('\n');
      this.intentForm = { description: '', targets: targetsDefault };
      this.intentScopeHint = deny.length > 0
        ? (allow.length > 0 ? `任务白名单+黑名单约束：${allow.join(', ')}（排除 ${deny.join(', ')}）` : `任务黑名单约束（不得包含：${deny.join(', ')}）`)
        : (allow.length > 0 ? `任务白名单：${allow.join(', ')}` : '无范围限制');
      this.showIntentModal = true;
    },

    selectManagedProject(projectId) {
      this.selectedProjectManagementId = projectId;
      this.reloadBlackboardGroup(projectId);
      const firstTask = this.tasksForGroup(projectId)[0];
      this.selectedProjectManagementTaskId = firstTask?.id || '';
      if (this.selectedProjectManagementTaskId) this.loadProjectDetailForManagement(this.selectedProjectManagementTaskId);
    },

    async loadProjectDetailForManagement(projectId) {
      if (!projectId) return;
      if (!this.projectDetailsCache[projectId]) {
        try {
          this.projectDetailsCache[projectId] = await this.api('GET', `/projects/${projectId}`);
        } catch (e) {
          this.showToast(e.message, 'error');
        }
      }
      const detail = this.projectDetailsCache[projectId];
      if (detail?.project?.parent_project_id) {
        await this.reloadBlackboardGroup(detail.project.parent_project_id);
      }
    },

    managedProjectDetail() {
      return this.projectDetailsCache[this.selectedProjectManagementTaskId] || null;
    },

    projectCanWriteManagedBlackboard() {
      const status = this.managedProjectDetail()?.project?.status;
      return ['active', 'stopped', 'completed'].includes(status);
    },

    selectedProjectGroup() {
      return this.projectGroups.find(group => group.id === this.selectedProjectManagementId) || null;
    },

    tasksForGroup(groupId) {
      return this.projects.filter(task => (task.parent_project_id || '') === groupId);
    },

    selectedProjectGroupTasks() {
      return this.tasksForGroup(this.selectedProjectManagementId);
    },

    projectGroupTitle(groupId) {
      return this.projectGroups.find(group => group.id === groupId)?.title || '未归属项目';
    },

    rememberProjectListScroll() {
      const scroller = this.$refs.projectListScroll;
      if (!scroller) return;
      this.projectListScrollTop = scroller.scrollTop;
    },

    restoreProjectListScroll() {
      if (!this.shouldRestoreProjectListScroll) return;
      this.$nextTick(() => {
        const scroller = this.$refs.projectListScroll;
        if (!scroller) return;
        const maxScrollTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
        scroller.scrollTop = Math.min(this.projectListScrollTop, maxScrollTop);
        this.shouldRestoreProjectListScroll = false;
      });
    },

    deleteConfirmLabel() {
      return this.projectLabel(this.deleteConfirm.id, this.deleteConfirm.title);
    },

    requestDeleteProject(projectId, projectTitle = '') {
      if (!projectId) return;
      this.deleteConfirm = { id: projectId, title: projectTitle };
      this.showDeleteModal = true;
    },

    closeDeleteModal(force = false) {
      if (this.isDeletingProject && !force) return;
      this.showDeleteModal = false;
      this.deleteConfirm = { id: '', title: '' };
    },

    deleteProject() {
      if (!this.selectedProjectId) return;
      this.requestDeleteProject(this.selectedProjectId, this.project?.project?.title || '');
    },

    deleteProjectById(projectId, projectTitle = '') {
      this.requestDeleteProject(projectId, projectTitle);
    },

    async confirmDeleteProject() {
      const projectId = this.deleteConfirm.id;
      if (!projectId || this.isDeletingProject) return;
      try {
        this.isDeletingProject = true;
        const wasSelected = this.selectedProjectId === projectId;
        await this.api('DELETE', `/projects/${projectId}`);
        delete this.projectDetailsCache[projectId];
        delete this.projectEventsCache[projectId];
        delete this.taskDiagnostics[projectId];
        delete this.taskDiagnosticsLoading[projectId];
        if (String(this.selectedVulnerabilityKey || '').startsWith(`${projectId}:`)) {
          this.selectedVulnerabilityKey = '';
        }
        if (wasSelected) {
          this.project = null;
          this.selectedProjectId = '';
          this.selectedNode = null;
          this.selectedFacts = [];
        }
        this.closeDeleteModal(true);
        if (wasSelected) {
          this.backToList();
        } else {
          await this.loadProjects();
        }
        await this.loadProjectGroups();
        this.showToast(`已删除 ${projectId}`);
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.isDeletingProject = false;
        if (!this.showDeleteModal) this.deleteConfirm = { id: '', title: '' };
      }
    },

    async toggleProjectStop() {
      if (!this.selectedProjectId || !this.project || this.project.project.status === 'completed') return;
      const nextStatus = this.project.project.status === 'active' ? 'stopped' : 'active';
      try {
        await this.setProjectStatus(this.selectedProjectId, nextStatus);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async toggleProjectStopById(projectId, currentStatus) {
      if (!projectId || currentStatus === 'completed') return;
      const nextStatus = currentStatus === 'active' ? 'stopped' : 'active';
      try {
        await this.setProjectStatus(projectId, nextStatus);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async setProjectStatus(projectId, status, options = {}) {
      const { reload = true, toast = true } = options;
      const updated = await this.api('PUT', `/projects/${projectId}/status`, { status });
      const projectSummary = this.projects.find(item => item.id === projectId);
      if (projectSummary) {
        projectSummary.status = updated.status;
        projectSummary.reason = updated.reason;
      }
      if (this.selectedProjectId === projectId && this.project) {
        this.project.project.status = updated.status;
        this.project.project.reason = updated.reason;
        if (!this.projectIsActive()) {
          this.showIntentModal = false;
          this.showConcludeModal = false;
          this.showCompleteModal = false;
        }
        if (!this.projectCanWriteHints()) {
          this.showHintModal = false;
        }
      }
      if (reload) {
        await this.loadProjects();
        await this.loadProjectGroups();
      }
      if (toast) this.showToast(status === 'stopped' ? '任务已暂停' : '任务已继续');
      return updated;
    },

    async stopAllActiveProjects() {
      const activeProjects = this.projects.filter(project => project.status === 'active');
      if (activeProjects.length === 0 || this.isStoppingAllProjects) return;
      this.isStoppingAllProjects = true;
      try {
        const results = await Promise.allSettled(
          activeProjects.map(project => this.setProjectStatus(project.id, 'stopped', { reload: false, toast: false }))
        );
        await this.loadProjects();
        await this.loadProjectGroups();
        const successCount = results.filter(result => result.status === 'fulfilled').length;
        const failureCount = results.length - successCount;
        if (failureCount === 0) {
          this.showToast(`已暂停 ${successCount} 个运行中任务`);
        } else {
          this.showToast(`已暂停 ${successCount} 个任务，${failureCount} 个失败`, 'error');
        }
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.isStoppingAllProjects = false;
      }
    },

    openReopenProject(projectId, projectTitle = '') {
      if (!projectId) return;
      this.reopenForm = { projectId, projectTitle, description: '' };
      this.showReopenModal = true;
    },

    openRenameProject(projectId, projectTitle = '') {
      if (!projectId) return;
      this.renameForm = { projectId, originalTitle: projectTitle, title: projectTitle };
      this.showRenameModal = true;
      this.$nextTick(() => {
        this.$refs.renameTitleInput?.focus();
        this.$refs.renameTitleInput?.select?.();
      });
    },

    async renameProject() {
      const projectId = this.renameForm.projectId || this.selectedProjectId;
      if (!projectId) return;
      try {
        const updated = await this.api('PUT', `/projects/${projectId}/title`, {
          title: this.renameForm.title,
        });
        const projectSummary = this.projects.find(item => item.id === projectId);
        if (projectSummary) {
          projectSummary.title = updated.title;
        }
        if (this.selectedProjectId === projectId && this.project) {
          this.project.project.title = updated.title;
        }
        this.showRenameModal = false;
        this.renameForm = { projectId: '', originalTitle: '', title: '' };
        await this.loadProjects();
        this.showToast('项目已重命名');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async reopenProject() {
      const projectId = this.reopenForm.projectId || this.selectedProjectId;
      if (!projectId) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${projectId}/reopen`, {
          description: this.reopenForm.description,
          creator: actor,
        });
        this.showReopenModal = false;
        this.reopenForm = { projectId: '', projectTitle: '', description: '' };
        await this.loadProjects();
        if (this.selectedProjectId === projectId && this.view === 'graph') {
          await this.loadProject(projectId);
          this.updateGraph();
        }
        await this.loadProjectGroups();
        this.showToast('任务已重新打开');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async openTaskOverview(id, fromRoute = false) {
      return this.openProject(id, fromRoute, 'human');
    },

    async openTaskGraph(id, fromRoute = false) {
      return this.openProject(id, fromRoute, 'graph');
    },

    async openProject(id, fromRoute = false, initialView = 'human') {
      if (this.replay.active) {
        this.stopReplayTimer();
        this.replay.active = false;
        this.replay.playing = false;
        this.replay.frames = [];
        this.replay.visibleEvents = [];
        this.replay.frameIndex = -1;
        this.replay.sourceProject = null;
        this._timelineEventsCacheProject = null;
        this._timelineEventsCache = [];
        this.polling = true;
      }
      this.rememberProjectListScroll();
      this.selectedProjectId = id;
      this.seeEvents = [];
      this.seeLastEventId = 0;
      this.selectedWorkerStream = '';
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      await this.loadProject(id);
      await this.loadSeeEvents(true);
      await this.loadDispatchSettings();
      this.humanTab = 'workspace';
      this.view = initialView === 'graph' ? 'graph' : 'human';
      this.activePage = 'task-detail';
      if (!fromRoute && location.hash !== `#/tasks/${id}`) location.hash = `/tasks/${id}`;
      this.$nextTick(() => {
        const clampedWidth = this.clampPanelWidth(this.sidePanelWidth);
        if (clampedWidth !== this.sidePanelWidth) {
          this.sidePanelWidth = clampedWidth;
          this.saveSidePanelWidth();
        }
        this.teardownAutoFit();
        if (this.cy) { this.cy.destroy(); this.cy = null; }
        if (this.view === 'graph') this.initGraph();
      });
    },

    backToList(fromRoute) {
      if (this.replay.active) {
        this.stopReplayTimer();
        this.replay.active = false;
        this.replay.playing = false;
        this.replay.frames = [];
        this.replay.visibleEvents = [];
        this.replay.frameIndex = -1;
        this.replay.sourceProject = null;
        this._timelineEventsCacheProject = null;
        this._timelineEventsCache = [];
        this.polling = true;
      }
      this.view = 'list';
      this.shouldRestoreProjectListScroll = true;
      this.project = null;
      this.selectedProjectId = '';
      this.seeEvents = [];
      this.seeLastEventId = 0;
      this.selectedWorkerStream = '';
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.teardownAutoFit();
      if (this.cy) { this.cy.destroy(); this.cy = null; }
      if (!fromRoute) location.hash = '/tasks';
      this.loadProjects().then(() => this.restoreProjectListScroll());
    },

    startPolling() {
      if (this.pollTimer) clearInterval(this.pollTimer);
      if (this.intentActivityPollTimer) clearInterval(this.intentActivityPollTimer);
      this.pollTimer = setInterval(async () => {
        if (!this.polling) return;
        if (this.selectedProjectId && ['graph', 'human'].includes(this.view) && this.activePage === 'task-detail') {
          await Promise.all([
            this.loadProject(this.selectedProjectId),
            this.loadSeeEvents(false),
            this.loadDispatchSettings(),
          ]);
          if (this.view === 'graph') this.updateGraph();
        } else {
          await this.loadProjects();
          if (['dashboard', 'projects', 'vulnerabilities'].includes(this.activePage)) {
            await this.loadAllProjectDetails();
          }
        }
      }, 5000);
      this.intentActivityPollTimer = setInterval(() => {
        this.refreshSelectedIntentActivity();
      }, 2000);
    },

    async refreshSelectedIntentActivity() {
      if (!this.polling || this.activePage !== 'task-detail' || this.view !== 'graph') return;
      const intentId = this.selectedIntentId();
      if (!this.selectedProjectId || !intentId) return;
      const current = this.selectedIntentRecord();
      if (!current || !['claimed', 'running', 'concluding'].includes(current.status)) return;
      try {
        const latest = await this.api('GET', `/projects/${this.selectedProjectId}/intents/${intentId}`);
        const index = (this.project?.intents || []).findIndex(item => item.id === intentId);
        if (index >= 0) this.project.intents.splice(index, 1, latest);
      } catch (error) {
        // Full-page polling remains the fallback. A transient activity refresh
        // must not distract the operator with repeated toast errors.
        console.debug('Intent activity refresh failed', error);
      }
    },

    // ── Asset Management ──
    async loadAssetFilters() {
      try {
        const [domains, projects, groups] = await Promise.all([
          this.api('GET', '/assets/domains'),
          this.api('GET', '/assets/projects'),
          this.api('GET', '/assets/groups'),
        ]);
        this.assetDomains = domains;
        this.assetProjects = projects;
        this.assetGroups = groups;
      } catch(e) { this.assetDomains = []; this.assetProjects = []; this.assetGroups = []; }
    },
    async loadAssets() {
      const params = new URLSearchParams({ limit: String(this.assetPageSize), offset: String(this.assetPage * this.assetPageSize) });
      if (this.assetFilterProject) params.set('project_id', this.assetFilterProject);
      if (this.assetFilterGroup) params.set('group_id', this.assetFilterGroup);
      if (this.assetFilterDomain) params.set('backend_domain', this.assetFilterDomain);
      if (this.assetFilterStatus) params.set('test_status', this.assetFilterStatus);
      if (this.assetFilterMethod) params.set('api_method', this.assetFilterMethod);
      if (this.assetSearch) params.set('search', this.assetSearch);
      try {
        this.assets = await this.api('GET', `/assets?${params}`);
        this.assetTotal = this.assets.length < this.assetPageSize ? this.assetPage * this.assetPageSize + this.assets.length : 999;
      } catch(e) { this.assets = []; }
      await this.loadAssetStats();
    },
    async loadAssetStats() {
      try {
        const p = new URLSearchParams();
        if (this.assetFilterProject) p.set('project_id', this.assetFilterProject);
        if (this.assetFilterGroup) p.set('group_id', this.assetFilterGroup);
        const qs = p.toString() ? `?${p}` : '';
        this.assetStats = await this.api('GET', `/assets/stats${qs}`);
      } catch(e) {}
    },
    openAssetEdit(asset) {
      this.editAssetId = asset.id;
      this.editAssetForm = { api_purpose: asset.api_purpose || '', test_status: asset.test_status, auth_required: asset.auth_required, auth_type: asset.auth_type || '' };
      this.showAssetEdit = true;
    },
    async saveAsset() {
      if (!this.editAssetId) return;
      try {
        await this.api('PUT', `/assets/${this.editAssetId}`, this.editAssetForm);
        this.loadAssets();
      } catch(e) { this.showToast(e.message, 'error'); }
    },
    async deleteAsset(id) {
      if (!confirm('确认删除此资产？')) return;
      try {
        await this.api('DELETE', `/assets/${id}`);
        this.loadAssets();
      } catch(e) { this.showToast(e.message, 'error'); }
    },
    openAssetAdd() {
      this.editAssetForm = { project_id: this.selectedProjectId || '', group_id: '', api_path: '', api_method: 'GET', backend_domain: '', api_purpose: '', auth_required: 'unknown', auth_type: '', test_status: 'untested', sample_body: '', content_type: '' };
      this.showAssetAdd = true;
    },
    async addAsset() {
      try {
        await this.api('POST', '/assets', this.editAssetForm);
        this.loadAssets();
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    // ── Credential Management ──
    async loadCredentials() {
      const params = new URLSearchParams({ limit: String(this.credPageSize), offset: String(this.credPage * this.credPageSize) });
      if (this.credentialFilterGroup) params.set('group_id', this.credentialFilterGroup);
      try {
        this.credentials = await this.api('GET', `/credentials?${params}`);
      } catch(e) { this.credentials = []; }
    },
    openCredAdd() {
      this.editCredForm = { group_id: '', label: '', auth_type: 'header', credentials_json: '', domains_text: '', source_task_id: '', expires_at: '', asset_id: '', active: true, captured_at: '' };
      this.showCredAdd = true;
    },
    async addCredential() {
      try {
        const body = { ...this.editCredForm, domains_json: JSON.stringify(this._parseDomainText(this.editCredForm.domains_text)) };
        delete body.domains_text;
        delete body.captured_at;
        body.active = body.active ? 1 : 0;
        if (body.asset_id !== '' && body.asset_id !== null && body.asset_id !== undefined) {
          body.asset_id = parseInt(body.asset_id) || null;
        } else {
          body.asset_id = null;
        }
        await this.api('POST', '/credentials', body);
        this.loadCredentials();
      } catch(e) { this.showToast(e.message, 'error'); }
    },
    openCredEdit(cred) {
      this.editCredId = cred.id;
      this.editCredForm = {
        label: cred.label,
        auth_type: cred.auth_type,
        credentials_json: this.credToEditText(cred.credentials_json),
        domains_text: this.parseDomains(cred.domains_json).join(', '),
        expires_at: cred.expires_at || '',
        asset_id: cred.asset_id || '',
        source_task_id: cred.source_task_id || '',
        active: cred.active === 1,
        captured_at: cred.captured_at || '',
      };
      this.showCredEdit = true;
    },
    async saveCredential() {
      if (!this.editCredId) return;
      try {
        const body = { ...this.editCredForm, domains_json: JSON.stringify(this._parseDomainText(this.editCredForm.domains_text)) };
        delete body.domains_text;
        delete body.captured_at;
        body.active = body.active ? 1 : 0;
        if (body.asset_id !== '' && body.asset_id !== null && body.asset_id !== undefined) {
          body.asset_id = parseInt(body.asset_id) || null;
        } else {
          body.asset_id = null;
        }
        await this.api('PUT', `/credentials/${this.editCredId}`, body);
        this.loadCredentials();
      } catch(e) { this.showToast(e.message, 'error'); }
    },
    async deleteCredential(id) {
      if (!confirm('确认删除此凭证？')) return;
      try {
        await this.api('DELETE', `/credentials/${id}`);
        this.loadCredentials();
      } catch(e) { this.showToast(e.message, 'error'); }
    },
    parseDomains(json) {
      try { return JSON.parse(json || '[]'); } catch(e) { return []; }
    },
    _parseDomainText(text) {
      if (!text || !text.trim()) return [];
      return text.split(',').map(s => s.trim()).filter(Boolean);
    },
    positionCredTooltip(event) {
      const td = event.currentTarget;
      const tooltip = document.getElementById('cred-tooltip-fixed');
      const content = document.getElementById('cred-tooltip-content');
      if (!tooltip || !content) return;
      // Find the credential data from the Alpine component
      const alpineData = Alpine.$data(td.closest('[x-data]') || document.body);
      const credJson = td.querySelector('span')?.textContent;
      // Get the full credentials_json from the row context
      const row = td.closest('tr');
      let jsonStr = '';
      if (alpineData && alpineData.credentials) {
        const idx = Array.from(row.parentNode.children).filter(el => el.tagName === 'TR' || el.tagName === 'TEMPLATE').indexOf(row);
        // Fallback: search by matching the summary text
        for (const c of alpineData.credentials) {
          if (this.credSummary(c.credentials_json) === credJson) { jsonStr = c.credentials_json; break; }
        }
      }
      if (!jsonStr) jsonStr = credJson;
      content.textContent = this.credKeyValueFormat(jsonStr);
      const rect = td.getBoundingClientRect();
      const vh = window.innerHeight;
      const spaceBelow = vh - rect.bottom;
      tooltip.classList.remove('hidden');
      if (spaceBelow < 150) {
        tooltip.style.top = 'auto';
        tooltip.style.bottom = (vh - rect.top + 4) + 'px';
      } else {
        tooltip.style.top = (rect.bottom + 4) + 'px';
        tooltip.style.bottom = 'auto';
      }
      tooltip.style.left = Math.min(rect.left, window.innerWidth - 330) + 'px';
    },
    hideCredTooltip(event) {
      const tooltip = document.getElementById('cred-tooltip-fixed');
      if (tooltip) tooltip.classList.add('hidden');
    },
    credSummary(json) {
      try {
        const obj = JSON.parse(json);
        const keys = Object.keys(obj);
        if (keys.length === 0) return '{}';
        return `{${keys.length} keys: ${keys.slice(0, 3).join(', ')}${keys.length > 3 ? '…' : ''}}`;
      } catch(e) { return json ? json.slice(0, 40) : '{}'; }
    },
    prettyJson(str) {
      try { return JSON.stringify(JSON.parse(str), null, 2); } catch(e) { return str; }
    },
    credKeyValueFormat(jsonStr) {
      try {
        const obj = JSON.parse(jsonStr);
        if (typeof obj !== 'object' || obj === null) return jsonStr;
        const lines = [];
        for (const [k, v] of Object.entries(obj)) {
          const val = (v !== null && typeof v === 'object') ? JSON.stringify(v) : String(v);
          lines.push(`${k}: ${val}`);
        }
        return lines.join('\n');
      } catch(e) { return jsonStr; }
    },
    // 凭证类型 → 中文显示名
    authTypeLabel(type) {
      const map = {
        header: '请求头', url: 'URL', body: '请求体',
        cookie: 'cookie', jwt: 'jwt', bearer: 'bearer',
        basic: 'basic', custom_header: 'custom', none: 'none',
      };
      return map[type] || (type || '—');
    },
    // 凭证类型 → 表格徽章配色
    authTypeBadgeClass(type) {
      const map = {
        header: 'bg-sky-50 text-sky-600',
        url: 'bg-teal-50 text-teal-600',
        body: 'bg-indigo-50 text-indigo-600',
        cookie: 'bg-amber-50 text-amber-600',
        jwt: 'bg-blue-50 text-blue-600',
        bearer: 'bg-purple-50 text-purple-600',
        basic: 'bg-slate-100 text-slate-500',
        custom_header: 'bg-slate-100 text-slate-500',
        none: 'bg-slate-100 text-slate-400',
      };
      return map[type] || 'bg-slate-100 text-slate-500';
    },
    // 存储的 credentials_json → 弹窗编辑用自由文本。
    // 扁平 dict（值均为标量）→ key: value 行；复杂/嵌套 JSON → 保留 pretty JSON 以便原样保存。
    credToEditText(jsonStr) {
      try {
        const obj = JSON.parse(jsonStr);
        if (typeof obj !== 'object' || obj === null || Array.isArray(obj)) return this.prettyJson(jsonStr);
        const allFlat = Object.values(obj).every(v => v === null || typeof v !== 'object');
        return allFlat ? this.credKeyValueFormat(jsonStr) : this.prettyJson(jsonStr);
      } catch(e) { return jsonStr || ''; }
    },

    openEditFact() {
      const fact = this.selectedFactRecord();
      if (!fact) return;
      this.editForm = { type: 'fact', id: fact.id, title: `编辑 Fact ${fact.id}`, content: fact.description };
      this.showEditContentModal = true;
      this.$nextTick(() => { if (this.$refs.editContentInput) this.$refs.editContentInput.focus(); });
    },
    openEditIntent() {
      const intent = this.selectedIntentRecord();
      if (!intent) return;
      this.editForm = { type: 'intent', id: intent.id, title: `编辑 Intent ${intent.id}`, content: intent.description };
      this.showEditContentModal = true;
      this.$nextTick(() => { if (this.$refs.editContentInput) this.$refs.editContentInput.focus(); });
    },
    openEditHint(h) {
      if (!h) return;
      this.editForm = { type: 'hint', id: h.id, title: `编辑 Hint ${h.id}`, content: h.content };
      this.showEditContentModal = true;
      this.$nextTick(() => { if (this.$refs.editContentInput) this.$refs.editContentInput.focus(); });
    },
    async saveEditContent() {
      if (!this.editForm.content) return;
      try {
        let path, body;
        if (this.editForm.type === 'fact') {
          path = `/projects/${this.selectedProjectId}/facts/${this.editForm.id}`;
          body = { description: this.editForm.content };
        } else if (this.editForm.type === 'intent') {
          path = `/projects/${this.selectedProjectId}/intents/${this.editForm.id}`;
          body = { description: this.editForm.content };
        } else if (this.editForm.type === 'hint') {
          path = `/projects/${this.selectedProjectId}/hints/${this.editForm.id}`;
          body = { content: this.editForm.content };
        }
        await this.api('PUT', path, body);
        this.showEditContentModal = false;
        this.editForm = { type: '', id: '', title: '', content: '' };
        this.showToast('已更新', 'success');
        // Refresh project data to reflect changes
        await this.loadProjectDetails(this.selectedProjectId);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async deleteFact(factId) {
      if (!factId || !confirm(`确定删除 Fact ${factId}？这将同时清理相关的 intent 引用。`)) return;
      try {
        await this.api('DELETE', `/projects/${this.selectedProjectId}/facts/${factId}`);
        this.showToast('已删除', 'success');
        this.clearGraphSelection();
        await this.loadProjectDetails(this.selectedProjectId);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async deleteIntent(intentId) {
      if (!intentId || !confirm(`确定删除 Intent ${intentId}？`)) return;
      try {
        await this.api('DELETE', `/projects/${this.selectedProjectId}/intents/${intentId}`);
        this.showToast('已删除', 'success');
        this.clearGraphSelection();
        await this.loadProjectDetails(this.selectedProjectId);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async toggleArchiveFact(factId) {
      if (!factId) return;
      try {
        await this.api('PUT', `/projects/${this.selectedProjectId}/facts/${factId}/archive`);
        this.showToast('已切换归档状态', 'success');
        await this.loadProjectDetails(this.selectedProjectId);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async toggleArchiveIntent(intentId) {
      if (!intentId) return;
      try {
        await this.api('PUT', `/projects/${this.selectedProjectId}/intents/${intentId}/archive`);
        this.showToast('已切换归档状态', 'success');
        await this.loadProjectDetails(this.selectedProjectId);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async loadSettings() {
      try {
        const s = await this.api('GET', '/settings');
        this.settingsForm.intent_timeout = s.intent_timeout;
        this.settingsForm.reason_timeout = s.reason_timeout;
        this.settingsForm.bootstrap_worker_count = s.bootstrap_worker_count || 1;
        this.settingsForm.explore_worker_count = s.explore_worker_count || 2;
        this.settingsForm.reason_worker_count = s.reason_worker_count || 1;
        this.settingsForm.plan_timeout = s.plan_timeout || 600;
        this.settingsForm.plan_worker_count = s.plan_worker_count || 1;
        this.settingsForm.crawler_active_window_seconds = s.crawler_active_window_seconds || 180;
        this.settingsForm.crawler_flush_window_seconds = s.crawler_flush_window_seconds || 45;
        this.settingsForm.crawler_total_timeout_seconds = s.crawler_total_timeout_seconds || 1800;
        this.settingsForm.proxy_url = s.proxy_url || '';
        this.settingsForm.proxy_enabled = !!s.proxy_enabled;
        await this.loadDispatchSettings();
        await this.loadJudgeSettings();
      } catch(e) { console.error(e); }
    },
  }
};
