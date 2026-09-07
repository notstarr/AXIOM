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

    normalizeJudgeList(value) {
      if (Array.isArray(value)) return value.map(v => String(v || '').trim()).filter(Boolean);
      return String(value || '').split(/[\n,]+/).map(v => v.trim()).filter(Boolean);
    },


    async loadJudgeSettings() {
      try {
        const data = await this.api('GET', '/settings/judge');
        this.judgeSettings = {
          enabled_default: !!data.enabled_default,
          mode: data.mode || 'enforce',
          base_url: data.base_url || 'https://api.siliconflow.cn/v1',
          model: data.model || 'Qwen/Qwen3.5-9B',
          timeout_seconds: data.timeout_seconds ?? 3,
          whitelist_tools: Array.isArray(data.whitelist_tools) ? data.whitelist_tools : ['read', 'ls', 'grep', 'find'],
          whitelist_bash_verbs: Array.isArray(data.whitelist_bash_verbs) ? data.whitelist_bash_verbs : ['pwd', 'whoami', 'id', 'uname', 'ls', 'cat', 'head', 'tail', 'grep', 'find', 'ifconfig', 'ip', 'env', 'printenv'],
          system_prompt: data.system_prompt || '',
          default_system_prompt: data.default_system_prompt || data.system_prompt || '',
          hard_block_rules: Array.isArray(data.hard_block_rules) ? data.hard_block_rules : [],
          default_hard_block_rules: Array.isArray(data.default_hard_block_rules) ? data.default_hard_block_rules : [],
          has_key: !!data.has_key,
          masked_key: data.masked_key || '',
          updated_at: data.updated_at || '',
        };
        if (this.newProject && !this.newProject._guardTouched) {
          this.newProject.guard_enabled = !!this.judgeSettings.enabled_default;
        }
      } catch (e) {
        this.showToast('加载裁判配置失败: ' + e.message, 'error');
      }
    },

    judgeSettingsPayload() {
      return {
        enabled_default: !!this.judgeSettings.enabled_default,
        mode: this.judgeSettings.mode ?? 'enforce',
        base_url: this.judgeSettings.base_url ?? 'https://api.siliconflow.cn/v1',
        model: this.judgeSettings.model ?? 'Qwen/Qwen3.5-9B',
        timeout_seconds: Number(this.judgeSettings.timeout_seconds ?? 3),
        whitelist_tools: this.normalizeJudgeList(this.judgeSettings.whitelist_tools),
        whitelist_bash_verbs: this.normalizeJudgeList(this.judgeSettings.whitelist_bash_verbs),
        system_prompt: this.judgeSettings.system_prompt ?? this.judgeSettings.default_system_prompt ?? '',
        hard_block_rules: this.judgeSettings.hard_block_rules || [],
      };
    },

    hardRulesText() {
      return (this.judgeSettings.hard_block_rules || [])
        .map(rule => `${rule.enabled === false ? '# ' : ''}${rule.reason || ''}\t${rule.pattern || ''}`)
        .join('\n');
    },

    parseHardRulesText(value) {
      const rules = [];
      for (const rawLine of String(value || '').split(/\n+/)) {
        let line = rawLine.trim();
        if (!line) continue;
        let enabled = true;
        if (line.startsWith('#')) {
          enabled = false;
          line = line.replace(/^#+\s*/, '').trim();
        }
        const tabIndex = line.indexOf('\t');
        const sepIndex = tabIndex >= 0 ? tabIndex : line.indexOf('  ');
        if (sepIndex < 0) continue;
        const reason = line.slice(0, sepIndex).trim();
        const pattern = line.slice(sepIndex).trim();
        if (!reason || !pattern) continue;
        rules.push({ reason, pattern, enabled });
      }
      this.judgeSettings.hard_block_rules = rules;
    },

    resetJudgeHardRules() {
      this.judgeSettings.hard_block_rules = (this.judgeSettings.default_hard_block_rules || []).map(rule => ({ ...rule }));
      this.showToast('已恢复默认硬规则，请保存后生效');
    },

    resetJudgePrompt() {
      this.judgeSettings.system_prompt = this.judgeSettings.default_system_prompt || '';
      this.showToast('已恢复默认裁判提示词，请保存后生效');
    },

    async saveJudgeSettings() {
      try {
        const data = await this.api('PUT', '/settings/judge', this.judgeSettingsPayload());
        this.judgeSettings = { ...this.judgeSettings, ...data };
        this.showToast('裁判配置已保存');
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    async saveJudgeKey() {
      const key = (this.judgeKeyForm.api_key || '').trim();
      if (!key) { this.showToast('请填写裁判 API Key', 'error'); return; }
      try {
        const data = await this.api('PUT', '/settings/keys/judge', { api_key: key, updated_by: this.actorName ? this.actorName() : 'human' });
        this.judgeSettings.has_key = !!data.has_key;
        this.judgeSettings.masked_key = data.masked_key || '';
        this.judgeKeyForm.api_key = '';
        this.showToast('裁判 API Key 已保存');
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    async testJudgeSettings() {
      try {
        this.judgeTesting = true;
        this.judgeTestResult = await this.api('POST', '/settings/judge/test', {});
        this.showToast(this.judgeTestResult.ok ? '裁判连接成功' : '裁判连接失败', this.judgeTestResult.ok ? 'success' : 'error');
      } catch (e) {
        this.judgeTestResult = { ok: false, message: e.message };
        this.showToast(e.message, 'error');
      } finally {
        this.judgeTesting = false;
      }
    },

    async testDingTalkSettings() {
      try {
        this.dingtalkTesting = true;
        const res = await this.api('POST', '/integrations/dingtalk/test', {
          webhook_url: this.dispatchSettings.dingtalk_webhook_url || '',
          secret: this.dispatchSettings.dingtalk_secret || '',
        });
        this.dingtalkTestResult = { ok: true, message: res.message || '测试通知已发送' };
        this.showToast('钉钉测试通知发送成功', 'success');
      } catch (e) {
        this.dingtalkTestResult = { ok: false, message: e.message };
        this.showToast(e.message, 'error');
      } finally {
        this.dingtalkTesting = false;
      }
    },

    async loadGuardAuditEvents() {
      try {
        this.guardAudit.loading = true;
        const params = new URLSearchParams();
        params.set('limit', String(this.guardAudit.limit || 100));
        params.set('decision', this.guardAudit.decision || 'all');
        if (this.guardAudit.project_id) params.set('project_id', this.guardAudit.project_id);
        this.guardAudit.events = await this.api('GET', '/tool-hooks/events?' + params.toString());
      } catch (e) {
        this.showToast('加载守门审计日志失败: ' + e.message, 'error');
      } finally {
        this.guardAudit.loading = false;
      }
    },

    parseGuardAuditJson(value) {
      if (!value) return {};
      if (typeof value === 'object') return value;
      try { return JSON.parse(value); } catch (e) { return { value: String(value) }; }
    },

    guardAuditProjectLabel(projectId) {
      const project = (this.projects || []).find(p => p.id === projectId);
      return project ? `${project.title || project.id} · ${project.id}` : (projectId || '未关联任务');
    },

    guardAuditStatusLabel(event) {
      return event && event.decision === 'BLOCK' ? '拦截' : '放行';
    },

    guardAuditCommand(event) {
      const normalized = this.parseGuardAuditJson(event?.normalized_json);
      const input = this.parseGuardAuditJson(event?.input_json);
      const command = normalized.command || normalized.path || input.command || input.cmd || input.path || input.file_path || input.filePath || JSON.stringify(input, null, 2);
      return String(command || '').slice(0, 12000);
    },

    guardAuditCommandPreview(event) {
      const command = this.guardAuditCommand(event).replace(/\s+/g, ' ').trim();
      if (!command) return '无命令内容';
      return command.length > 180 ? command.slice(0, 180) + '…' : command;
    },

    guardAuditReason(event) {
      const reason = String(event?.reason || '').trim();
      if (reason) return reason;
      if (event?.decision_source === 'whitelist') return '白名单直放';
      if (event?.decision_source === 'model') return '模型判定 ALLOW';
      if (event?.decision_source === 'fail_open') return '模型异常 fail-open';
      if (event?.decision_source === 'disabled') return '监管关闭';
      return '无原因';
    },

    guardAuditDetail(event) {
      return JSON.stringify({
        normalized: this.parseGuardAuditJson(event?.normalized_json),
        input: this.parseGuardAuditJson(event?.input_json),
        raw_model_output: event?.raw_model_output || '',
      }, null, 2);
    },

    async loadDispatchSettings() {
      try {
        const data = await this.api('GET', '/settings/dispatch');
        this.applyDispatchSettings(data);
        if (typeof this.syncDispatchPresetName === 'function') this.syncDispatchPresetName();
      } catch (e) {
        this.showToast('加载 Dispatch 配置失败: ' + e.message, 'error');
      }
    },

    applyDispatchSettings(data) {
      this.dispatchSettings = {
        runner_interval_seconds: data.runner_interval_seconds ?? 3,
        global_max_active_runs: data.global_max_active_runs ?? 10,
        global_max_active_tasks: data.global_max_active_tasks ?? 3,
        per_task_max_active_runs: data.per_task_max_active_runs ?? 4,
        per_task_explore_concurrency: data.per_task_explore_concurrency ?? 4,
        per_task_review_concurrency: data.per_task_review_concurrency ?? 4,
        heartbeat_interval_seconds: data.heartbeat_interval_seconds ?? 10,
        lease_timeout_seconds: data.lease_timeout_seconds ?? 60,
        execution_retry_limit: data.execution_retry_limit ?? 6,
        infrastructure_retry_limit: data.infrastructure_retry_limit ?? 3,
        retry_base_seconds: data.retry_base_seconds ?? 15,
        retry_max_seconds: data.retry_max_seconds ?? 300,
        worker_failure_cooldown_seconds: data.worker_failure_cooldown_seconds ?? 30,
        bootstrap_execute_seconds: data.bootstrap_execute_seconds ?? 300,
        bootstrap_conclude_seconds: data.bootstrap_conclude_seconds ?? 120,
        reason_execute_seconds: data.reason_execute_seconds ?? 300,
        reason_conclude_seconds: data.reason_conclude_seconds ?? 120,
        explore_execute_default_seconds: data.explore_execute_default_seconds ?? 900,
        explore_conclude_default_seconds: data.explore_conclude_default_seconds ?? 300,
        explore_no_progress_seconds: data.explore_no_progress_seconds ?? 300,
        plan_execute_seconds: data.plan_execute_seconds ?? 600,
        plan_conclude_seconds: data.plan_conclude_seconds ?? 300,
        review_execute_seconds: data.review_execute_seconds ?? 300,
        review_conclude_seconds: data.review_conclude_seconds ?? 120,
        reason_batch_size: data.reason_batch_size ?? 2,
        reason_low_watermark: data.reason_low_watermark ?? 1,
        reason_max_wait_seconds: data.reason_max_wait_seconds ?? 180,
        reason_min_interval_seconds: data.reason_min_interval_seconds ?? 60,
        reason_queue_limit: data.reason_queue_limit ?? 3,
        plan_queue_limit: data.plan_queue_limit ?? 3,
        plan_max_consecutive_runs: data.plan_max_consecutive_runs ?? 3,
        sandbox_backend: data.sandbox_backend || 'local',
        sandbox_required: !!data.sandbox_required,
        sandbox_local_fallback: data.sandbox_local_fallback !== false,
        sandbox_template: data.sandbox_template || 'axiom-worker',
        sandbox_runtime_revision: data.sandbox_runtime_revision || 'legacy-local',
        sandbox_healthcheck_timeout: data.sandbox_healthcheck_timeout ?? 10,
        browser_gateway_enabled: data.browser_gateway_enabled !== false,
        browser_gateway_bind: data.browser_gateway_bind || '127.0.0.1',
        browser_gateway_base_url: data.browser_gateway_base_url || '',
        browser_file_bridge_limit: data.browser_file_bridge_limit ?? 2097152,
        explore_execute_min_seconds: data.explore_execute_min_seconds ?? 60,
        explore_execute_max_seconds: data.explore_execute_max_seconds ?? 3600,
        explore_conclude_min_seconds: data.explore_conclude_min_seconds ?? 180,
        explore_conclude_max_seconds: data.explore_conclude_max_seconds ?? 600,
        bootstrap_concurrency: 1,
        reason_concurrency: 1,
        workers: (data.workers || []).map(worker => this.decorateWorkerDraft(worker)),
        config_version: data.config_version ?? 1,
        config_updated_at: data.config_updated_at || '',
        dispatcher_applied_version: data.dispatcher_applied_version ?? 0,
        dispatcher_applied_at: data.dispatcher_applied_at || '',
        effective_workers: data.effective_workers || [],
        network_proxy_mode: data.network_proxy_mode || 'off',
        network_proxy_url: data.network_proxy_url || 'http://127.0.0.1:7890',
        dingtalk_enabled: !!data.dingtalk_enabled,
        dingtalk_webhook_url: data.dingtalk_webhook_url || '',
        dingtalk_secret: data.dingtalk_secret || '',
        dingtalk_app_key: data.dingtalk_app_key || '',
        dingtalk_app_secret: data.dingtalk_app_secret || '',
      };
      this.settingsForm.proxy_url = data.proxy_url ?? this.settingsForm.proxy_url ?? '';
      this.settingsForm.proxy_username = data.proxy_username || '';
      this.settingsForm.proxy_password = data.proxy_password || '';
      this.settingsForm.proxy_enabled = !!data.proxy_enabled;
    },

    nextWorkerDraftId() {
      this.workerDraftCounter = Number(this.workerDraftCounter || 0) + 1;
      return `worker-draft-${this.workerDraftCounter}`;
    },

    decorateWorkerDraft(worker) {
      const taskTypes = Array.isArray(worker.task_types) ? worker.task_types : [];
      const legacyThinking = String(worker.pi_thinking || 'off').toLowerCase();
      const thinkingLevels = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
      const configuredThinkingLevel = String(worker.pi_thinking_level || '').toLowerCase();
      return {
        ...worker,
        task_types: taskTypes,
        persisted_name: worker.persisted_name || worker.name || '',
        _draftId: worker?._draftId || this.nextWorkerDraftId(),
        phase_bootstrap: taskTypes.includes('bootstrap'),
        phase_reason: taskTypes.includes('reason'),
        phase_explore: taskTypes.includes('explore'),
        phase_plan: taskTypes.includes('plan'),
        phase_review: taskTypes.includes('review'),
        phase_maintainer: taskTypes.includes('maintainer'),
        pi_api_key: '',
        pi_thinking_enabled: typeof worker.pi_thinking_enabled === 'boolean'
          ? worker.pi_thinking_enabled
          : legacyThinking !== 'off',
        pi_thinking_level: thinkingLevels.includes(configuredThinkingLevel)
          ? configuredThinkingLevel
          : (thinkingLevels.includes(legacyThinking) ? legacyThinking : 'medium'),
        mcp_enabled: !!worker.mcp_enabled,
        mcp_profile: worker.mcp_profile || '',
        mcp_config_json: worker.mcp_config_json || '',
        mcp_allowed_servers: worker.mcp_allowed_servers || '',
        mcp_allowed_tools: worker.mcp_allowed_tools || '',
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

    normalizeWorkerThinkingLevel(worker) {
      const level = String(worker.pi_thinking_level || '').trim().toLowerCase();
      const allowed = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
      if (!allowed.includes(level)) {
        throw new Error(`Worker ${worker.name || '未命名'} 的思考强度无效`);
      }
      worker.pi_thinking_level = level;
      return level;
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
      this.workerModelFilter = '';
      const model = (worker.available_models || []).find(item => item.id === modelId);
      if (!model || !model.context_window) return;
      worker.pi_model_context_window = String(model.context_window);
      worker.show_model_dropdown = false;
    },

    filteredWorkerModels(worker) {
      const list = Array.isArray(worker?.available_models) ? worker.available_models : [];
      const keyword = (this.workerModelFilter || '').trim().toLowerCase();
      if (!keyword) return list;
      return list.filter(item => (item.id || '').toLowerCase().includes(keyword));
    },

    toggleWorkerModelDropdown(worker) {
      if (!Array.isArray(worker.available_models) || worker.available_models.length === 0) {
        this.showToast('请先获取模型列表', 'error');
        return;
      }
      worker.show_model_dropdown = !worker.show_model_dropdown;
      if (worker.show_model_dropdown) {
        // 依据模型输入行在可视卡片内的剩余空间决定上/下弹与限高，避免被卡片边缘裁剪。
        this.$nextTick(() => {
          const input = document.querySelector('input[placeholder="输入或从下拉选择模型 ID"]');
          const panel = input ? input.closest('.sto-field')?.querySelector('div[x-show*="show_model_dropdown"]') : null;
          if (!input || !panel) return;
          const card = input.closest('.sto-panel');
          const inputRect = input.getBoundingClientRect();
          const cardRect = (card || document.body).getBoundingClientRect();
          const spaceBelow = cardRect.bottom - inputRect.bottom - 14;
          const spaceAbove = inputRect.top - cardRect.top - 14;
          const desired = Math.min(280, panel.scrollHeight + 14);
          if (spaceBelow >= desired || spaceBelow >= spaceAbove) {
            panel.style.top = 'calc(100% + 6px)';
            panel.style.bottom = 'auto';
            panel.style.maxHeight = `${Math.max(160, Math.min(280, spaceBelow))}px`;
          } else {
            panel.style.top = 'auto';
            panel.style.bottom = 'calc(100% + 6px)';
            panel.style.maxHeight = `${Math.max(160, Math.min(280, spaceAbove))}px`;
          }
        });
      }
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

    async loadSeeEvents(reset = false, options = {}) {
      if (!this.selectedProjectId) {
        this.seeEvents = [];
        this.seeLastEventId = 0;
        return [];
      }
      if (reset) {
        this.seeLastEventId = 0;
        this.seeEvents = [];
      }
      try {
        const events = await this.api(
          'GET',
          `/projects/${this.selectedProjectId}/events?after_id=${this.seeLastEventId}&limit=200`,
          null,
          options,
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
        return Array.isArray(events) ? events : [];
      } catch (e) {
        if (e?.name !== 'AbortError') console.error(e);
        return [];
      }
    },

    workerPayload(worker) {
      const taskTypes = [];
      if (worker.phase_bootstrap) taskTypes.push('bootstrap');
      if (worker.phase_reason) taskTypes.push('reason');
      if (worker.phase_explore) taskTypes.push('explore');
      if (worker.phase_plan) taskTypes.push('plan');
      if (worker.phase_review) taskTypes.push('review');
      if (worker.phase_maintainer) taskTypes.push('maintainer');
      if (taskTypes.length === 0) {
        throw new Error(`Worker ${worker.name || '未命名'} 至少要勾选一个参与阶段`);
      }
      const normalizedContextWindow = this.normalizeWorkerContextWindow(worker);
      const normalizedThinkingLevel = this.normalizeWorkerThinkingLevel(worker);
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
        pi_thinking_enabled: !!worker.pi_thinking_enabled,
        pi_thinking_level: normalizedThinkingLevel,
        mcp_enabled: !!worker.mcp_enabled,
        mcp_profile: (worker.mcp_profile || '').trim(),
        mcp_config_json: (worker.mcp_config_json || '').trim(),
        mcp_allowed_servers: (worker.mcp_allowed_servers || '').trim(),
        mcp_allowed_tools: (worker.mcp_allowed_tools || '').trim(),
        disabled: !!worker.disabled,
      };
    },

    dispatchPayload() {
      return {
        config_version: Number(this.dispatchSettings.config_version ?? 1),
        runner_interval_seconds: Number(this.dispatchSettings.runner_interval_seconds ?? 3),
        global_max_active_runs: Number(this.dispatchSettings.global_max_active_runs ?? 10),
        global_max_active_tasks: Number(this.dispatchSettings.global_max_active_tasks ?? 3),
        per_task_max_active_runs: Number(this.dispatchSettings.per_task_max_active_runs ?? 4),
        per_task_explore_concurrency: Number(this.dispatchSettings.per_task_explore_concurrency ?? 4),
        per_task_review_concurrency: Number(this.dispatchSettings.per_task_review_concurrency ?? 4),
        heartbeat_interval_seconds: Number(this.dispatchSettings.heartbeat_interval_seconds ?? 10),
        lease_timeout_seconds: Number(this.dispatchSettings.lease_timeout_seconds ?? 60),
        execution_retry_limit: Number(this.dispatchSettings.execution_retry_limit ?? 6),
        infrastructure_retry_limit: Number(this.dispatchSettings.infrastructure_retry_limit ?? 3),
        retry_base_seconds: Number(this.dispatchSettings.retry_base_seconds ?? 15),
        retry_max_seconds: Number(this.dispatchSettings.retry_max_seconds ?? 300),
        worker_failure_cooldown_seconds: Number(this.dispatchSettings.worker_failure_cooldown_seconds ?? 30),
        bootstrap_execute_seconds: Number(this.dispatchSettings.bootstrap_execute_seconds ?? 300),
        bootstrap_conclude_seconds: Number(this.dispatchSettings.bootstrap_conclude_seconds ?? 120),
        reason_execute_seconds: Number(this.dispatchSettings.reason_execute_seconds ?? 300),
        reason_conclude_seconds: Number(this.dispatchSettings.reason_conclude_seconds ?? 120),
        explore_execute_default_seconds: Number(this.dispatchSettings.explore_execute_default_seconds ?? 900),
        explore_conclude_default_seconds: Number(this.dispatchSettings.explore_conclude_default_seconds ?? 300),
        explore_no_progress_seconds: Number(this.dispatchSettings.explore_no_progress_seconds ?? 300),
        plan_execute_seconds: Number(this.dispatchSettings.plan_execute_seconds ?? 600),
        plan_conclude_seconds: Number(this.dispatchSettings.plan_conclude_seconds ?? 300),
        review_execute_seconds: Number(this.dispatchSettings.review_execute_seconds ?? 300),
        review_conclude_seconds: Number(this.dispatchSettings.review_conclude_seconds ?? 120),
        reason_batch_size: Number(this.dispatchSettings.reason_batch_size ?? 2),
        reason_low_watermark: Number(this.dispatchSettings.reason_low_watermark ?? 1),
        reason_max_wait_seconds: Number(this.dispatchSettings.reason_max_wait_seconds ?? 180),
        reason_min_interval_seconds: Number(this.dispatchSettings.reason_min_interval_seconds ?? 60),
        reason_queue_limit: Number(this.dispatchSettings.reason_queue_limit ?? 3),
        plan_queue_limit: Number(this.dispatchSettings.plan_queue_limit ?? 3),
        plan_max_consecutive_runs: Number(this.dispatchSettings.plan_max_consecutive_runs ?? 3),
        sandbox_backend: this.dispatchSettings.sandbox_backend || 'local',
        sandbox_required: Boolean(this.dispatchSettings.sandbox_required),
        sandbox_local_fallback: Boolean(this.dispatchSettings.sandbox_local_fallback),
        sandbox_template: String(this.dispatchSettings.sandbox_template || 'axiom-worker'),
        sandbox_runtime_revision: String(this.dispatchSettings.sandbox_runtime_revision || 'legacy-local'),
        sandbox_healthcheck_timeout: Number(this.dispatchSettings.sandbox_healthcheck_timeout ?? 10),
        browser_gateway_enabled: Boolean(this.dispatchSettings.browser_gateway_enabled),
        browser_gateway_bind: String(this.dispatchSettings.browser_gateway_bind || '127.0.0.1'),
        browser_gateway_base_url: String(this.dispatchSettings.browser_gateway_base_url || ''),
        browser_file_bridge_limit: Number(this.dispatchSettings.browser_file_bridge_limit ?? 2097152),
        explore_execute_min_seconds: Number(this.dispatchSettings.explore_execute_min_seconds ?? 60),
        explore_execute_max_seconds: Number(this.dispatchSettings.explore_execute_max_seconds ?? 3600),
        explore_conclude_min_seconds: Number(this.dispatchSettings.explore_conclude_min_seconds ?? 180),
        explore_conclude_max_seconds: Number(this.dispatchSettings.explore_conclude_max_seconds ?? 600),
        network_proxy_mode: this.dispatchSettings.network_proxy_mode || 'off',
        network_proxy_url: this.dispatchSettings.network_proxy_url || 'http://127.0.0.1:7890',
        dingtalk_enabled: Boolean(this.dispatchSettings.dingtalk_enabled),
        dingtalk_webhook_url: String(this.dispatchSettings.dingtalk_webhook_url || ''),
        dingtalk_secret: String(this.dispatchSettings.dingtalk_secret || ''),
        dingtalk_app_key: String(this.dispatchSettings.dingtalk_app_key || ''),
        dingtalk_app_secret: String(this.dispatchSettings.dingtalk_app_secret || ''),
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
        phase_review: false,
        max_running: 1,
        priority: 1,
        pi_model: '',
        pi_base_url: '',
        pi_provider_api: 'openai-completions',
        pi_model_context_window: '200000',
        pi_api_key: '',
        pi_api_key_masked: '',
        pi_thinking_enabled: false,
        pi_thinking_level: 'medium',
        mcp_enabled: false,
        mcp_profile: '',
        mcp_config_json: '',
        mcp_allowed_servers: '',
        mcp_allowed_tools: '',
        disabled: false,
        testing: false,
        test_result: null,
        models_loading: false,
        models_error: '',
        available_models: [],
        show_model_dropdown: false,
      });
    },

    async removePiWorker(index) {
      const worker = this.dispatchSettings.workers[index];
      if (!worker) return;
      if (!worker.persisted_name) {
        this.dispatchSettings.workers.splice(index, 1);
        return;
      }
      try {
        const updated = await this.api('DELETE', `/settings/dispatch/workers/${encodeURIComponent(worker.persisted_name)}`);
        this.applyDispatchSettings(updated);
        this.showToast(`Worker ${worker.persisted_name} 已删除`);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async toggleWorkerDisabled(worker) {
      if (!worker.persisted_name) {
        worker.disabled = !worker.disabled;
        return;
      }
      try {
        const updated = await this.api('PUT', `/settings/dispatch/workers/${encodeURIComponent(worker.persisted_name)}/toggle`);
        this.applyDispatchSettings(updated);
        this.showToast(`Worker ${worker.name} ${worker.disabled ? '已启用' : '已禁用'}`);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async savePiWorker(worker) {
      try {
        const persistedName = worker.persisted_name || '__new__';
        const updated = await this.api(
          'PUT',
          `/settings/dispatch/workers/${encodeURIComponent(persistedName)}`,
          this.workerPayload(worker),
        );
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
        this.workerPresetPending = '';
        this.showToast('系统配置已保存');
        this.waitForRuntimeSettingsApplied(updated.config_version);
      } catch(e) {
        this.showToast(e.message, 'error');
        if (e.status === 409) await this.loadDispatchSettings();
      }
    },

    waitForRuntimeSettingsApplied(version, attempts = 6) {
      if (!version || attempts <= 0) return;
      window.setTimeout(async () => {
        try {
          const data = await this.api('GET', '/settings/dispatch');
          this.applyDispatchSettings(data);
          if (Number(data.dispatcher_applied_version ?? 0) < Number(version)) {
            this.waitForRuntimeSettingsApplied(version, attempts - 1);
          }
        } catch (_) {
          this.waitForRuntimeSettingsApplied(version, attempts - 1);
        }
      }, 1500);
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
      const version = this.dispatchSettings.dispatcher_applied_version ?? 0;
      const appliedAt = this.dispatchSettings.dispatcher_applied_at;
      if (!version) return '等待 dispatcher 上报';
      return appliedAt ? `v${version} · ${this.formatTime(appliedAt)}` : `v${version}`;
    },

    runtimeSettingsApplied() {
      return Number(this.dispatchSettings.dispatcher_applied_version ?? 0)
        >= Number(this.dispatchSettings.config_version ?? 0);
    },

    dingtalkStreamReady() {
      return !!(this.dispatchSettings.dingtalk_app_key && this.dispatchSettings.dingtalk_app_secret);
    },

    bootstrapOutcomeLabel(events) {
      const bootstrapEvents = (events || [])
        .filter(event => event?.task_type === 'bootstrap')
        .filter(event => event?.payload && typeof event.payload === 'object' && event.payload.conclusion);
      if (bootstrapEvents.length === 0) return '暂无 Bootstrap 结论';
      const latest = bootstrapEvents[bootstrapEvents.length - 1];
      const mapping = {
        fact_written: '已写入初始事实',
        complete: '已直接完成目标',
        rejected: 'Bootstrap 已拒绝',
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

    async loadMcpServers() {
      try {
        const servers = await this.api('GET', '/mcp/servers');
        this.mcpServers = (servers || []).map(server => ({
          ...server,
          tools: server.tools || [],
          _newToolName: '',
          _syncingTools: false,
        }));
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    mcpEnabledWorkers() {
      return (this.dispatchSettings.workers || []).filter(worker => !worker.disabled);
    },

    mcpServerSummary(server) {
      if (server.url) return server.url;
      const args = server.args_json && server.args_json !== '[]' ? ` ${server.args_json}` : '';
      return `${server.command || ''}${args}`.trim() || '未配置';
    },

    mcpToolCountLabel(server) {
      const tools = server.tools || [];
      if (!tools.length) return '未登记工具';
      const enabled = tools.filter(tool => tool.enabled).length;
      return `${enabled}/${tools.length} 已启用`;
    },

    enabledMcpTools(server) {
      return ((server && server.tools) || []).filter(tool => tool.enabled);
    },

    isDangerousMcpTool(toolName) {
      return /(click|type|input|press|navigate|goto|scroll|drag|evaluate|eval|execute|control|tap)/i.test(toolName || '');
    },

    mcpScopedToolName(serverName, toolName) {
      return `${serverName}:${toolName}`;
    },

    csvItems(raw) {
      return (raw || '').split(',').map(item => item.trim()).filter(Boolean);
    },

    async addMcpTool(server) {
      const name = (server._newToolName || '').trim();
      if (!name) {
        this.showToast('请输入工具名', 'error');
        return;
      }
      try {
        await this.api('POST', `/mcp/servers/${server.id}/tools`, { name, enabled: true, source: 'manual' });
        server._newToolName = '';
        await this.loadMcpServers();
        this.showToast('MCP 工具已添加');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async importMcpTools(server) {
      const text = (this.mcpToolImportText[server.id] || '').trim();
      if (!text) {
        this.showToast('请粘贴工具名，每行一个或逗号分隔', 'error');
        return;
      }
      try {
        await this.api('POST', `/mcp/servers/${server.id}/tools/import`, { text });
        this.mcpToolImportText[server.id] = '';
        await this.loadMcpServers();
        this.showToast('MCP 工具已导入');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async deleteMcpTool(server, tool) {
      if (!confirm(`确定要删除工具 "${tool.name}" 吗？`)) return;
      try {
        await this.api('DELETE', `/mcp/servers/${server.id}/tools/${tool.id}`);
        await this.loadMcpServers();
        this.showToast('MCP 工具已删除');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async disableDangerousMcpTools(server) {
      const tools = (server.tools || []).filter(tool => tool.enabled && this.isDangerousMcpTool(tool.name));
      if (!tools.length) {
        this.showToast('没有发现需要禁用的控制类工具');
        return;
      }
      try {
        await Promise.all(tools.map(tool => this.api('PUT', `/mcp/servers/${server.id}/tools/${tool.id}`, { enabled: false })));
        await this.loadMcpServers();
        this.showToast(`已禁用 ${tools.length} 个控制类工具`);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async enableAllMcpTools(server) {
      const tools = (server.tools || []).filter(tool => !tool.enabled);
      try {
        await Promise.all(tools.map(tool => this.api('PUT', `/mcp/servers/${server.id}/tools/${tool.id}`, { enabled: true })));
        await this.loadMcpServers();
        this.showToast('已启用全部已登记工具');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async syncMcpTools(server, options = {}) {
      if (!server || !server.id) return [];
      server._syncingTools = true;
      try {
        const tools = await this.api('POST', `/mcp/servers/${server.id}/tools/sync`);
        if (!options.skipReload) await this.loadMcpServers();
        if (!options.silent) this.showToast(`已同步 ${tools.length} 个 MCP 工具`);
        return tools;
      } catch (e) {
        if (!options.silent) this.showToast(e.message, 'error');
        throw e;
      } finally {
        server._syncingTools = false;
      }
    },

    openMcpServerModal(server = null) {
      if (server) {
        this.mcpServerForm = { ...server };
        // url may live in server.url (newer API) or packed inside args_json (legacy / stdio entries).
        let url = server.url || '';
        if (!url && server.args_json) {
          try {
            const parsed = JSON.parse(server.args_json);
            if (parsed && typeof parsed === 'object' && parsed.url) url = parsed.url;
          } catch (e) {}
        }
        const isStdio = !!server.command;
        this.mcpServerFormJson = JSON.stringify({
          name: server.name || '',
          command: server.command || '',
          url: url,
          args_json: isStdio ? (server.args_json || '[]') : '[]',
          env_json: server.env_json || '{}',
        }, null, 2);
      } else {
        this.mcpServerForm = { id: '', name: '', command: '', url: '', args_json: '[]', env_json: '{}', disabled: false };
        this.mcpServerFormJson = '{"name":"","command":"","url":"","args_json":"[]","env_json":"{}"}';
      }
      this.showMcpServerModal = true;
    },

    closeMcpServerModal() {
      this.showMcpServerModal = false;
      this.mcpServerForm = { id: '', name: '', command: '', url: '', args_json: '[]', env_json: '{}', disabled: false };
      this.mcpServerFormJson = '';
    },

    _parseMcpServerJson(rawJson) {
      let parsed;
      try {
        parsed = JSON.parse(rawJson || '{}');
      } catch (e) {
        return { error: 'JSON 格式错误：' + e.message };
      }
      if (!parsed || typeof parsed !== 'object') {
        return { error: 'JSON 必须是对象' };
      }
      // Unwrap standard mcpServers wrapper: {"mcpServers": {"name": {"url|command..."}}}
      if (
        !parsed.name &&
        parsed.mcpServers && typeof parsed.mcpServers === 'object' &&
        !Array.isArray(parsed.mcpServers)
      ) {
        const entries = Object.entries(parsed.mcpServers);
        if (entries.length === 0) {
          return { error: 'mcpServers 里至少要有一个 server' };
        }
        if (entries.length > 1) {
          return { error: '一次只能保存一个 MCP Server（检测到 ' + entries.length + ' 个）' };
        }
        const [name, config] = entries[0];
        if (!config || typeof config !== 'object') {
          return { error: 'mcpServers.' + name + ' 必须是对象' };
        }
        config.name = name;
        parsed = config;
      }
      if (!parsed.name || !String(parsed.name).trim()) {
        return { error: 'name 为必填字段' };
      }
      const cmd = parsed.command ? String(parsed.command).trim() : '';
      const url = parsed.url ? String(parsed.url).trim() : '';
      if (!cmd && !url) {
        return { error: '必须提供 command（stdio）或 url（sse/http）' };
      }
      parsed.command = cmd;
      parsed.url = url;
      return { value: parsed };
    },

    async saveMcpServerFromJson() {
      const result = this._parseMcpServerJson(this.mcpServerFormJson);
      if (result.error) {
        this.showToast(result.error, 'error');
        return;
      }
      const parsed = result.value;
      const payload = {
        name: parsed.name,
        command: parsed.command || null,
        url: parsed.url || null,
        args_json: parsed.args_json || '[]',
        env_json: parsed.env_json || '{}',
        disabled: !!this.mcpServerForm.disabled,
      };
      // Sync the form JSON so subsequent saves round-trip identically.
      this.mcpServerFormJson = JSON.stringify({
        name: parsed.name,
        command: parsed.command || '',
        url: parsed.url || '',
        args_json: payload.args_json,
        env_json: payload.env_json,
      }, null, 2);
      try {
        const wasEdit = !!this.mcpServerForm.id;
        let savedServer;
        if (wasEdit) {
          savedServer = await this.api('PUT', '/mcp/servers/' + this.mcpServerForm.id, payload);
        } else {
          savedServer = await this.api('POST', '/mcp/servers', payload);
        }
        let syncMessage = '';
        try {
          const tools = await this.syncMcpTools(savedServer, { silent: true, skipReload: true });
          syncMessage = `，已同步 ${tools.length} 个工具`;
        } catch (syncError) {
          syncMessage = `，但工具同步失败：${syncError.message}`;
        }
        this.closeMcpServerModal();
        await this.loadMcpServers();
        await this.loadDispatchSettings();
        this.showToast(`MCP Server ${wasEdit ? '已更新' : '已创建'}${syncMessage}`);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async toggleMcpServerDisabled(server) {
      try {
        await this.api('PUT', `/mcp/servers/${server.id}`, {
          disabled: !server.disabled
        });
        this.showToast(`MCP Server ${server.name} ${server.disabled ? '已启用' : '已禁用'}`);
        await this.loadMcpServers();
        await this.loadDispatchSettings();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async deleteMcpServer(server) {
      if (!confirm(`确定要删除 MCP Server "${server.name}" 吗？`)) return;
      try {
        await this.api('DELETE', `/mcp/servers/${server.id}`);
        this.showToast('MCP Server 已删除');
        await this.loadMcpServers();
        await this.loadDispatchSettings();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    isServerEnabledForWorker(worker, serverName) {
      const allowed = this.csvItems(worker.mcp_allowed_servers || '');
      return allowed.includes(serverName);
    },

    async toggleWorkerServer(worker, serverName) {
      const allowed = this.csvItems(worker.mcp_allowed_servers || '');
      const idx = allowed.indexOf(serverName);
      if (idx >= 0) {
        allowed.splice(idx, 1);
        this.removeWorkerToolsForServer(worker, serverName);
      } else {
        allowed.push(serverName);
        this.addWorkerToolsForServerIfNarrowed(worker, serverName);
      }
      worker.mcp_allowed_servers = allowed.join(', ');
      await this.savePiWorker(worker);
    },

    enabledToolsForServerName(serverName) {
      const server = (this.mcpServers || []).find(item => item.name === serverName);
      return ((server && server.tools) || []).filter(tool => tool.enabled);
    },

    workerAvailableToolTokens(worker) {
      const serverNames = this.csvItems(worker.mcp_allowed_servers || '');
      const tokens = [];
      serverNames.forEach(serverName => {
        this.enabledToolsForServerName(serverName).forEach(tool => {
          tokens.push(this.mcpScopedToolName(serverName, tool.name));
        });
      });
      return tokens;
    },

    removeWorkerToolsForServer(worker, serverName) {
      const prefix = `${serverName}:`;
      const slashPrefix = `${serverName}/`;
      const filtered = this.csvItems(worker.mcp_allowed_tools || '').filter(token => (
        !token.startsWith(prefix) && !token.startsWith(slashPrefix)
      ));
      worker.mcp_allowed_tools = filtered.join(', ');
    },

    addWorkerToolsForServerIfNarrowed(worker, serverName) {
      const existing = this.csvItems(worker.mcp_allowed_tools || '');
      if (!existing.length) return;
      const tokens = new Set(existing);
      this.enabledToolsForServerName(serverName).forEach(tool => {
        tokens.add(this.mcpScopedToolName(serverName, tool.name));
      });
      worker.mcp_allowed_tools = Array.from(tokens).join(', ');
    },

    isToolEnabledForWorker(worker, serverName, toolName) {
      if (!worker.mcp_enabled || !this.isServerEnabledForWorker(worker, serverName)) return false;
      const allowedTools = this.csvItems(worker.mcp_allowed_tools || '');
      if (!allowedTools.length) return true;
      return allowedTools.includes(toolName)
        || allowedTools.includes(this.mcpScopedToolName(serverName, toolName))
        || allowedTools.includes(`${serverName}/${toolName}`);
    },

    async toggleWorkerTool(worker, serverName, toolName) {
      if (!this.isServerEnabledForWorker(worker, serverName)) {
        const allowed = this.csvItems(worker.mcp_allowed_servers || '');
        allowed.push(serverName);
        worker.mcp_allowed_servers = allowed.join(', ');
        this.addWorkerToolsForServerIfNarrowed(worker, serverName);
      }
      const scoped = this.mcpScopedToolName(serverName, toolName);
      let allowedTools = this.csvItems(worker.mcp_allowed_tools || '');
      if (!allowedTools.length) {
        allowedTools = this.workerAvailableToolTokens(worker).filter(token => token !== scoped);
      } else {
        const variants = new Set([toolName, scoped, `${serverName}/${toolName}`]);
        const hasTool = allowedTools.some(token => variants.has(token));
        allowedTools = allowedTools.filter(token => !variants.has(token));
        if (!hasTool) allowedTools.push(scoped);
      }
      worker.mcp_allowed_tools = allowedTools.join(', ');
      await this.savePiWorker(worker);
    },
  }
};
