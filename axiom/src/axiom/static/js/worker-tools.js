/* AXIOM logical Worker and phase capability management.
 *
 * Four logical Workers (Bootstrap / Reason / Explore / Reviewer) own capability policy.
 * Concrete `workers` rows are model execution backends and may participate in
 * one or more phase pools.  Skill, MCP Tool and extension Tool all share the same
 * common/bootstrap/reason/explore/review binding service.  Built-in Tools are a
 * read-only projection of the real Pi and AXIOM control-plane contracts.
 */
window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.workerTools = {
  methods: {
    axiomPhases() {
      return ['bootstrap', 'reason', 'explore', 'review'];
    },

    axiomPhaseLabel(phase) {
      return ({ bootstrap: 'Bootstrap', reason: 'Reason', explore: 'Explore', review: 'Reviewer' }[phase] || phase || '未知阶段');
    },

    axiomPhaseShortLabel(phase) {
      return ({ bootstrap: 'B', reason: 'R', explore: 'E', review: 'V' }[phase] || '?');
    },

    capabilityScopeLabel(scope) {
      return ({ common: '全部阶段', bootstrap: 'Bootstrap', reason: 'Reason', explore: 'Explore', review: 'Reviewer' }[scope] || scope || '未知');
    },

    capabilityKindLabel(kind) {
      return ({ skill: 'Skill', mcp_tool: 'MCP Tool', axiom_tool: '扩展 Tool', builtin_tool: '内置 Tool' }[kind] || kind || '能力');
    },

    builtinToolSourceLabel(source) {
      return ({ pi_native: 'Pi 原生', axiom_internal: 'AXIOM 内部', axiom_browser: 'AXIOM 浏览器' }[source] || source || '内置');
    },

    builtinToolsForPhase(phase) {
      return (this.phaseCapabilities?.builtin_tools || []).filter(tool => (
        Array.isArray(tool.phases) && tool.phases.includes(phase)
      ));
    },

    builtinToolCount(phase) {
      return this.builtinToolsForPhase(phase).length;
    },

    builtinToolAvailabilityLabel(tool, phase) {
      const modes = tool?.availability?.[phase] || [];
      if (modes.includes('execute') && modes.includes('conclude')) return '执行与收口阶段均可用';
      if (modes.includes('execute')) return '仅执行阶段可用';
      if (modes.includes('conclude')) return '仅收口阶段可用';
      return '当前阶段不可用';
    },

    selectedBuiltinToolRecord() {
      return (this.phaseCapabilities?.builtin_tools || []).find(tool => tool.key === this.selectedBuiltinTool) || null;
    },

    selectBuiltinTool(tool) {
      const key = typeof tool === 'string' ? tool : tool?.key;
      if (key) this.selectedBuiltinTool = key;
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

    workerPhaseBackendRunning(phase, workerName) {
      const record = (this.workerPhases || []).find(item => item.phase === phase);
      const backend = (record?.backends || []).find(item => item.name === workerName);
      return Number(backend?.running || 0);
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

    phaseCapabilityId(kind, key) {
      return `${kind}:${key}`;
    },

    applyPhaseCapabilities(value) {
      const response = value && typeof value === 'object' ? value : { scopes: {}, items: [], builtin_tools: [] };
      this.phaseCapabilities = {
        scopes: response.scopes || {},
        items: Array.isArray(response.items) ? response.items : [],
        builtin_tools: Array.isArray(response.builtin_tools) ? response.builtin_tools : [],
      };
      const drafts = {};
      this.phaseCapabilities.items.forEach(item => {
        drafts[this.phaseCapabilityId(item.capability_kind, item.capability_key)] = [...(item.scopes || [])];
      });
      this.phaseCapabilityDraft = drafts;
      this.axiomToolsCatalog = (this.axiomToolsCatalog || []).map(tool => {
        const item = this.phaseCapabilityRecord('axiom_tool', tool.key);
        return item ? { ...tool, scopes: [...(item.scopes || [])] } : tool;
      });
      if (!(this.phaseCapabilities.builtin_tools || []).some(tool => tool.key === this.selectedBuiltinTool)) {
        this.selectedBuiltinTool = this.phaseCapabilities.builtin_tools?.[0]?.key || '';
      }
    },

    async loadPhaseCapabilities() {
      this.phaseCapabilitiesLoading = true;
      try {
        const value = await this.api('GET', '/phase-capabilities', null, {
          signal: this._pageAbortController?.signal,
        });
        this.applyPhaseCapabilities(value);
        return this.phaseCapabilities;
      } catch (error) {
        if (error?.name !== 'AbortError') this.showToast(error?.message || '加载阶段能力失败', 'error');
        return this.phaseCapabilities;
      } finally {
        this.phaseCapabilitiesLoading = false;
      }
    },

    phaseCapabilityRecord(kind, key) {
      return (this.phaseCapabilities?.items || []).find(item => (
        item.capability_kind === kind && item.capability_key === key
      )) || null;
    },

    phaseCapabilityDraftScopes(itemOrKind, key = '') {
      const item = typeof itemOrKind === 'string'
        ? this.phaseCapabilityRecord(itemOrKind, key)
        : itemOrKind;
      if (!item) return [];
      const id = this.phaseCapabilityId(item.capability_kind, item.capability_key);
      if (!Array.isArray(this.phaseCapabilityDraft?.[id])) {
        this.phaseCapabilityDraft = {
          ...this.phaseCapabilityDraft,
          [id]: [...(item.scopes || [])],
        };
      }
      return this.phaseCapabilityDraft[id];
    },

    phaseCapabilityScopeSelected(item, scope) {
      return this.phaseCapabilityDraftScopes(item).includes(scope);
    },

    phaseCapabilityScopeLocked(item, scope) {
      return Array.isArray(item?.locked_scopes) && item.locked_scopes.includes(scope);
    },

    phaseCapabilitySupportsScope(item, scope) {
      if (!item) return false;
      if (item.capability_kind !== 'axiom_tool') return true;
      const supported = new Set(item.supported_phases || []);
      if (scope === 'common') return this.axiomPhases().every(phase => supported.has(phase));
      return supported.has(scope);
    },

    phaseCapabilityScopeDisabled(item, scope) {
      const selected = this.phaseCapabilityScopeSelected(item, scope);
      if (selected) return this.phaseCapabilityScopeLocked(item, scope);
      if (scope !== 'common' && this.phaseCapabilityScopeSelected(item, 'common')) return true;
      if (!item?.enabled || !item?.ready) return true;
      return !this.phaseCapabilitySupportsScope(item, scope);
    },

    phaseCapabilityScopeHint(item, scope) {
      if (!item) return '';
      if (this.phaseCapabilityScopeLocked(item, scope)) return '锁定能力，不能取消';
      if (scope !== 'common' && this.phaseCapabilityScopeSelected(item, 'common')) {
        return '该能力已由通用范围生效；如需改为阶段专属，请先取消通用';
      }
      if (!this.phaseCapabilitySupportsScope(item, scope)) return scope === 'common'
        ? '通用能力必须支持四个阶段'
        : `该能力不支持 ${this.capabilityScopeLabel(scope)}`;
      if (!item.enabled) return '能力已全局停用；可保留旧绑定，但不能新增';
      if (!item.ready) return `能力状态为 ${item.status || 'not ready'}`;
      if (scope !== 'common' && item.inactive_reasons?.[scope]) return item.inactive_reasons[scope];
      return '';
    },

    togglePhaseCapabilityScope(item, scope) {
      if (!item || this.phaseCapabilityScopeDisabled(item, scope)) return;
      const current = [...this.phaseCapabilityDraftScopes(item)];
      const index = current.indexOf(scope);
      if (index >= 0) current.splice(index, 1);
      else if (scope === 'common') {
        current.splice(0, current.length, 'common');
      } else {
        current.push(scope);
      }
      const order = ['common', ...this.axiomPhases()];
      current.sort((left, right) => order.indexOf(left) - order.indexOf(right));
      this.phaseCapabilityDraft = {
        ...this.phaseCapabilityDraft,
        [this.phaseCapabilityId(item.capability_kind, item.capability_key)]: current,
      };
    },

    async savePhaseCapability(item) {
      if (!item) return;
      const id = this.phaseCapabilityId(item.capability_kind, item.capability_key);
      if (this.phaseCapabilitySaving?.[id]) return;
      this.phaseCapabilitySaving = { ...this.phaseCapabilitySaving, [id]: true };
      try {
        const saved = await this.api(
          'PUT',
          `/phase-capabilities/${encodeURIComponent(item.capability_kind)}/${encodeURIComponent(item.capability_key)}`,
          { scopes: this.phaseCapabilityDraftScopes(item) },
        );
        this.applyPhaseCapabilities(saved);
        this.showToast(`${this.capabilityKindLabel(item.capability_kind)} ${item.title || item.capability_key} 的阶段分配已保存`);
      } catch (error) {
        this.showToast(error?.message || '保存阶段能力失败', 'error');
        await this.loadPhaseCapabilities();
      } finally {
        this.phaseCapabilitySaving = { ...this.phaseCapabilitySaving, [id]: false };
      }
    },

    phaseCapabilityAssigned(item, phase) {
      const scopes = item?.scopes || [];
      return scopes.includes('common') || scopes.includes(phase);
    },

    phaseCapabilityEffective(item, phase) {
      return this.phaseCapabilityAssigned(item, phase) && !item?.inactive_reasons?.[phase];
    },

    phaseCapabilityCount(phase, kind, effective = true) {
      return (this.phaseCapabilities?.items || []).filter(item => (
        item.capability_kind === kind
        && (effective ? this.phaseCapabilityEffective(item, phase) : this.phaseCapabilityAssigned(item, phase))
      )).length;
    },

    phaseCapabilityScopeCount(scope, kind) {
      const values = this.phaseCapabilities?.scopes?.[scope]?.[kind];
      return Array.isArray(values) ? values.length : 0;
    },

    phaseCapabilityItemsFor(phase, kind, effective = false) {
      return (this.phaseCapabilities?.items || []).filter(item => (
        (!kind || item.capability_kind === kind)
        && (effective ? this.phaseCapabilityEffective(item, phase) : this.phaseCapabilityAssigned(item, phase))
      ));
    },

    phaseCapabilityCatalogForPhase(phase, kind) {
      return (this.phaseCapabilities?.items || []).filter(item => (
        item.capability_kind === kind
        && (
          this.phaseCapabilitySupportsScope(item, phase)
          || this.phaseCapabilityScopeSelected(item, 'common')
          || this.phaseCapabilityScopeSelected(item, phase)
        )
      ));
    },

    async toggleAndSavePhaseCapabilityScope(item, scope) {
      if (!item || this.phaseCapabilityScopeDisabled(item, scope)) return;
      const before = [...this.phaseCapabilityDraftScopes(item)];
      this.togglePhaseCapabilityScope(item, scope);
      const after = this.phaseCapabilityDraftScopes(item);
      if (before.length === after.length && before.every((value, index) => value === after[index])) return;
      await this.savePhaseCapability(item);
    },

    capabilityAssignedPhases(item) {
      const scopes = Array.isArray(item?.scopes) ? item.scopes : [];
      if (scopes.includes('common')) return this.axiomPhases();
      return this.axiomPhases().filter(phase => scopes.includes(phase));
    },

    capabilityPhaseSummary(item) {
      const phases = this.capabilityAssignedPhases(item);
      if (phases.length === this.axiomPhases().length) return '全部阶段';
      if (phases.length === 0) return '未分配';
      return phases.map(phase => this.axiomPhaseLabel(phase)).join(' · ');
    },

    workerCapabilityDraftPhases(item) {
      if (!item) return [];
      const id = this.phaseCapabilityId(item.capability_kind, item.capability_key);
      if (!Array.isArray(this.workerCapabilityDraft?.[id])) {
        this.workerCapabilityDraft = {
          ...(this.workerCapabilityDraft || {}),
          [id]: this.capabilityAssignedPhases(item),
        };
      }
      return this.workerCapabilityDraft[id];
    },

    resetWorkerCapabilityDraft() {
      const values = {};
      (this.phaseCapabilities?.items || []).forEach(item => {
        values[this.phaseCapabilityId(item.capability_kind, item.capability_key)] = this.capabilityAssignedPhases(item);
      });
      this.workerCapabilityDraft = JSON.parse(JSON.stringify(values));
      this.workerCapabilityBaseline = JSON.parse(JSON.stringify(values));
    },

    workerCapabilityPhaseSelected(item, phase) {
      return this.workerCapabilityDraftPhases(item).includes(phase);
    },

    workerCapabilitySupportsPhase(item, phase) {
      return this.phaseCapabilitySupportsScope(item, phase);
    },

    workerCapabilityPhaseLocked(item, phase) {
      const locked = Array.isArray(item?.locked_scopes) ? item.locked_scopes : [];
      return locked.includes('common') || locked.includes(phase);
    },

    workerCapabilityPhaseDisabled(item, phase) {
      if (!item || !this.axiomPhases().includes(phase)) return true;
      if (this.workerCapabilityPhaseLocked(item, phase)) return true;
      if (this.workerCapabilityPhaseSelected(item, phase)) return false;
      if (!this.workerCapabilitySupportsPhase(item, phase)) return true;
      return !item.enabled || !item.ready;
    },

    workerCapabilityPhaseHint(item, phase) {
      if (!item) return '';
      if (this.workerCapabilityPhaseLocked(item, phase)) return '系统锁定能力，不能修改';
      if (!this.workerCapabilitySupportsPhase(item, phase)) return `该能力不支持 ${this.axiomPhaseLabel(phase)}`;
      if (!item.enabled && !this.workerCapabilityPhaseSelected(item, phase)) return '能力已全局停用，只能解除已有分配';
      if (!item.ready && !this.workerCapabilityPhaseSelected(item, phase)) return `能力状态为 ${item.status || 'not ready'}，只能解除已有分配`;
      return item.inactive_reasons?.[phase] || '';
    },

    toggleWorkerCapabilityPhase(item, phase) {
      if (!item || this.workerCapabilityPhaseDisabled(item, phase)) return;
      const id = this.phaseCapabilityId(item.capability_kind, item.capability_key);
      const current = [...this.workerCapabilityDraftPhases(item)];
      const index = current.indexOf(phase);
      if (index >= 0) current.splice(index, 1);
      else current.push(phase);
      const order = this.axiomPhases();
      current.sort((left, right) => order.indexOf(left) - order.indexOf(right));
      this.workerCapabilityDraft = { ...this.workerCapabilityDraft, [id]: current };
    },

    workerCapabilityAllSelected(item) {
      return this.axiomPhases().every(phase => this.workerCapabilityPhaseSelected(item, phase));
    },

    workerCapabilityAllDisabled(item) {
      if (!item) return true;
      if (Array.isArray(item.locked_scopes) && item.locked_scopes.length > 0) return true;
      if (this.workerCapabilityAllSelected(item)) return false;
      if (!item.enabled || !item.ready) return true;
      return !this.axiomPhases().every(phase => this.workerCapabilitySupportsPhase(item, phase));
    },

    workerCapabilityAllHint(item) {
      if (!item) return '';
      if (Array.isArray(item.locked_scopes) && item.locked_scopes.length > 0) return '系统锁定为全部阶段';
      if (!this.axiomPhases().every(phase => this.workerCapabilitySupportsPhase(item, phase))) return '该能力不支持全部四个阶段';
      if (!item.enabled || !item.ready) return '能力当前不可新增分配';
      return this.workerCapabilityAllSelected(item) ? '清空四个阶段的分配' : '分配给全部四个阶段';
    },

    toggleWorkerCapabilityAll(item) {
      if (!item || this.workerCapabilityAllDisabled(item)) return;
      const id = this.phaseCapabilityId(item.capability_kind, item.capability_key);
      this.workerCapabilityDraft = {
        ...this.workerCapabilityDraft,
        [id]: this.workerCapabilityAllSelected(item) ? [] : this.axiomPhases(),
      };
    },

    workerCapabilityItemChanged(item) {
      if (!item) return false;
      const id = this.phaseCapabilityId(item.capability_kind, item.capability_key);
      const current = [...this.workerCapabilityDraftPhases(item)].sort();
      const baseline = [...(this.workerCapabilityBaseline?.[id] || [])].sort();
      return current.length !== baseline.length || current.some((value, index) => value !== baseline[index]);
    },

    workerCapabilityChangeCount() {
      return (this.phaseCapabilities?.items || []).filter(item => this.workerCapabilityItemChanged(item)).length;
    },

    workerCapabilityDirty() {
      return this.workerCapabilityChangeCount() > 0;
    },

    workerCapabilityItemAbnormal(item, phase = '') {
      if (!item?.enabled || !item?.ready) return true;
      if (phase) return Boolean(item.inactive_reasons?.[phase]);
      return Object.keys(item.inactive_reasons || {}).length > 0;
    },

    workerCapabilityFilteredItems(kind) {
      const query = String(this.workerCapabilitySearch || '').trim().toLowerCase();
      const filter = this.workerCapabilityFilter || 'all';
      const phase = this.workerCapabilityDrawerPhase || 'bootstrap';
      return (this.phaseCapabilities?.items || []).filter(item => {
        if (item.capability_kind !== kind) return false;
        const selected = this.workerCapabilityPhaseSelected(item, phase);
        if (filter === 'assigned' && !selected) return false;
        if (filter === 'unassigned' && selected) return false;
        if (filter === 'abnormal' && !this.workerCapabilityItemAbnormal(item, phase)) return false;
        if (!query) return true;
        return [item.title, item.capability_key, item.description, item.server_name, item.metadata?.tool_name]
          .some(value => String(value || '').toLowerCase().includes(query));
      });
    },

    workerCapabilityDraftCount(phase, kind, effective = true) {
      return (this.phaseCapabilities?.items || []).filter(item => (
        item.capability_kind === kind
        && this.workerCapabilityPhaseSelected(item, phase)
        && (!effective || !item.inactive_reasons?.[phase])
      )).length;
    },

    workerCapabilitySummaryNames(phase, kind, limit = 3) {
      const names = (this.phaseCapabilities?.items || [])
        .filter(item => (
          item.capability_kind === kind
          && this.workerCapabilityPhaseSelected(item, phase)
          && !item.inactive_reasons?.[phase]
        ))
        .map(item => item.title || item.capability_key);
      if (names.length === 0) return '暂无';
      const visible = names.slice(0, limit).join('、');
      return names.length > limit ? `${visible} 等 ${names.length} 个` : visible;
    },

    workerCapabilityMcpServerCount(phase) {
      return new Set((this.phaseCapabilities?.items || [])
        .filter(item => (
          item.capability_kind === 'mcp_tool'
          && this.workerCapabilityPhaseSelected(item, phase)
          && !item.inactive_reasons?.[phase]
        ))
        .map(item => item.server_id)
        .filter(Boolean)).size;
    },

    workerCapabilityIssueCount(phase) {
      return (this.phaseCapabilities?.items || []).filter(item => (
        this.workerCapabilityPhaseSelected(item, phase) && this.workerCapabilityItemAbnormal(item, phase)
      )).length;
    },

    workerCapabilityTabKind() {
      return ({ skill: 'skill', mcp: 'mcp_tool', extension: 'axiom_tool' })[this.workerCapabilityDrawerTab] || '';
    },

    workerCapabilityMcpGroups() {
      const groups = new Map();
      this.workerCapabilityFilteredItems('mcp_tool').forEach(item => {
        const key = item.server_id || item.server_name || '未知-server';
        if (!groups.has(key)) groups.set(key, {
          id: key,
          name: item.server_name || '未知 MCP Server',
          status: item.status || 'unknown',
          tools: [],
        });
        groups.get(key).tools.push(item);
      });
      return [...groups.values()].sort((left, right) => left.name.localeCompare(right.name, 'zh-CN'));
    },

    workerCapabilityMcpGroupPhaseState(group, phase) {
      const tools = (group?.tools || []).filter(item => this.workerCapabilitySupportsPhase(item, phase));
      if (tools.length === 0) return 'none';
      const selected = tools.filter(item => this.workerCapabilityPhaseSelected(item, phase)).length;
      if (selected === 0) return 'none';
      return selected === tools.length ? 'all' : 'some';
    },

    workerCapabilityMcpGroupPhaseCount(group, phase) {
      return (group?.tools || []).filter(item => this.workerCapabilityPhaseSelected(item, phase)).length;
    },

    toggleWorkerCapabilityMcpGroupPhase(group, phase) {
      const state = this.workerCapabilityMcpGroupPhaseState(group, phase);
      this.workerCapabilitySetItemsPhase(group?.tools || [], phase, state !== 'all');
    },

    workerCapabilitySetItemsPhase(items, phase, selected) {
      const next = { ...(this.workerCapabilityDraft || {}) };
      (items || []).forEach(item => {
        const id = this.phaseCapabilityId(item.capability_kind, item.capability_key);
        const current = [...this.workerCapabilityDraftPhases(item)];
        const has = current.includes(phase);
        if (selected && !has && !this.workerCapabilityPhaseDisabled(item, phase)) current.push(phase);
        if (!selected && has && !this.workerCapabilityPhaseLocked(item, phase)) current.splice(current.indexOf(phase), 1);
        next[id] = this.axiomPhases().filter(value => current.includes(value));
      });
      this.workerCapabilityDraft = next;
    },

    workerCapabilityBulkSet(kind, selected) {
      this.workerCapabilitySetItemsPhase(
        this.workerCapabilityFilteredItems(kind),
        this.workerCapabilityDrawerPhase,
        selected,
      );
    },

    workerCapabilityMcpExpandedState(group) {
      return Boolean(this.workerCapabilityMcpExpanded?.[group?.id]);
    },

    toggleWorkerCapabilityMcpExpanded(group) {
      if (!group?.id) return;
      this.workerCapabilityMcpExpanded = {
        ...(this.workerCapabilityMcpExpanded || {}),
        [group.id]: !this.workerCapabilityMcpExpandedState(group),
      };
    },

    workerCapabilityMatrixPayload() {
      const kinds = ['skill', 'mcp_tool', 'axiom_tool'];
      const payload = {};
      ['common', ...this.axiomPhases()].forEach(scope => {
        payload[scope] = Object.fromEntries(kinds.map(kind => [kind, []]));
      });
      (this.phaseCapabilities?.items || []).forEach(item => {
        const phases = this.workerCapabilityDraftPhases(item)
          .filter(phase => this.workerCapabilitySupportsPhase(item, phase));
        const supportsAll = this.axiomPhases()
          .every(phase => this.workerCapabilitySupportsPhase(item, phase));
        if (supportsAll && phases.length === this.axiomPhases().length) {
          payload.common[item.capability_kind].push(item.capability_key);
          return;
        }
        phases.forEach(phase => payload[phase][item.capability_kind].push(item.capability_key));
      });
      return payload;
    },

    async saveWorkerCapabilityDraft() {
      if (this.workerCapabilitySaving || !this.workerCapabilityDirty()) return;
      this.workerCapabilitySaving = true;
      try {
        const saved = await this.api('PUT', '/phase-capabilities', this.workerCapabilityMatrixPayload());
        this.applyPhaseCapabilities(saved);
        this.resetWorkerCapabilityDraft();
        this.showToast('四阶段 Worker 能力已保存，将从新 Lease 开始生效');
      } catch (error) {
        this.showToast(error?.message || '保存 Worker 能力失败', 'error');
      } finally {
        this.workerCapabilitySaving = false;
      }
    },

    cancelWorkerCapabilityDraft() {
      this.resetWorkerCapabilityDraft();
    },

    workerCapabilityDrawerRecord() {
      return (this.workerPhases || []).find(item => item.phase === this.workerCapabilityDrawerPhase) || null;
    },

    openWorkerCapabilityDrawer(phase, tab = 'overview', capabilityKey = '') {
      if (!this.axiomPhases().includes(phase)) return;
      const validTabs = ['overview', 'builtin', 'skill', 'mcp', 'extension'];
      this.selectedWorkerPhase = phase;
      this.workerCapabilityDrawerPhase = phase;
      this.workerCapabilityDrawerTab = validTabs.includes(tab) ? tab : 'overview';
      this.workerCapabilitySearch = '';
      this.workerCapabilityFilter = 'all';
      this.workerCapabilityFocusKey = capabilityKey || '';
      this.resetWorkerCapabilityDraft();
      this.workerCapabilityDrawerOpen = true;
      if (capabilityKey && this.workerCapabilityDrawerTab === 'mcp') {
        const item = this.phaseCapabilityRecord('mcp_tool', capabilityKey);
        if (item?.server_id) {
          this.workerCapabilityMcpExpanded = { ...this.workerCapabilityMcpExpanded, [item.server_id]: true };
        }
      }
      this.$nextTick?.(() => this.focusWorkerCapability());
    },

    focusWorkerCapability() {
      if (!this.workerCapabilityFocusKey || typeof document === 'undefined') return;
      const escaped = typeof CSS !== 'undefined' && typeof CSS.escape === 'function'
        ? CSS.escape(this.workerCapabilityFocusKey)
        : this.workerCapabilityFocusKey.replace(/["\\]/g, '\\$&');
      const selector = `[data-worker-capability-key="${escaped}"]`;
      const target = document.querySelector(selector);
      target?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
    },

    closeWorkerCapabilityDrawer(force = false) {
      if (!force && this.workerCapabilityDirty()) {
        const accepted = typeof window === 'undefined' || typeof window.confirm !== 'function'
          ? false
          : window.confirm('当前能力分配尚未保存，确定放弃修改吗？');
        if (!accepted) return false;
      }
      this.resetWorkerCapabilityDraft();
      this.workerCapabilityDrawerOpen = false;
      this.workerCapabilityFocusKey = '';
      return true;
    },

    async refreshAxiomWorkersPage() {
      if (this.workerCapabilityDrawerOpen && this.workerCapabilityDirty()) {
        if (!window.confirm('刷新将放弃当前未保存的能力修改，是否继续？')) return;
      }
      await this.loadAxiomWorkersPage();
      if (this.workerCapabilityDrawerOpen) this.resetWorkerCapabilityDraft();
    },

    applyWorkerRouteParams(params) {
      const mode = params?.get('mode');
      const phase = params?.get('phase');
      const tab = params?.get('tab');
      const capability = params?.get('capability') || '';
      if (mode === 'config' || mode === 'manage') this.workerPageMode = mode;
      if (this.axiomPhases().includes(phase)) this.selectedWorkerPhase = phase;
      if (this.workerPageMode === 'manage' && this.axiomPhases().includes(phase)) {
        this.workerCapabilityRouteTarget = { phase, tab: tab || 'overview', capability };
      } else {
        this.workerCapabilityRouteTarget = null;
        this.workerCapabilityDrawerOpen = false;
        this.workerCapabilityFocusKey = '';
      }
    },

    consumeWorkerCapabilityRouteTarget() {
      const target = this.workerCapabilityRouteTarget;
      if (!target || this.workerPageMode !== 'manage') return;
      this.workerCapabilityRouteTarget = null;
      this.openWorkerCapabilityDrawer(target.phase, target.tab, target.capability);
    },

    capabilityPreferredPhase(item) {
      const phases = this.capabilityAssignedPhases(item);
      if (phases.includes(this.selectedWorkerPhase)) return this.selectedWorkerPhase;
      return phases[0] || 'reason';
    },

    navigateToWorkerCapability(kind, key, phase = '') {
      const item = this.phaseCapabilityRecord(kind, key);
      const selectedPhase = this.axiomPhases().includes(phase)
        ? phase
        : this.capabilityPreferredPhase(item);
      const tab = ({ skill: 'skill', mcp_tool: 'mcp', axiom_tool: 'extension' })[kind] || 'overview';
      const params = new URLSearchParams({ mode: 'manage', phase: selectedPhase, tab });
      if (key) params.set('capability', key);
      const target = `#/workers?${params.toString()}`;
      if (typeof location !== 'undefined' && location.hash === target) this.handleRoute?.();
      else if (typeof location !== 'undefined') location.hash = target;
    },

    async loadWorkerPhases() {
      this.workerPhasesLoading = true;
      try {
        const items = await this.api('GET', '/worker-phases', null, {
          signal: this._pageAbortController?.signal,
        });
        this.workerPhases = Array.isArray(items) ? items : [];
        const drafts = { ...this.workerPhaseBackendDraft };
        this.workerPhases.forEach(item => {
          drafts[item.phase] = (item.backends || []).map(backend => backend.name);
        });
        this.workerPhaseBackendDraft = drafts;
        if (!this.workerPhases.some(item => item.phase === this.selectedWorkerPhase)) {
          this.selectedWorkerPhase = this.workerPhases[0]?.phase || 'bootstrap';
        }
        return this.workerPhases;
      } catch (error) {
        if (error?.name !== 'AbortError') this.showToast(error?.message || '加载四阶段 Worker 失败', 'error');
        return this.workerPhases;
      } finally {
        this.workerPhasesLoading = false;
      }
    },

    selectedWorkerPhaseRecord() {
      return (this.workerPhases || []).find(item => item.phase === this.selectedWorkerPhase) || null;
    },

    selectWorkerPhase(phase) {
      if (!this.axiomPhases().includes(phase)) return;
      this.selectedWorkerPhase = phase;
    },

    workerPhaseBackendSelected(phase, name) {
      return Array.isArray(this.workerPhaseBackendDraft?.[phase])
        && this.workerPhaseBackendDraft[phase].includes(name);
    },

    toggleWorkerPhaseBackend(phase, name) {
      const current = [...(this.workerPhaseBackendDraft?.[phase] || [])];
      const index = current.indexOf(name);
      if (index >= 0) current.splice(index, 1);
      else current.push(name);
      this.workerPhaseBackendDraft = { ...this.workerPhaseBackendDraft, [phase]: current };
    },

    async saveWorkerPhaseBackends(phase) {
      if (this.workerPhaseBackendsSaving?.[phase]) return;
      this.workerPhaseBackendsSaving = { ...this.workerPhaseBackendsSaving, [phase]: true };
      try {
        await this.api('PUT', `/worker-phases/${encodeURIComponent(phase)}/backends`, {
          backends: this.workerPhaseBackendDraft?.[phase] || [],
        });
        await Promise.all([
          this.loadWorkerPhases(),
          this.loadAxiomWorkersOverview({ preserveSelection: true }),
          this.loadDispatchSettings?.(),
        ]);
        this.showToast(`${this.axiomPhaseLabel(phase)} 后端池已保存`);
      } catch (error) {
        this.showToast(error?.message || '保存阶段后端池失败', 'error');
        await this.loadWorkerPhases();
      } finally {
        this.workerPhaseBackendsSaving = { ...this.workerPhaseBackendsSaving, [phase]: false };
      }
    },

    selectedAxiomToolRecord() {
      return (this.axiomToolsCatalog || []).find(tool => tool.key === this.selectedAxiomTool) || null;
    },

    selectAxiomTool(tool) {
      const key = typeof tool === 'string' ? tool : tool?.key;
      if (!key) return;
      this.selectedAxiomTool = key;
      const item = this.phaseCapabilityRecord('axiom_tool', key);
      if (item) this.phaseCapabilityDraftScopes(item);
    },

    axiomToolCapabilityRecord(tool) {
      return tool ? this.phaseCapabilityRecord('axiom_tool', tool.key) : null;
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
        await this.loadPhaseCapabilities();
        this.showToast(updated.enabled ? `扩展 Tool ${updated.key} 已启用` : `扩展 Tool ${updated.key} 已停用`);
      } catch (error) {
        this.showToast(error?.message || '更新工具状态失败', 'error');
      }
    },

    async scanAxiomTools({ silent = false } = {}) {
      if (this.axiomToolsScanLoading) return;
      this.axiomToolsScanLoading = true;
      try {
        const result = await this.api('POST', '/axiom-tools/scan');
        this.axiomToolsCatalog = result.items || [];
        if (!this.axiomToolsCatalog.some(item => item.key === this.selectedAxiomTool)) {
          this.selectedAxiomTool = this.axiomToolsCatalog[0]?.key || '';
        }
        await this.loadPhaseCapabilities();
        if (!silent) this.showToast(`扫描完成：${result.ready} 个扩展 Tool 就绪`);
      } catch (error) {
        this.showToast(error?.message || '扫描扩展 Tool 失败', 'error');
      } finally {
        this.axiomToolsScanLoading = false;
      }
    },

    async loadAxiomToolsCatalog({ preserveSelection = true } = {}) {
      this.axiomToolsLoading = true;
      try {
        const items = await this.api('GET', '/axiom-tools', null, {
          signal: this._pageAbortController?.signal,
        });
        this.axiomToolsCatalog = Array.isArray(items) ? items : [];
        if (!preserveSelection || !this.axiomToolsCatalog.some(item => item.key === this.selectedAxiomTool)) {
          this.selectedAxiomTool = this.axiomToolsCatalog[0]?.key || '';
        }
      } catch (error) {
        if (error?.name !== 'AbortError') this.showToast(error?.message || '加载扩展 Tool 失败', 'error');
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
        return this.axiomWorkers;
      } catch (error) {
        if (error?.name !== 'AbortError') this.showToast(error?.message || '加载模型执行后端失败', 'error');
        return this.axiomWorkers;
      } finally {
        this.axiomWorkersLoading = false;
      }
    },

    async selectAxiomWorker(worker) {
      const name = typeof worker === 'string' ? worker : worker?.name;
      if (!name) return;
      this.selectedAxiomWorker = name;
      this.editAxiomWorker(name);
    },

    selectedAxiomWorkerRecord() {
      return (this.axiomWorkers || []).find(worker => worker.name === this.selectedAxiomWorker) || null;
    },

    editAxiomWorker(name) {
      const source = (this.dispatchSettings?.workers || []).find(worker => (
        (worker.persisted_name || worker.name) === name
      ));
      this.axiomWorkerEditor = source ? JSON.parse(JSON.stringify(source)) : null;
    },

    newAxiomWorker() {
      const phase = this.selectedWorkerPhase || 'explore';
      this.axiomWorkerEditor = {
        _draftId: `axiom-worker-${Date.now()}`,
        persisted_name: '',
        name: `pi-worker-${(this.axiomWorkers || []).length + 1}`,
        task_types: [phase],
        phase_bootstrap: phase === 'bootstrap',
        phase_reason: phase === 'reason',
        phase_explore: phase === 'explore',
        phase_review: phase === 'review',
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
        await Promise.all([
          this.loadAxiomWorkersOverview({ preserveSelection: false }),
          this.loadWorkerPhases(),
        ]);
        this.selectedAxiomWorker = worker.name;
        this.editAxiomWorker(worker.name);
        this.showToast(`模型执行后端 ${worker.name} 已保存`);
      } catch (error) {
        this.showToast(error?.message || '保存模型执行后端失败', 'error');
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
      if (!window.confirm(`确认删除模型执行后端 ${worker.persisted_name}？`)) return;
      try {
        await this.api('DELETE', `/settings/dispatch/workers/${encodeURIComponent(worker.persisted_name)}`);
        this.axiomWorkerEditor = null;
        await Promise.all([
          this.loadDispatchSettings(),
          this.loadAxiomWorkersOverview({ preserveSelection: false }),
          this.loadWorkerPhases(),
        ]);
        this.showToast(`模型执行后端 ${worker.persisted_name} 已删除`);
      } catch (error) {
        this.showToast(error?.message || '删除模型执行后端失败', 'error');
      }
    },

    async loadAxiomWorkersPage() {
      await Promise.all([
        this.loadDispatchSettings?.(),
        this.loadAxiomWorkersOverview({ preserveSelection: true }),
        this.loadWorkerPhases(),
        this.loadPhaseCapabilities(),
      ]);
      if (this.selectedAxiomWorker) this.editAxiomWorker(this.selectedAxiomWorker);
      this.consumeWorkerCapabilityRouteTarget();
    },

    async loadAxiomToolsPage() {
      await Promise.all([
        this.scanAxiomTools({ silent: true }),
        this.loadPhaseCapabilities(),
      ]);
    },
  },
};
