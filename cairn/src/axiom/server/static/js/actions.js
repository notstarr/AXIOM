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

    openHintModal() {
      if (!this.projectCanWriteHints()) return;
      this.hintForm = { content:'' };
      this.showHintModal = true;
    },

    async addHint() {
      if (!this.projectCanWriteHints()) return;
      try {
        const actor = this.actorName();
        await this.api('POST', `/projects/${this.selectedProjectId}/hints`, { content: this.hintForm.content, creator: actor });
        this.showHintModal = false;
        this.hintForm = { content:'' };
        await this.loadProjects();
        await this.loadProject(this.selectedProjectId);
        this.sideTab = 'hints';
        this.showToast('提示已添加');
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
        this.showToast('黑板已保存');
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    async deleteBlackboardItem(itemId) {
      const gid = this.activeBlackboardGroupId();
      if (!gid) return;
      try {
        await this.api('DELETE', `/project-groups/${gid}/blackboard/${itemId}`);
        await this.reloadBlackboardGroup(gid);
        this.showToast('黑板已删除');
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
