window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.core = {
  methods: {
    _templateCache: {},
    _loadedTemplates: {},
    async loadTemplate(name) {
      if (this._templateCache[name]) return this._templateCache[name];
      try {
        const resp = await fetch('/static/templates/' + name + '.html', { cache: 'no-store' });
        if (!resp.ok) throw new Error('Template ' + name + ' not found');
        const html = await resp.text();
        this._templateCache[name] = html;
        return html;
      } catch (e) {
        console.error('Failed to load template:', name, e);
        return '<div class="p-4 text-red-500">Template load failed: ' + name + '</div>';
      }
    },
    async loadPageTemplate(name) {
      if (this._loadedTemplates[name]) return;
      this._loadedTemplates[name] = true;
      const html = await this.loadTemplate(name);
      const container = document.querySelector('[data-page="' + name + '"]');
      if (!container) {
        console.error('No container for page:', name);
        return;
      }
      container.innerHTML = html;
      this.$nextTick(() => {
        if (window.Alpine) window.Alpine.initTree(container);
      });
    },

    async init() {
      this._panelResizeMove = (e) => this.onPanelResize(e);
      this._panelResizeStop = () => this.stopPanelResize();
      window.addEventListener('pointermove', this._panelResizeMove);
      window.addEventListener('pointerup', this._panelResizeStop);
      this.loadLocalPrefs();
      this.initDarkMode();
      this.loadPrompts();
      await this.loadProjectGroups();
      await this.loadProjects();
      await this.loadSettings();
      this.startPolling();
      window.addEventListener('hashchange', () => this.handleRoute());
      window.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); this.showGlobalSearch = true; }
        if (e.key === 'Escape') { this.showGlobalSearch = false; if (this.errorDetail) this.errorDetail = null; }
      });
      this.handleRoute();
    },

    initDarkMode() {
      const saved = localStorage.getItem('axiom-dark-mode');
      if (saved === 'true' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
        this.darkMode = true;
        document.documentElement.classList.add('dark');
      }
    },

    toggleDarkMode() {
      this.darkMode = !this.darkMode;
      localStorage.setItem('axiom-dark-mode', this.darkMode);
      if (this.darkMode) document.documentElement.classList.add('dark');
      else document.documentElement.classList.remove('dark');
    },

    globalSearchResults() {
      if (!this.globalSearchQuery.trim()) return [];
      const q = this.globalSearchQuery.toLowerCase();
      const results = [];
      for (const p of this.projects || []) {
        const title = (p.title || '').toLowerCase();
        const id = (p.id || '').toLowerCase();
        if (title.includes(q) || id.includes(q)) {
          results.push({type: 'project', id: p.id, title: p.title || p.id, subtitle: p.id, action: () => { this.navigateTo('tasks'); this.openProject(p.id, true); }});
        }
      }
      for (const g of this.projectGroups || []) {
        if ((g.title || '').toLowerCase().includes(q)) {
          results.push({type: 'group', id: g.id, title: g.title, subtitle: g.id, action: () => { this.navigateTo('projects'); this.selectManagedProject(g.id); }});
        }
      }
      for (const v of this.allVulnerabilities()) {
        if ((v.title || '').toLowerCase().includes(q)) {
          results.push({type: 'vuln', id: v.id, title: v.title, subtitle: v.project_id, action: () => { this.navigateTo('vulnerabilities'); }});
        }
      }
      return results;
    },

    pushNotification(notif) {
      notif.id = Date.now() + Math.random();
      notif.time = new Date().toLocaleTimeString();
      this.notifications.unshift(notif);
      if (this.notifications.length > 20) this.notifications.pop();
    },

    showErrorDetail(error) {
      this.errorDetail = { message: error.message || String(error), stack: error.stack || '', time: new Date().toISOString() };
    },

    dismissNotification(id) {
      this.notifications = this.notifications.filter(n => n.id !== id);
    },

    handleRoute() {
      const hash = location.hash || '#/';
      const taskMatch = hash.match(/^#\/tasks\/(.+)$/);
      const legacyProjectMatch = hash.match(/^#\/projects\/(.+)$/);
      if (taskMatch || legacyProjectMatch) {
        const id = decodeURIComponent((taskMatch || legacyProjectMatch)[1]);
        this.activePage = 'task-detail';
        if (this.selectedProjectId !== id || !this.project) this.openTaskOverview(id, true);
        return;
      }
      const page = (hash.replace(/^#\/?/, '') || 'dashboard').split('/')[0];
      const allowed = ['dashboard', 'tasks', 'vulnerabilities', 'projects', 'guard-audit', 'skills', 'files', 'mcp', 'settings', 'prompts'];
      const newPage = allowed.includes(page) ? page : 'dashboard';
      if (this.activePage !== newPage && !this._navigatingBack) {
        this.pageHistory.push(this.activePage);
      }
      this._navigatingBack = false;
      this.activePage = newPage;
      if (this.activePage !== 'task-detail' && this.view !== 'list') this.backToList(true);
      if (this.activePage === 'vulnerabilities' || this.activePage === 'projects' || this.activePage === 'dashboard') {
        this.loadAllProjectDetails();
      }
      if (this.activePage === 'dashboard') this.loadGlobalSkillUsage();
      if (this.activePage === 'projects' && this.selectedProjectManagementId) {
        this.loadProjectCredentials(this.selectedProjectManagementId);
      }
      if (this.activePage === 'skills') {
        this.loadSkills();
      }
      if (this.activePage === 'settings') this.loadSettings();
      if (this.activePage === 'tasks') this.loadDispatchSettings();
      if (this.activePage === 'guard-audit') this.loadGuardAuditEvents();
      if (this.activePage === 'files') this.loadArtifactCenter();
      if (this.activePage === 'mcp') this.loadMcpPage();
    },

    navigateTo(page) {
      const routes = {
        dashboard: '#/dashboard',
        tasks: '#/tasks',
        vulnerabilities: '#/vulnerabilities',
        projects: '#/projects',
        'guard-audit': '#/guard-audit',
        skills: '#/skills',
        files: '#/files',
        mcp: '#/mcp',
        settings: '#/settings',
        prompts: '#/prompts',
      };
      location.hash = routes[page] || '#/dashboard';
      this.handleRoute();
    },

    goBack() {
      if (this.pageHistory.length === 0) return;
      const prev = this.pageHistory.pop();
      this._navigatingBack = true;
      const routes = {
        dashboard: '#/dashboard', tasks: '#/tasks', vulnerabilities: '#/vulnerabilities',
        projects: '#/projects', 'guard-audit': '#/guard-audit', skills: '#/skills', files: '#/files', mcp: '#/mcp',
        settings: '#/settings',
        prompts: '#/prompts', 'task-detail': '#/task-detail',
      };
      location.hash = routes[prev] || '#/dashboard';
      this.handleRoute();
    },

    async api(method, path, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json' } };
      if (body) opts.body = JSON.stringify(body);
      const r = await fetch(path, opts);
      if (r.status === 204) return null;
      const text = await r.text();
      let data = null;
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (e) {
          data = null;
        }
      }
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        if (data && typeof data.detail === 'string') msg = data.detail;
        else if (data && Array.isArray(data.detail)) msg = data.detail.map(e => e.msg).join('; ');
        else if (text) msg = text;
        const err = new Error(msg);
        err.status = r.status;
        err.path = path;
        throw err;
      }
      if (data === null) return text || {};
      return data;
    },

    async fetchText(path) {
      const r = await fetch(path);
      const text = await r.text();
      if (!r.ok) {
        let detail = text;
        try {
          const data = JSON.parse(text);
          detail = data.detail || text;
        } catch {}
        throw new Error(detail || `HTTP ${r.status}`);
      }
      return text;
    },

    showToast(msg, type = 'info') {
      this.toast = { show: true, message: msg, type };
      setTimeout(() => this.toast.show = false, 3000);
    },

    async refreshCurrentPage(options = {}) {
      if (this.refreshLoading) return;
      const { silent = false } = options;
      this.refreshLoading = true;
      try {
        if (this.activePage === 'dashboard') {
          await this.refreshDashboardPage({ silent: true });
        } else if (this.activePage === 'tasks') {
          await Promise.all([this.loadProjectGroups(), this.loadProjects(), this.loadDispatchSettings()]);
        } else if (this.activePage === 'task-detail' && this.selectedProjectId) {
          await Promise.all([this.loadProject(this.selectedProjectId), this.loadSeeEvents(true), this.loadDispatchSettings()]);
          if (this.view === 'archive') await this.loadArchives(this.selectedArchiveId);
          if (this.sideTab === 'board') await this.reloadBlackboardGroup(this.project?.project?.parent_project_id);
          if (this.view === 'graph') this.updateGraph();
        } else if (this.activePage === 'vulnerabilities') {
          await this.loadProjects();
          await this.loadAllProjectDetails(true);
        } else if (this.activePage === 'projects') {
          await Promise.all([this.loadProjectGroups(), this.loadProjects()]);
          await this.loadAllProjectDetails(true);
          if (this.selectedProjectManagementTaskId) await this.loadProjectDetailForManagement(this.selectedProjectManagementTaskId);
          if (this.selectedProjectManagementId) await this.loadProjectCredentials(this.selectedProjectManagementId);
        } else if (this.activePage === 'skills') {
          await this.loadSkills();
          if (this.skillEditor?.name && !this.skillEditor.fileDirty) await this.loadSkillFiles();
        } else if (this.activePage === 'files') {
          const selected = {
            groupId: this.fileGroupId,
            taskId: this.fileTaskId,
            folder: this.fileFolder,
            isGroupLevel: this.fileIsGroupLevel,
          };
          await Promise.all([this.loadProjectGroups(), this.loadProjects()]);
          await this.loadFileTree();
          if (selected.folder && (selected.isGroupLevel ? selected.groupId : selected.taskId)) {
            await this.selectFolder(selected.groupId, selected.taskId, selected.folder, selected.isGroupLevel);
          }
        } else if (this.activePage === 'settings') {
          await Promise.all([this.loadSettings(), this.loadDispatchSettings()]);
        } else if (this.activePage === 'prompts') {
          this.loadPrompts();
        }
        if (!silent) this.showToast('已刷新');
      } catch (e) {
        this.showToast('刷新失败: ' + (e.message || e), 'error');
      } finally {
        this.refreshLoading = false;
      }
    },

    loadLocalPrefs() {
      try {
        const raw = localStorage.getItem('axiom.localPrefs');
        if (!raw) {
          this.localPrefs.actor_name = '人工';
        } else {
          const parsed = JSON.parse(raw);
          if (typeof parsed.actor_name === 'string') this.localPrefs.actor_name = parsed.actor_name;
          if (this.isValidLayoutMode(parsed.layout_mode)) {
            this.localPrefs.layout_mode = parsed.layout_mode;
          } else if (parsed.layout_dir === 'TB' || parsed.layout_dir === 'LR') {
            this.localPrefs.layout_mode = parsed.layout_dir === 'LR' ? 'elk_lr' : 'elk_tb';
          }
        }
        const rawPanelWidth = localStorage.getItem('axiom.sidePanelWidth');
        if (rawPanelWidth !== null) {
          const savedPanelWidth = Number(rawPanelWidth);
          if (Number.isFinite(savedPanelWidth)) this.sidePanelWidth = savedPanelWidth;
        }
      } catch (e) {
        console.error(e);
      }
      if (!this.localPrefs.actor_name.trim()) this.localPrefs.actor_name = '人工';
      if (!this.isValidLayoutMode(this.localPrefs.layout_mode)) this.localPrefs.layout_mode = 'elk_tb';
      this.layoutMode = this.localPrefs.layout_mode;
    },

    saveLocalPrefs() {
      try {
        localStorage.setItem('axiom.localPrefs', JSON.stringify(this.localPrefs));
      } catch (e) {
        console.error(e);
      }
    },

    saveSidePanelWidth() {
      try {
        localStorage.setItem('axiom.sidePanelWidth', String(this.sidePanelWidth));
      } catch (e) {
        console.error(e);
      }
    },

    saveLocalSettings() {
      try {
        this.localPrefs.actor_name = this.actorName();
        this.localPrefs.layout_mode = this.isValidLayoutMode(this.localPrefs.layout_mode) ? this.localPrefs.layout_mode : 'elk_tb';
        this.layoutMode = this.localPrefs.layout_mode;
        this.saveLocalPrefs();
        if (this.cy) this.cy.layout(this.layoutOpts()).run();
        this.showLocalPrefs = false;
        this.showToast('本地偏好已保存');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async copyYamlPreview() {
      if (!this.yamlPreviewText) return;
      try {
        await this.copyText(this.yamlPreviewText);
        this.showToast('已复制');
      } catch {
        this.showToast('复制失败', 'error');
      }
    },

    async viewProjectYaml(projectId, projectTitle = '') {
      if (!projectId) return;
      this.exportProjectId = projectId;
      this.yamlPreviewTitle = this.projectLabel(projectId, projectTitle);
      this.exportTab = 'yaml';
      await this.loadExportTab('yaml');
      this.showYamlModal = true;
    },

    async viewCrawlerSnapshot(groupId, filename, title = '') {
      if (!groupId || !filename) return;
      try {
        const data = await this.api('GET', `/file-groups/${groupId}/crawler/${encodeURIComponent(filename)}`);
        const text = data.content || '';
        this.yamlPreviewTitle = `爬虫快照 · ${title || groupId} · ${filename}`;
        this.exportTab = 'yaml';
        this.yamlPreviewText = text;
        this.yamlPreviewHtml = this.highlightYaml(text);
        this.showYamlModal = true;
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async switchExportTab(tab) {
      if (tab === this.exportTab) return;
      this.exportTab = tab;
      await this.loadExportTab(tab);
    },

    async loadExportTab(tab) {
      if (!this.exportProjectId) return;
      try {
        const text = await this.fetchText(`/projects/${this.exportProjectId}/export?format=${tab}`);
        this.yamlPreviewText = text;
        this.yamlPreviewHtml = tab === 'yaml' ? this.highlightYaml(text) : this.highlightTimeline(text);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },
  }
};
