window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.dispatch = {
  methods: {
    workerStreams() {
      const groups = new Map();
      for (const event of this.seeEvents || []) {
        const worker = event.worker || 'unknown';
        if (!groups.has(worker)) groups.set(worker, []);
        groups.get(worker).push(event);
      }
      return Array.from(groups.entries()).map(([worker, events]) => {
        const latest = events[events.length - 1];
        const warnCount = events.filter(event => event.level === 'warn').length;
        return { worker, events, latest, warnCount };
      });
    },

    selectedWorkerEvents() {
      if (!this.selectedWorkerStream) return [];
      return (this.seeEvents || []).filter(event => (event.worker || 'unknown') === this.selectedWorkerStream);
    },

    async loadDispatchSettings() {
      const data = await this.api('GET', '/settings/dispatch');
      this.applyDispatchSettings(data);
    },

    applyDispatchSettings(data) {
      this.dispatchSettings = {
        runtime_interval: data.runtime_interval || 3,
        runtime_max_workers: data.runtime_max_workers || 8,
        runtime_max_running_projects: data.runtime_max_running_projects || 3,
        runtime_max_project_workers: data.runtime_max_project_workers || 4,
        runtime_healthcheck_timeout: data.runtime_healthcheck_timeout || 20,
        runtime_worker_healthcheck: data.runtime_worker_healthcheck || 'startup_only',
        runtime_prompt_group: data.runtime_prompt_group || 'default',
        bootstrap_timeout: data.bootstrap_timeout || 300,
        bootstrap_conclude_timeout: data.bootstrap_conclude_timeout || 90,
        explore_timeout: data.explore_timeout || 300,
        explore_conclude_timeout: data.explore_conclude_timeout || 90,
        reason_timeout: data.reason_timeout || 300,
        reason_max_intents: data.reason_max_intents || 2,
        depth_reason_max_intents: data.depth_reason_max_intents || 5,
        workers: (data.workers || []).map(worker => this.decorateWorkerDraft(worker)),
        config_version: data.config_version || 1,
        config_updated_at: data.config_updated_at || '',
        dispatcher_applied_version: data.dispatcher_applied_version || 0,
        dispatcher_applied_at: data.dispatcher_applied_at || '',
        effective_workers: data.effective_workers || [],
        bootstrap_worker_count: data.bootstrap_worker_count || 1,
        explore_worker_count: data.explore_worker_count || 2,
        reason_worker_count: data.reason_worker_count || 1,
      };
      this.settingsForm.proxy_url = data.proxy_url || this.settingsForm.proxy_url || '';
      this.settingsForm.proxy_username = data.proxy_username || '';
      this.settingsForm.proxy_password = data.proxy_password || '';
      this.settingsForm.proxy_enabled = !!data.proxy_enabled;
    },

    nextWorkerDraftId() {
      this.workerDraftCounter = Number(this.workerDraftCounter || 0) + 1;
      return `worker-draft-${this.workerDraftCounter}`;
    },

    decorateWorkerDraft(worker) {
      return {
        ...worker,
        _draftId: worker?._draftId || this.nextWorkerDraftId(),
        phase_bootstrap: Array.isArray(worker.task_types) && worker.task_types.includes('bootstrap'),
        phase_reason: Array.isArray(worker.task_types) && worker.task_types.includes('reason'),
        phase_explore: Array.isArray(worker.task_types) && worker.task_types.includes('explore'),
        phase_plan: Array.isArray(worker.task_types) && worker.task_types.includes('plan'),
        phase_maintainer: Array.isArray(worker.task_types) && worker.task_types.includes('maintainer'),
        pi_api_key: '',
        testing: false,
        test_result: null,
        models_loading: false,
        models_error: '',
        available_models: Array.isArray(worker.available_models) ? worker.available_models : [],
        show_model_dropdown: false,
      };
    },

    phaseToggleClass(enabled) {
      return enabled
        ? 'border shadow-sm'
        : 'border';
    },

    phaseToggleStyle(enabled) {
      if (enabled) {
        return 'background:#0f172a;color:#ffffff;border-color:#0f172a;';
      }
      return 'background:#f1f5f9;color:#94a3b8;border-color:#e2e8f0;';
    },

    toggleWorkerPhase(worker, phaseKey) {
      worker[phaseKey] = !worker[phaseKey];
    },

    parseContextWindowInput(rawValue) {
      const value = String(rawValue ?? '').trim().toLowerCase();
      if (!value) return '';
      const match = value.match(/^(\d+)\s*([km])?$/i);
      if (!match) return null;
      const base = Number(match[1]);
      if (!Number.isFinite(base) || base <= 0) return null;
      const unit = (match[2] || '').toLowerCase();
      if (unit === 'k') return String(base * 1000);
      if (unit === 'm') return String(base * 1000000);
      return String(base);
    },

    normalizeWorkerContextWindow(worker) {
      const normalized = this.parseContextWindowInput(worker.pi_model_context_window);
      if (normalized === null) {
        throw new Error(`Worker ${worker.name || '未命名'} 的上下文窗口格式无效，支持如 200000 / 500k / 1m`);
      }
      worker.pi_model_context_window = normalized;
      return normalized;
    },

    async fetchPiModels(worker) {
      try {
        worker.models_loading = true;
        worker.models_error = '';
        worker.show_model_dropdown = false;
        const params = new URLSearchParams();
        params.set('base_url', (worker.pi_base_url || '').trim());
        if ((worker.pi_api_key || '').trim()) params.set('api_key', worker.pi_api_key.trim());
        if ((worker.name || '').trim()) params.set('worker_name', worker.name.trim());
        const data = await this.api('GET', `/settings/dispatch/models?${params.toString()}`);
        worker.available_models = Array.isArray(data.models) ? data.models : [];
        worker.models_error = data.error || '';
        worker.show_model_dropdown = worker.available_models.length > 0;
      } catch (e) {
        worker.available_models = [];
        worker.models_error = e.message || '获取模型失败';
        worker.show_model_dropdown = false;
      } finally {
        worker.models_loading = false;
      }
    },

    chooseWorkerModel(worker, modelId) {
      if (!modelId) return;
      worker.pi_model = modelId;
      const model = (worker.available_models || []).find(item => item.id === modelId);
      if (!model || !model.context_window) return;
      worker.pi_model_context_window = String(model.context_window);
      worker.show_model_dropdown = false;
    },

    toggleWorkerModelDropdown(worker) {
      if (!Array.isArray(worker.available_models) || worker.available_models.length === 0) {
        this.showToast('请先获取模型列表', 'error');
        return;
      }
      worker.show_model_dropdown = !worker.show_model_dropdown;
    },

    async openActionReplay(projectId, title = '', fromRoute = false) {
      this.actionReplayProjectId = projectId || '';
      this.selectedTaskManagementId = projectId || this.selectedTaskManagementId;
      if (!this.tools || this.tools.length === 0) {
        await this.loadTools();
      }
      if (!title && (!this.projects || this.projects.length === 0)) {
        await this.loadProjects();
      }
      const matchedTask = (this.projects || []).find(task => task.id === projectId);
      this.actionReplayProjectTitle = title || matchedTask?.title || projectId || '';
      this.actionReplayEvents = [];
      this.actionReplayLastEventId = 0;
      this.selectedActionReplayWorker = '';
      this.toolExecutions = [];
      this.replayViewMode = 'tools';
      this.activePage = 'action-replay';
      if (!fromRoute && location.hash !== `#/tasks/${projectId}/replay`) location.hash = `/tasks/${projectId}/replay`;
      await this.loadDispatchSettings();
      await this.loadActionReplayEvents(true);
      await this.loadToolExecutions();
      // Auto-poll tool executions every 2s while viewing replay
      if (this.toolPollTimer) clearInterval(this.toolPollTimer);
      this.toolPollTimer = setInterval(() => this.loadToolExecutions(), 2000);
    },

    closeActionReplay() {
      if (this.toolPollTimer) { clearInterval(this.toolPollTimer); this.toolPollTimer = null; }
      this.actionReplayProjectId = '';
      this.actionReplayProjectTitle = '';
      this.actionReplayEvents = [];
      this.actionReplayLastEventId = 0;
      this.selectedActionReplayWorker = '';
      this.toolExecutions = [];
      this.toolFilterType = '';
      this.toolFilterSearch = '';
    },

    async loadToolExecutions() {
      if (!this.actionReplayProjectId) return;
      try {
        // Incremental: only fetch new records since the last known ID.
        const lastId = this.toolExecutions.length > 0
          ? this.toolExecutions[this.toolExecutions.length - 1].id
          : '';
        const since = lastId ? `&since=${encodeURIComponent(lastId)}` : '';
        const resp = await fetch(`/projects/${this.actionReplayProjectId}/tool-executions?limit=200${since}`);
        if (resp.ok) {
          const data = await resp.json();
          if (lastId) {
            // Append new records to existing list.
            for (const item of (data.items || [])) {
              this.toolExecutions.push(item);
            }
          } else {
            this.toolExecutions = data.items || [];
          }
        }
      } catch (e) { /* ignore */ }
      await this.loadSkillUsage();
    },

    async loadSkillUsage() {
      if (!this.actionReplayProjectId) return;
      try {
        const resp = await fetch(`/projects/${this.actionReplayProjectId}/skill-usage`);
        if (resp.ok) {
          const data = await resp.json();
          this.skillUsage = data.items || [];
        }
      } catch (e) { /* ignore */ }
    },

    async loadActionReplayEvents(reset = false) {
      if (!this.actionReplayProjectId) {
        this.actionReplayEvents = [];
        this.actionReplayLastEventId = 0;
        return;
      }
      if (reset) {
        this.actionReplayLastEventId = 0;
        this.actionReplayEvents = [];
      }
      try {
        const events = await this.api(
          'GET',
          `/projects/${this.actionReplayProjectId}/events?after_id=${this.actionReplayLastEventId}&limit=200`
        );
        if (Array.isArray(events) && events.length > 0) {
          this.actionReplayEvents.push(...events);
          this.actionReplayLastEventId = events[events.length - 1].id;
          const workers = this.actionReplayWorkers().map(stream => stream.worker);
          if (!this.selectedActionReplayWorker || !workers.includes(this.selectedActionReplayWorker)) {
            this.selectedActionReplayWorker = workers[0] || '';
          }
          this.$nextTick(() => {
            const el = this.$refs.actionReplayScroll;
            if (el) el.scrollTop = el.scrollHeight;
          });
        }
      } catch (e) {
        console.error(e);
      }
    },

    async loadSeeEvents(reset = false) {
      if (!this.selectedProjectId) {
        this.seeEvents = [];
        this.seeLastEventId = 0;
        return;
      }
      if (reset) {
        this.seeLastEventId = 0;
        this.seeEvents = [];
      }
      try {
        const events = await this.api(
          'GET',
          `/projects/${this.selectedProjectId}/events?after_id=${this.seeLastEventId}&limit=200`
        );
        if (Array.isArray(events) && events.length > 0) {
          this.seeEvents.push(...events);
          this.seeLastEventId = events[events.length - 1].id;
          const streamWorkers = this.workerStreams().map(stream => stream.worker);
          if (!this.selectedWorkerStream || !streamWorkers.includes(this.selectedWorkerStream)) {
            this.selectedWorkerStream = streamWorkers[0] || '';
          }
          this.$nextTick(() => {
            const el = this.$refs.seeScroll;
            if (el) el.scrollTop = el.scrollHeight;
          });
        }
      } catch (e) {
        console.error(e);
      }
    },

    workerPayload(worker) {
      const taskTypes = [];
      if (worker.phase_bootstrap) taskTypes.push('bootstrap');
      if (worker.phase_reason) taskTypes.push('reason');
      if (worker.phase_explore) taskTypes.push('explore');
      if (worker.phase_plan) taskTypes.push('plan');
      if (worker.phase_maintainer) taskTypes.push('maintainer');
      if (taskTypes.length === 0) {
        throw new Error(`Worker ${worker.name || '未命名'} 至少要勾选一个参与阶段`);
      }
      const normalizedContextWindow = this.normalizeWorkerContextWindow(worker);
      return {
        name: worker.name,
        task_types: taskTypes,
        max_running: Number(worker.max_running || 1),
        priority: Number(worker.priority || 0),
        pi_model: worker.pi_model || '',
        pi_base_url: worker.pi_base_url || '',
        pi_provider_api: 'openai-completions',
        pi_model_context_window: normalizedContextWindow,
        pi_api_key: worker.pi_api_key || '',
        pi_api_key_masked: worker.pi_api_key_masked || '',
        disabled: !!worker.disabled,
      };
    },

    dispatchPayload() {
      return {
        runtime_interval: Number(this.dispatchSettings.runtime_interval || 3),
        runtime_max_workers: Number(this.dispatchSettings.runtime_max_workers || 1),
        runtime_max_running_projects: Number(this.dispatchSettings.runtime_max_running_projects || 1),
        runtime_max_project_workers: Number(this.dispatchSettings.runtime_max_project_workers || 1),
        runtime_healthcheck_timeout: Number(this.dispatchSettings.runtime_healthcheck_timeout || 20),
        runtime_worker_healthcheck: this.dispatchSettings.runtime_worker_healthcheck || 'startup_only',
        runtime_prompt_group: this.dispatchSettings.runtime_prompt_group || 'default',
        bootstrap_timeout: Number(this.dispatchSettings.bootstrap_timeout || 300),
        bootstrap_conclude_timeout: Number(this.dispatchSettings.bootstrap_conclude_timeout || 90),
        explore_timeout: Number(this.dispatchSettings.explore_timeout || 300),
        explore_conclude_timeout: Number(this.dispatchSettings.explore_conclude_timeout || 90),
        reason_timeout: Number(this.dispatchSettings.reason_timeout || 300),
        reason_max_intents: Number(this.dispatchSettings.reason_max_intents || 2),
        depth_reason_max_intents: Number(this.dispatchSettings.depth_reason_max_intents || 5),
        bootstrap_worker_count: Number(this.settingsForm.bootstrap_worker_count || 1),
        explore_worker_count: Number(this.settingsForm.explore_worker_count || 2),
        reason_worker_count: Number(this.settingsForm.reason_worker_count || 1),
        plan_timeout: Number(this.settingsForm.plan_timeout || 600),
        plan_worker_count: Number(this.settingsForm.plan_worker_count || 1),
        workers: (this.dispatchSettings.workers || []).map(worker => this.workerPayload(worker)),
        proxy_url: this.settingsForm.proxy_url || '',
        proxy_username: this.settingsForm.proxy_username || '',
        proxy_password: this.settingsForm.proxy_password || '',
        proxy_enabled: !!this.settingsForm.proxy_enabled,
      };
    },

    addPiWorker() {
      const index = (this.dispatchSettings.workers || []).length + 1;
      this.dispatchSettings.workers.push({
        _draftId: this.nextWorkerDraftId(),
        name: `pi-worker-${index}`,
        task_types: ['bootstrap', 'reason', 'explore'],
        phase_bootstrap: true,
        phase_reason: true,
        phase_explore: true,
        phase_plan: false,
        max_running: 1,
        priority: 1,
        pi_model: '',
        pi_base_url: '',
        pi_provider_api: 'openai-completions',
        pi_model_context_window: '200000',
        pi_api_key: '',
        pi_api_key_masked: '',
        disabled: false,
        testing: false,
        test_result: null,
        models_loading: false,
        models_error: '',
        available_models: [],
        show_model_dropdown: false,
      });
    },

    removePiWorker(index) {
      this.dispatchSettings.workers.splice(index, 1);
    },

    async toggleWorkerDisabled(worker) {
      try {
        const updated = await this.api('PUT', `/settings/dispatch/workers/${encodeURIComponent(worker.name)}/toggle`);
        this.applyDispatchSettings(updated);
        this.showToast(`Worker ${worker.name} ${worker.disabled ? '已启用' : '已禁用'}`);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async savePiWorker(worker) {
      try {
        const updated = await this.api('PUT', '/settings/dispatch', this.dispatchPayload());
        this.applyDispatchSettings(updated);
        this.showToast(`Worker ${worker.name} 已保存`);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async testPiWorkerConnection(worker) {
      try {
        worker.testing = true;
        worker.test_result = null;
        const result = await this.api('POST', '/settings/test-pi', {
          worker_name: worker.name,
          pi_base_url: worker.pi_base_url,
          pi_model: worker.pi_model,
          pi_provider_api: 'openai-completions',
          pi_api_key: worker.pi_api_key || '',
        });
        worker.test_result = result;
        this.showToast(result.message, result.ok ? 'info' : 'error');
      } catch (e) {
        worker.test_result = { ok: false, message: e.message };
        this.showToast(e.message, 'error');
      } finally {
        worker.testing = false;
      }
    },

    async saveServerSettings() {
      try {
        const updated = await this.api('PUT', '/settings/dispatch', this.dispatchPayload());
        this.applyDispatchSettings(updated);
        this.showToast('系统配置已保存');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    effectiveRuntimeWorkerNames() {
      return (this.dispatchSettings.effective_workers || []).map(worker => worker.name).filter(Boolean);
    },

    dispatchVersionLabel() {
      const version = this.dispatchSettings.config_version || 0;
      const updatedAt = this.dispatchSettings.config_updated_at;
      if (!version) return '未加载';
      return updatedAt ? `v${version} · ${this.formatTime(updatedAt)}` : `v${version}`;
    },

    dispatcherAppliedLabel() {
      const version = this.dispatchSettings.dispatcher_applied_version || 0;
      const appliedAt = this.dispatchSettings.dispatcher_applied_at;
      if (!version) return '等待 dispatcher 上报';
      return appliedAt ? `v${version} · ${this.formatTime(appliedAt)}` : `v${version}`;
    },

    bootstrapOutcomeLabel(events) {
      const bootstrapEvents = (events || [])
        .filter(event => event?.task_type === 'bootstrap')
        .filter(event => event?.payload && typeof event.payload === 'object' && event.payload.conclusion);
      if (bootstrapEvents.length === 0) return '暂无 bootstrap 结论';
      const latest = bootstrapEvents[bootstrapEvents.length - 1];
      const mapping = {
        fact_written: '已写入初始事实',
        complete: '已直接完成目标',
        rejected: 'bootstrap 已拒绝',
      };
      const label = mapping[latest.payload.conclusion] || latest.payload.conclusion;
      return latest.worker ? `${label} · ${latest.worker}` : label;
    },

    selectedProjectBootstrapOutcome() {
      return this.bootstrapOutcomeLabel(this.seeEvents || []);
    },

    actionReplayBootstrapOutcome() {
      return this.bootstrapOutcomeLabel(this.actionReplayEvents || []);
    },
  }
};
