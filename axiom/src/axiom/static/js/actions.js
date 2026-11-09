window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.actions = {
  methods: {
    async createIntent(claim = false) {
      try {
        const actor = this.actorName();
        // 解析用户填写的 targets（支持逗号/分号/换行/空格分隔的 host/ip）
        let targets = [];
        const raw = (this.intentForm.targets || '').trim();
        if (raw) {
          targets = raw.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
        }
        // 用户未填时，回退到当前任务的白名单。
        if (targets.length === 0) {
          const scope = this.project?.project?.task_scope || {};
          const allow = scope.targets || [];
          if (allow.length > 0) targets = allow.slice();
        }
        await this.api('POST', `/projects/${this.selectedProjectId}/intents`, {
          from: this.selectedFacts,
          description: this.intentForm.description,
          creator: actor,
          worker: claim ? actor : null,
          targets,
        });
        this.showIntentModal = false;
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast(claim ? '意图已声明并认领' : '意图已声明');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async sendHeartbeatForSelectedIntent() {
      const intent = this.selectedActionableOpenIntentRecord();
      if (!intent) return;
      try {
        const actor = this.actorName();
        const claiming = !intent.worker;
        await this.api('POST', `/projects/${this.selectedProjectId}/intents/${intent.id}/heartbeat`, { worker: actor });
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast(claiming ? '意图已认领' : '心跳已发送');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async releaseSelectedIntent() {
      const intent = this.selectedReleasableOpenIntentRecord();
      if (!intent) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/intents/${intent.id}/release`, { worker: actor });
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('意图已释放');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openConclude(intent) { this.concludeForm = { description:'', intentId: intent.id }; this.showConcludeModal = true; },

    async concludeIntent() {
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/intents/${this.concludeForm.intentId}/conclude`, { worker: actor, description: this.concludeForm.description });
        this.showConcludeModal = false;
        this.selectedNode = null;
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('意图已结束');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openCompleteProject() {
      if (!this.canActOnSelectedFacts()) return;
      this.completeForm = { description:'' };
      this.showCompleteModal = true;
    },

    async completeProject() {
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/complete`, { from: this.selectedFacts, description: this.completeForm.description, worker: actor });
        this.showCompleteModal = false;
        await this.loadProjects();
        await this.loadProjectGroups();
        await this.loadProject(this.selectedProjectId);
        this.updateGraph();
        this.showToast('任务已完成');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    directiveScopeLabel(scope) {
      return ({
        reason_all: '全体 Reason · 常驻',
        reason_next: '下一次 Reason · 一次',
        explore_all: '全体 Explore · 常驻',
        explore_one: '指定 Explore · 一次',
        all_workers: '全部 Worker · 常驻',
      }[scope] || scope || '未知作用域');
    },
    // 图谱侧栏：选了预设就把模板填进输入框（落库的仍是填完的文本）
    applyDirectivePreset() {
      const preset = this.presetsForScope(this.newDirective.scope)
        .find(item => item.key === this.newDirective.preset);
      if (!preset) return;
      this.newDirective.content = this.presetPlaceholderText(preset);
    },
    directiveStatusLabel(item) {
      if (!item) return '';
      if (item.status === 'disabled' || item.archived) return '已停用';
      if (item.status === 'pending') return '待投递';
      if (item.status === 'delivered') return '已投递';
      return '生效中';
    },
    // 指令的下发/停用只改这一块，没必要整套重拉详情（还会被 hydration 锁挡住）。
    async refreshDirectives() {
      if (!this.selectedProjectId) return;
      try {
        const items = await this.api('GET', `/tasks/${this.selectedProjectId}/directives`);
        if (this.project) this.project = { ...this.project, directives: items || [] };
      } catch (e) { /* 列表刷新失败不该把已经成功的下发判成失败 */ }
    },
    // 概览里的「下发 / 管理」：切到图谱并直接打开指令侧栏。
    async openTaskDirectivePanel() {
      await this.switchTaskView('graph');
      this.sidePanelCollapsed = false;
      this.sideTab = 'directives';
      await this.loadEligibleExplores();
      await this.loadDirectivePresets();
    },
    async loadEligibleExplores() {
      // 打开指令面板时顺手把预设取回来，否则下拉是空的
      void this.loadDirectivePresets();
      try {
        const items = await this.api('GET', `/projects/${this.selectedProjectId}/directives/eligible-explores`);
        this.eligibleExplores = Array.isArray(items) ? items : [];
      } catch (e) {
        this.eligibleExplores = [];
      }
    },
    async addDirective() {
      if (this.directiveSubmitting) return;
      const content = (this.newDirective.content || '').trim();
      if (!content) return;
      const scope = this.newDirective.scope;
      if (scope === 'explore_one' && !this.newDirective.target_intent_id) {
        this.showToast('请先选择一个尚未领取的 Explore', 'error');
        return;
      }
      this.directiveSubmitting = true;
      try {
        await this.api('POST', `/projects/${this.selectedProjectId}/directives`, {
          content,
          creator: this.actorName(),
          scope,
          target_intent_id: scope === 'explore_one' ? this.newDirective.target_intent_id : null,
        });
        this.newDirective = { scope, content: '', target_intent_id: '' };
        await this.refreshDirectives();
        this.showToast('指令已下发');
      } catch (e) { this.showToast(e.message, 'error'); }
      finally { this.directiveSubmitting = false; }
    },
    async toggleDirective(item) {
      if (!item) return;
      const disabled = item.status === 'disabled' || item.archived;
      try {
        await this.api('PATCH', `/projects/${this.selectedProjectId}/directives/${item.id}`, { disabled: !disabled });
        await this.refreshDirectives();
        this.showToast(disabled ? '指令已启用' : '指令已停用');
      } catch (e) { this.showToast(e.message, 'error'); }
    },
    openHintModal() {
      if (!this.projectCanWriteHints()) return;
      this.hintForm = { content:'' };
      this.showHintModal = true;
    },

    async addHint() {
      if (!this.projectCanWriteHints()) return;
      try {
        const actor = this.actorName();
        const hint = await this.api('POST', `/projects/${this.selectedProjectId}/hints`, { content: this.hintForm.content, creator: actor });
        this.showHintModal = false;
        this.hintForm = { content:'' };
        await this.loadProjects();
        await this.loadProject(this.selectedProjectId);
        this.sideTab = 'hints';
        const reason = this.project?.project?.reason;
        const triggeredByHint = reason?.trigger === 'hint'
          && reason?.trigger_payload?.hint_id === hint?.id;
        this.showToast(triggeredByHint
          ? `提示已添加，已触发 ${reason.id}`
          : '提示已添加');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openBlackboardEditor(item, forManaged = false) {
      this.blackboardEditorForManaged = forManaged;
      if (item) {
        this.blackboardEditor = { open: true, id: item.id, title: item.title || '', content: item.content || '', hint: item.hint || '' };
      } else {
        this.blackboardEditor = { open: true, id: '', title: '', content: '', hint: '' };
      }
    },

    async saveBlackboardItem() {
      const gid = this.activeBlackboardGroupId();
      if (!gid) return;
      const actor = this.actorName();
      const body = {
        title: this.blackboardEditor.title,
        content: this.blackboardEditor.content,
        hint: this.blackboardEditor.hint || '',
        category: 'general',
        status: 'open',
        priority: 'medium',
      };
      try {
        if (this.blackboardEditor.id) {
          await this.api('PUT', `/project-groups/${gid}/blackboard/${this.blackboardEditor.id}`, { ...body, updated_by: actor });
        } else {
          await this.api('POST', `/project-groups/${gid}/blackboard`, { ...body, creator: actor });
        }
        this.blackboardEditor = { open: false, id: '', title: '', content: '', hint: '' };
        await this.reloadBlackboardGroup(gid);
        this.showToast('共享知识已保存');
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    async deleteBlackboardItem(itemId) {
      const gid = this.activeBlackboardGroupId();
      if (!gid) return;
      try {
        await this.api('DELETE', `/project-groups/${gid}/blackboard/${itemId}`);
        await this.reloadBlackboardGroup(gid);
        this.showToast('共享知识已删除');
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    activeBlackboardGroupId() {
      if (this.activePage === 'projects') return this.selectedProjectManagementId;
      return this.project?.project?.parent_project_id || '';
    },

    async reloadBlackboardGroup(groupId) {
      if (!groupId) return;
      const items = await this.api('GET', `/project-groups/${groupId}/blackboard`);
      this._groupBlackboardCache = this._groupBlackboardCache || {};
      this._groupBlackboardCache[groupId] = items;
      const group = (this.projectGroups || []).find(item => item.id === groupId);
      if (group) group.blackboard_count = items.length;
    },

    groupBlackboardItems() {
      if (!this._groupBlackboardCache) this._groupBlackboardCache = {};
      const gid = this.activePage === 'projects' ? this.selectedProjectManagementId : (this.project?.project?.parent_project_id || '');
      if (!gid) return [];
      return this._groupBlackboardCache[gid] || [];
    },

    async loadMaintainerStatus(id) {
      try {
        const resp = await this.api('GET', `/projects/${id}/maintainer-status`);
        this.maintainingIntentIds = resp.maintaining_intent_ids || [];
      } catch { /* ignore */ }
    },

    isMaintainerRunning(factId) {
      if (!factId || this.maintainingIntentIds.length === 0) return false;
      const intent = this.project?.intents?.find(i => i.to === factId);
      return intent && this.maintainingIntentIds.includes(intent.id);
    },
  }
};
