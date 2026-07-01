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
      try {
        this.projects = await this.api('GET', '/projects');
        if (!this.selectedTaskManagementId && this.projects.length > 0) {
          this.selectedTaskManagementId = this.projects[0].id;
        }
        if (!this.selectedProjectManagementTaskId && this.projects.length > 0) {
          this.selectedProjectManagementTaskId = this.tasksForGroup(this.selectedProjectManagementId)[0]?.id || '';
        }
      } catch(e) { console.error(e); }
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
        if (this.selectedProjectManagementId) this.syncProjectGroupSettingsForm();
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

    async refreshDashboardPage() {
      await this.loadProjectGroups();
      await this.loadProjects();
      await this.loadAllProjectDetails(true);
      await this.loadGlobalSkillUsage();
      this.showToast('仪表盘已刷新');
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
      const details = Object.values(this.projectDetailsCache || {});
      if (this.project && this.selectedProjectId) {
        const withoutSelected = details.filter(item => item?.project?.id !== this.selectedProjectId);
        return [this.project, ...withoutSelected];
      }
      return details;
    },

    selectedTaskSummary() {
      const tasks = this.projects || [];
      if (!this.selectedTaskManagementId && tasks.length > 0) this.selectedTaskManagementId = tasks[0].id;
      return tasks.find(task => task.id === this.selectedTaskManagementId) || tasks[0] || null;
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
        this.loadMaintainerStatus(id);
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async createProject() {
      try {
        if (this.projectGroups.length === 0) await this.loadProjectGroups();
        const parentProjectId = this.newProject.parent_project_id || this.projectGroups[0]?.id || '';
        if (!parentProjectId) {
          this.showToast('请先创建项目，再创建任务', 'error');
          return;
        }

        let origin = this.newProject.origin;
        if (this.newProject.depth_mode && this.newProject.depthInputMode === 'crawler' && this.newProject.crawlerTaskId) {
          try {
            const resp = await this.api('GET', `/crawler/tasks/${this.newProject.crawlerTaskId}/export`);
            origin = resp.yaml || '';
          } catch(e) {
            this.showToast('获取爬虫数据失败: ' + (e.message || e), 'error');
            return;
          }
          if (!origin) {
            this.showToast('该爬虫任务暂无数据，请先运行爬虫', 'error');
            return;
          }
        }

        const actor = this.actorName();
        const body = {
          parent_project_id: parentProjectId,
          title: this.newProject.title,
          origin: origin,
          goal: this.newProject.depth_mode ? '深度模式' : this.newProject.goal,
          bootstrap_enabled: this.newProject.bootstrap && !this.newProject.depth_mode,
          depth_mode: !!this.newProject.depth_mode,
          blackboard: this.newProject.blackboard || '',
        };
        const data = await this.api('POST', '/projects', body);
        const pid = data.project.id;
        // Upload HAR to project human/ folder and set origin to full path
        if (this._harFile) {
          try {
            const form = new FormData();
            form.append('file', this._harFile);
            const uploadResp = await fetch(`/files/${pid}/upload?set_origin=true`, { method: 'POST', body: form });
            if (!uploadResp.ok) {
              this.showToast('HAR 文件上传失败，请手动上传', 'error');
            } else {
              await this.loadProjects();
              const updated = this.projects.find(p => p.id === pid);
              if (updated) this.openProject(pid);
            }
          } catch(e) {
            this.showToast('HAR 文件上传失败: ' + (e.message || e), 'error');
          }
          this._harFile = null;
        }
        this.showTaskComposer = false;
        this.newProject = {
          parent_project_id: parentProjectId || this.projectGroups[0]?.id || '',
          title:'', origin:'', goal:'',
          bootstrap: true, depth_mode: false, blackboard: '',
          depthInputMode: 'har', crawlerTaskId: '',
        };
        await this.loadProjectGroups();
        await this.loadProjects();
        await this.openProject(pid);
        this.showToast('任务已创建');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openCreateIntent() {
      if (!this.canActOnSelectedFacts()) return;
      this.intentForm = { description:'' };
      this.showIntentModal = true;
    },

    selectManagedProject(projectId) {
      this.selectedProjectManagementId = projectId;
      this.syncProjectGroupSettingsForm();
      this.reloadBlackboardGroup(projectId);
      const firstTask = this.tasksForGroup(projectId)[0];
      this.selectedProjectManagementTaskId = firstTask?.id || '';
      if (this.selectedProjectManagementTaskId) this.loadProjectDetailForManagement(this.selectedProjectManagementTaskId);
    },

    syncProjectGroupSettingsForm() {
      const group = this.selectedProjectGroup();
      if (!group) return;
      const scope = group.scope || {};
      const targets = scope.targets || [];
      const exclude = scope.exclude || [];
      const scopeMode = targets.length > 0 ? 'whitelist' : (exclude.length > 0 ? 'blacklist' : 'none');
      this.projectGroupSettingsForm = {
        title: group.title || '',
        description: group.description || '',
        scope_mode: scopeMode,
        targets_text: targets.join('\n'),
        exclude_text: exclude.join('\n'),
        notes: scope.notes || '',
        status: group.status || 'active',
      };
    },

    cycleProjectGroupScopeMode() {
      const order = ['none', 'whitelist', 'blacklist'];
      const current = this.projectGroupSettingsForm.scope_mode || 'none';
      const idx = order.indexOf(current);
      this.projectGroupSettingsForm.scope_mode = order[(idx + 1) % order.length];
    },

    projectGroupScopeModeLabel(mode) {
      if (mode === 'whitelist') return '白名单';
      if (mode === 'blacklist') return '黑名单';
      return '不开启';
    },

    async saveProjectGroupSettings() {
      const group = this.selectedProjectGroup();
      if (!group) return;
      try {
        const mode = this.projectGroupSettingsForm.scope_mode || 'none';
        const targetLines = this.linesFromText(this.projectGroupSettingsForm.targets_text);
        const excludeLines = this.linesFromText(this.projectGroupSettingsForm.exclude_text);
        const targets = mode === 'whitelist' ? targetLines : [];
        const exclude = mode === 'blacklist' ? excludeLines : [];
        if (mode === 'whitelist' && targets.length === 0) {
          this.showToast('白名单模式至少需要 1 个目标', 'error');
          return;
        }
        if (mode === 'blacklist' && exclude.length === 0) {
          this.showToast('黑名单模式至少需要 1 个目标', 'error');
          return;
        }
        const updated = await this.api('PUT', `/project-groups/${group.id}`, {
          title: this.projectGroupSettingsForm.title,
          description: this.projectGroupSettingsForm.description,
          status: this.projectGroupSettingsForm.status,
          scope: {
            targets,
            exclude,
            notes: this.projectGroupSettingsForm.notes,
          },
        });
        const idx = this.projectGroups.findIndex(item => item.id === updated.id);
        if (idx >= 0) this.projectGroups.splice(idx, 1, updated);
        this.syncProjectGroupSettingsForm();
        this.showToast('项目设置已保存');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
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
        await this.api('DELETE', `/projects/${projectId}`);
        this.closeDeleteModal(true);
        if (this.selectedProjectId === projectId && this.view === 'graph') {
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

    async openProject(id, fromRoute = false) {
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
      this.view = 'graph';
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
        this.initGraph();
      });
    },

    backToList(fromRoute) {
      this.closeActionReplay();
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
      this.pollTimer = setInterval(async () => {
        if (!this.polling) return;
        if (this.activePage === 'action-replay' && this.actionReplayProjectId) {
          await Promise.all([this.loadActionReplayEvents(false), this.loadDispatchSettings()]);
        }
        if (this.selectedProjectId && this.view === 'graph' && this.activePage === 'task-detail') {
          await Promise.all([
            this.loadProject(this.selectedProjectId),
            this.loadSeeEvents(false),
            this.loadDispatchSettings(),
          ]);
          this.updateGraph();
        } else {
          await this.loadProjects();
          if (['dashboard', 'projects', 'vulnerabilities'].includes(this.activePage)) {
            await this.loadAllProjectDetails();
          }
        }
        // Auto-poll crawler status on crawler management page
        if (this.activePage === 'crawler-results' && this.selectedCrawlerTaskId) {
          if (this.crawlerStatus && (this.crawlerStatus.status === 'running' || this.crawlerStatus.status === 'stopping')) {
            const wasRunning = this.crawlerStatus.status === 'running';
            await this.loadCrawlerStatus();
            if (wasRunning && this.crawlerStatus.status === 'done') {
              this.pushNotification({ type: 'success', title: '爬虫完成', message: this.crawlerStatus.target_url });
              await this.loadCrawlerResultsPage();
              await this.loadCrawlerTasks();
            } else if (wasRunning && this.crawlerStatus.status === 'failed') {
              this.pushNotification({ type: 'error', title: '爬虫失败', message: this.crawlerStatus.target_url });
              await this.loadCrawlerTasks();
            }
          } else {
            await this.loadCrawlerStatus();
          }
          await this.loadCrawlerResultsPage();
        }
      }, 5000);
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
      this.editCredForm = { group_id: '', label: '', auth_type: 'cookie', credentials_json: '{}', domains_text: '', source_task_id: '', expires_at: '', asset_id: '' };
      this.showCredAdd = true;
    },
    async addCredential() {
      try {
        const body = { ...this.editCredForm, domains_json: JSON.stringify(this._parseDomainText(this.editCredForm.domains_text)) };
        delete body.domains_text;
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
      this.editCredForm = { label: cred.label, auth_type: cred.auth_type, credentials_json: cred.credentials_json, domains_text: this.parseDomains(cred.domains_json).join(', '), expires_at: cred.expires_at || '', asset_id: cred.asset_id || '' };
      this.showCredEdit = true;
    },
    async saveCredential() {
      if (!this.editCredId) return;
      try {
        const body = { ...this.editCredForm, domains_json: JSON.stringify(this._parseDomainText(this.editCredForm.domains_text)) };
        delete body.domains_text;
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
    toggleCredActive(cred) {
      this.api('PUT', `/credentials/${cred.id}`, { active: cred.active !== 1 }).then(() => this.loadCredentials());
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
      } catch(e) { console.error(e); }
    },
  }
};
