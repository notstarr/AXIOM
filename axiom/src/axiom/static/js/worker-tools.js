/* AXIOM Worker / Tool management.
 *
 * This page intentionally keeps the four capability families separate.  The
 * existing settings adapter remains the persistence path for Pi connection
 * configuration; this module owns the Worker overview and AXIOM Tool phase
 * bindings only.
 */
window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.workerTools = {
  methods: {
    axiomPhaseLabel(phase) {
      return ({ bootstrap: 'Bootstrap', reason: 'Reason', explore: 'Explore' }[phase] || phase || '未知阶段');
    },

    axiomPhaseShortLabel(phase) {
      return ({ bootstrap: 'B', reason: 'R', explore: 'E' }[phase] || '?');
    },

    axiomToolStatusLabel(status) {
      return ({ ready: '就绪', invalid: '清单无效', missing: '包缺失' }[status] || status || '未知');
    },

    axiomToolStatusClass(status) {
      return ({
        ready: 'bg-emerald-50 text-emerald-700 border-emerald-200',
        invalid: 'bg-rose-50 text-rose-700 border-rose-200',
        missing: 'bg-amber-50 text-amber-700 border-amber-200',
      }[status] || 'bg-slate-100 text-slate-500 border-slate-200');
    },

    axiomWorkerConnectionLabel(worker) {
      return ({ ready: '可用', disabled: '已停用', cooldown: '冷却中' }[worker?.connection_state] || '未知');
    },

    axiomWorkerConnectionClass(worker) {
      return ({
        ready: 'bg-emerald-50 text-emerald-700 border-emerald-200',
        disabled: 'bg-slate-100 text-slate-500 border-slate-200',
        cooldown: 'bg-amber-50 text-amber-700 border-amber-200',
      }[worker?.connection_state] || 'bg-slate-100 text-slate-500 border-slate-200');
    },

    axiomWorkerCapability(worker, phase) {
      return Array.isArray(worker?.capabilities) && worker.capabilities.includes(phase);
    },

    axiomToolSupports(tool, phase) {
      return Array.isArray(tool?.supported_phases) && tool.supported_phases.includes(phase);
    },

    axiomToolRiskLabel(tag) {
      return ({
        network: '网络',
        'scope-sensitive': 'Scope 敏感',
        'filesystem-read': '文件读取',
        filesystem: '文件',
        'code-execution': '代码执行',
      }[tag] || tag || '未标记');
    },

    axiomToolFiltered() {
      const query = String(this.axiomToolSearch || '').trim().toLowerCase();
      const status = this.axiomToolStatusFilter || 'all';
      const source = this.axiomToolSourceFilter || 'all';
      const phase = this.axiomToolPhaseFilter || 'all';
      const risk = this.axiomToolRiskFilter || 'all';
      return (this.axiomToolsCatalog || []).filter(tool => {
        if (status !== 'all' && tool.status !== status) return false;
        if (source !== 'all' && tool.source !== source) return false;
        if (phase !== 'all' && !this.axiomToolSupports(tool, phase)) return false;
        if (risk !== 'all' && !(tool.risk_tags || []).includes(risk)) return false;
        if (!query) return true;
        return [tool.key, tool.title, tool.description, tool.version]
          .some(value => String(value || '').toLowerCase().includes(query));
      });
    },

    axiomToolRiskOptions() {
      return [...new Set((this.axiomToolsCatalog || []).flatMap(tool => tool.risk_tags || []))]
        .filter(Boolean).sort();
    },

    axiomToolPhases(tool) {
      return (tool?.supported_phases || []).map(phase => this.axiomPhaseLabel(phase)).join('、');
    },

    axiomToolPromptGuidelines(tool) {
      const values = tool?.manifest?.prompt_guidelines;
      return Array.isArray(values) ? values : [];
    },

    axiomToolSchemaProperties(tool) {
      const properties = tool?.input_schema?.properties;
      if (!properties || typeof properties !== 'object') return [];
      const required = new Set(tool?.input_schema?.required || []);
      return Object.entries(properties).map(([name, schema]) => ({
        name,
        type: Array.isArray(schema?.type) ? schema.type.join(' | ') : (schema?.type || 'any'),
        description: schema?.description || '',
        required: required.has(name),
        default: schema?.default,
      }));
    },

    axiomToolBindingsFor(tool) {
      return Array.isArray(tool?.bindings) ? tool.bindings : [];
    },

    axiomToolBindingSelected(tool, workerName, phase) {
      return this.axiomToolBindingsFor(tool).some(binding => (
        binding.worker_name === workerName && binding.phase === phase
      ));
    },

    axiomToolBindingEffective(tool, workerName, phase) {
      const binding = this.axiomToolBindingsFor(tool).find(item => (
        item.worker_name === workerName && item.phase === phase
      ));
      return binding?.effective === true;
    },

    axiomToolBindingReason(tool, workerName, phase) {
      const binding = this.axiomToolBindingsFor(tool).find(item => (
        item.worker_name === workerName && item.phase === phase
      ));
      return binding?.inactive_reason || '';
    },

    axiomToolDraftBindings(tool) {
      if (!tool) return [];
      if (!Array.isArray(this.axiomToolBindingsDraft?.[tool.key])) {
        this.axiomToolBindingsDraft = {
          ...this.axiomToolBindingsDraft,
          [tool.key]: this.axiomToolBindingsFor(tool).map(binding => ({
            worker_name: binding.worker_name,
            phase: binding.phase,
          })),
        };
      }
      return this.axiomToolBindingsDraft[tool.key];
    },

    axiomToolDraftSelected(tool, workerName, phase) {
      return this.axiomToolDraftBindings(tool).some(binding => (
        binding.worker_name === workerName && binding.phase === phase
      ));
    },

    toggleAxiomToolDraft(tool, workerName, phase) {
      if (!tool || !this.axiomToolSupports(tool, phase)) return;
      const current = [...this.axiomToolDraftBindings(tool)];
      const index = current.findIndex(binding => (
        binding.worker_name === workerName && binding.phase === phase
      ));
      if (index >= 0) current.splice(index, 1);
      else current.push({ worker_name: workerName, phase });
      this.axiomToolBindingsDraft = { ...this.axiomToolBindingsDraft, [tool.key]: current };
    },

    async saveAxiomToolBindings(tool) {
      if (!tool || this.axiomToolBindingsSaving[tool.key]) return;
      this.axiomToolBindingsSaving = { ...this.axiomToolBindingsSaving, [tool.key]: true };
      try {
        const saved = await this.api('PUT', `/axiom-tools/${encodeURIComponent(tool.key)}/bindings`, {
          bindings: this.axiomToolDraftBindings(tool),
        });
        this.axiomToolsCatalog = (this.axiomToolsCatalog || []).map(item => (
          item.key === saved.key ? saved : item
        ));
        this.axiomToolBindingsDraft = {
          ...this.axiomToolBindingsDraft,
          [tool.key]: (saved.bindings || []).map(binding => ({
            worker_name: binding.worker_name,
            phase: binding.phase,
          })),
        };
        await this.loadAxiomWorkersOverview({ preserveSelection: true });
        this.showToast(`工具 ${tool.key} 的绑定已保存`);
      } catch (error) {
        this.showToast(error?.message || '保存工具绑定失败', 'error');
        await this.loadAxiomToolsCatalog({ preserveSelection: true });
      } finally {
        this.axiomToolBindingsSaving = { ...this.axiomToolBindingsSaving, [tool.key]: false };
      }
    },

    async toggleAxiomToolEnabled(tool) {
      if (!tool) return;
      try {
        const updated = await this.api('PATCH', `/axiom-tools/${encodeURIComponent(tool.key)}`, {
          enabled: !tool.enabled,
        });
        this.axiomToolsCatalog = (this.axiomToolsCatalog || []).map(item => (
          item.key === updated.key ? updated : item
        ));
        this.axiomToolBindingsDraft = {
          ...this.axiomToolBindingsDraft,
          [updated.key]: (updated.bindings || []).map(binding => ({
            worker_name: binding.worker_name,
            phase: binding.phase,
          })),
        };
        this.showToast(updated.enabled ? `工具 ${updated.key} 已启用` : `工具 ${updated.key} 已停用`);
      } catch (error) {
        this.showToast(error?.message || '更新工具状态失败', 'error');
      }
    },

    async scanAxiomTools() {
      if (this.axiomToolsScanLoading) return;
      this.axiomToolsScanLoading = true;
      try {
        const result = await this.api('POST', '/axiom-tools/scan');
        this.axiomToolsCatalog = result.items || [];
        this.axiomToolsCatalog.forEach(tool => {
          this.axiomToolBindingsDraft = {
            ...this.axiomToolBindingsDraft,
            [tool.key]: (tool.bindings || []).map(binding => ({
              worker_name: binding.worker_name,
              phase: binding.phase,
            })),
          };
        });
        if (!this.selectedAxiomTool && this.axiomToolsCatalog.length) {
          this.selectedAxiomTool = this.axiomToolsCatalog[0].key;
        }
        this.showToast(`扫描完成：${result.ready} 个工具就绪`);
      } catch (error) {
        this.showToast(error?.message || '扫描 AXIOM Tool 失败', 'error');
      } finally {
        this.axiomToolsScanLoading = false;
      }
    },

    selectedAxiomToolRecord() {
      return (this.axiomToolsCatalog || []).find(tool => tool.key === this.selectedAxiomTool) || null;
    },

    selectAxiomTool(tool) {
      const key = typeof tool === 'string' ? tool : tool?.key;
      if (!key) return;
      this.selectedAxiomTool = key;
      const record = (this.axiomToolsCatalog || []).find(item => item.key === key);
      if (record) this.axiomToolDraftBindings(record);
    },

    async loadAxiomToolsCatalog({ preserveSelection = true } = {}) {
      this.axiomToolsLoading = true;
      try {
        const items = await this.api('GET', '/axiom-tools', null, {
          signal: this._pageAbortController?.signal,
        });
        this.axiomToolsCatalog = Array.isArray(items) ? items : [];
        this.axiomToolsCatalog.forEach(tool => {
          this.axiomToolBindingsDraft = {
            ...this.axiomToolBindingsDraft,
            [tool.key]: (tool.bindings || []).map(binding => ({
              worker_name: binding.worker_name,
              phase: binding.phase,
            })),
          };
        });
        if (!preserveSelection || !this.axiomToolsCatalog.some(item => item.key === this.selectedAxiomTool)) {
          this.selectedAxiomTool = this.axiomToolsCatalog[0]?.key || '';
        }
      } catch (error) {
        if (error?.name !== 'AbortError') this.showToast(error?.message || '加载 AXIOM Tool 失败', 'error');
      } finally {
        this.axiomToolsLoading = false;
      }
    },

    async loadAxiomWorkersOverview({ preserveSelection = true } = {}) {
      this.axiomWorkersLoading = true;
      try {
        const workers = await this.api('GET', '/workers/overview', null, {
          signal: this._pageAbortController?.signal,
        });
        this.axiomWorkers = Array.isArray(workers) ? workers : [];
        if (!preserveSelection || !this.axiomWorkers.some(item => item.name === this.selectedAxiomWorker)) {
          this.selectedAxiomWorker = this.axiomWorkers[0]?.name || '';
        }
        if (this.selectedAxiomWorker) await this.loadAxiomWorkerBindings(this.selectedAxiomWorker);
      } catch (error) {
        if (error?.name !== 'AbortError') this.showToast(error?.message || '加载 Worker 概览失败', 'error');
      } finally {
        this.axiomWorkersLoading = false;
      }
    },

    async loadAxiomWorkerBindings(workerName) {
      if (!workerName) return;
      this.axiomWorkerBindingsLoading = { ...this.axiomWorkerBindingsLoading, [workerName]: true };
      try {
        const result = await this.api('GET', `/workers/${encodeURIComponent(workerName)}/axiom-tools`, null, {
          signal: this._pageAbortController?.signal,
        });
        this.axiomWorkerBindings = { ...this.axiomWorkerBindings, [workerName]: result };
      } catch (error) {
        if (error?.name !== 'AbortError') this.showToast(error?.message || '加载 Worker Tool 绑定失败', 'error');
      } finally {
        this.axiomWorkerBindingsLoading = { ...this.axiomWorkerBindingsLoading, [workerName]: false };
      }
    },

    async selectAxiomWorker(worker) {
      const name = typeof worker === 'string' ? worker : worker?.name;
      if (!name) return;
      this.selectedAxiomWorker = name;
      await this.loadAxiomWorkerBindings(name);
      this.editAxiomWorker(name);
    },

    selectedAxiomWorkerRecord() {
      return (this.axiomWorkers || []).find(worker => worker.name === this.selectedAxiomWorker) || null;
    },

    selectedAxiomWorkerBindingRecord() {
      return this.axiomWorkerBindings?.[this.selectedAxiomWorker] || null;
    },

    axiomWorkerConfigured(workerName, phase, key) {
      const record = this.axiomWorkerBindings?.[workerName];
      return Array.isArray(record?.[phase]) && record[phase].includes(key);
    },

    axiomWorkerEffective(workerName, phase, key) {
      const record = this.axiomWorkerBindings?.[workerName];
      return Array.isArray(record?.effective?.[phase]) && record.effective[phase].includes(key);
    },

    axiomWorkerBindingReason(workerName, phase, key) {
      return this.selectedAxiomWorkerBindingRecord()?.inactive_reasons?.[`${phase}:${key}`] || '';
    },

    toggleAxiomWorkerTool(phase, key) {
      const name = this.selectedAxiomWorker;
      if (!name) return;
      const tool = (this.axiomToolsCatalog || []).find(item => item.key === key);
      if (!tool || tool.status !== 'ready' || !tool.enabled || !this.axiomToolSupports(tool, phase)) return;
      const current = this.axiomWorkerBindings?.[name] || { bootstrap: [], reason: [], explore: [] };
      const next = {
        bootstrap: [...(current.bootstrap || [])],
        reason: [...(current.reason || [])],
        explore: [...(current.explore || [])],
      };
      const index = next[phase].indexOf(key);
      if (index >= 0) next[phase].splice(index, 1);
      else next[phase].push(key);
      this.axiomWorkerBindings = {
        ...this.axiomWorkerBindings,
        [name]: { ...current, ...next },
      };
    },

    async saveAxiomWorkerTools() {
      const name = this.selectedAxiomWorker;
      const record = this.selectedAxiomWorkerBindingRecord();
      if (!name || !record || this.axiomWorkerBindingsSaving[name]) return;
      this.axiomWorkerBindingsSaving = { ...this.axiomWorkerBindingsSaving, [name]: true };
      try {
        const saved = await this.api('PUT', `/workers/${encodeURIComponent(name)}/axiom-tools`, {
          bootstrap: record.bootstrap || [],
          reason: record.reason || [],
          explore: record.explore || [],
        });
        this.axiomWorkerBindings = { ...this.axiomWorkerBindings, [name]: saved };
        await this.loadAxiomToolsCatalog({ preserveSelection: true });
        await this.loadAxiomWorkersOverview({ preserveSelection: true });
        this.showToast(`Worker ${name} 的阶段工具绑定已保存`);
      } catch (error) {
        this.showToast(error?.message || '保存 Worker Tool 绑定失败', 'error');
        await this.loadAxiomWorkerBindings(name);
      } finally {
        this.axiomWorkerBindingsSaving = { ...this.axiomWorkerBindingsSaving, [name]: false };
      }
    },

    editAxiomWorker(name) {
      const source = (this.dispatchSettings?.workers || []).find(worker => (
        (worker.persisted_name || worker.name) === name
      ));
      if (!source) {
        this.axiomWorkerEditor = null;
        return;
      }
      this.axiomWorkerEditor = JSON.parse(JSON.stringify(source));
    },

    newAxiomWorker() {
      this.axiomWorkerEditor = {
        _draftId: `axiom-worker-${Date.now()}`,
        persisted_name: '',
        name: `pi-worker-${(this.axiomWorkers || []).length + 1}`,
        task_types: ['bootstrap', 'reason', 'explore'],
        phase_bootstrap: true,
        phase_reason: true,
        phase_explore: true,
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
        disabled: false,
        testing: false,
        test_result: null,
        available_models: [],
        models_loading: false,
        models_error: '',
        show_model_dropdown: false,
      };
      this.selectedAxiomWorker = '';
    },

    async saveAxiomWorkerEditor() {
      const worker = this.axiomWorkerEditor;
      if (!worker || this.axiomWorkerEditorSaving) return;
      try {
        this.axiomWorkerEditorSaving = true;
        const persistedName = worker.persisted_name || '__new__';
        const payload = this.workerPayload(worker);
        const result = await this.api('PUT', `/settings/dispatch/workers/${encodeURIComponent(persistedName)}`, payload);
        this.applyDispatchSettings(result);
        await this.loadAxiomWorkersOverview({ preserveSelection: false });
        await this.loadAxiomToolsCatalog({ preserveSelection: true });
        this.selectedAxiomWorker = worker.name;
        this.editAxiomWorker(worker.name);
        await this.loadAxiomWorkerBindings(worker.name);
        this.showToast(`Worker ${worker.name} 已保存`);
      } catch (error) {
        this.showToast(error?.message || '保存 Worker 失败', 'error');
      } finally {
        this.axiomWorkerEditorSaving = false;
      }
    },

    async testAxiomWorkerEditor() {
      if (!this.axiomWorkerEditor || this.axiomWorkerEditorTesting) return;
      this.axiomWorkerEditorTesting = true;
      try {
        const worker = this.axiomWorkerEditor;
        const result = await this.api('POST', '/settings/test-pi', {
          worker_name: worker.name,
          pi_base_url: worker.pi_base_url,
          pi_model: worker.pi_model,
          pi_provider_api: worker.pi_provider_api || 'openai-completions',
          pi_api_key: worker.pi_api_key || '',
        });
        worker.test_result = result;
        this.showToast(result.message || '连接测试完成', result.ok ? 'info' : 'error');
      } catch (error) {
        this.axiomWorkerEditor.test_result = { ok: false, message: error?.message || '连接测试失败' };
      } finally {
        this.axiomWorkerEditorTesting = false;
      }
    },

    async deleteAxiomWorkerEditor() {
      const worker = this.axiomWorkerEditor;
      if (!worker?.persisted_name) {
        this.axiomWorkerEditor = null;
        return;
      }
      if (!window.confirm(`确认删除 Worker ${worker.persisted_name}？`)) return;
      try {
        await this.api('DELETE', `/settings/dispatch/workers/${encodeURIComponent(worker.persisted_name)}`);
        this.axiomWorkerEditor = null;
        await this.loadDispatchSettings();
        await this.loadAxiomWorkersOverview({ preserveSelection: false });
        await this.loadAxiomToolsCatalog({ preserveSelection: true });
        this.showToast(`Worker ${worker.persisted_name} 已删除`);
      } catch (error) {
        this.showToast(error?.message || '删除 Worker 失败', 'error');
      }
    },

    async loadAxiomWorkersPage() {
      await Promise.all([
        this.loadDispatchSettings?.(),
        this.loadAxiomToolsCatalog({ preserveSelection: true }),
        this.loadAxiomWorkersOverview({ preserveSelection: true }),
      ]);
      if (this.selectedAxiomWorker) this.editAxiomWorker(this.selectedAxiomWorker);
    },

    async loadAxiomToolsPage() {
      await Promise.all([
        this.loadAxiomToolsCatalog({ preserveSelection: true }),
        this.loadAxiomWorkersOverview({ preserveSelection: true }),
      ]);
    },
  },
};
