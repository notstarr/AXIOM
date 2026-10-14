window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.core = {
  methods: {
    _templateCache: {},
    _loadedScripts: {},
    _pageMountGeneration: 0,
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
    pageTemplateName() {
      if (this.activePage === 'task-detail') {
        if (this.view === 'graph') return 'graph-view';
        if (this.view === 'archive') return 'archive-view';
        return 'human-view';
      }
      return ({
        dashboard: 'dashboard', tasks: 'task-list', vulnerabilities: 'vulnerabilities',
        projects: 'project-mgmt', 'guard-audit': 'guard-audit', skills: 'skills',
        files: 'files', mcp: 'mcp', prompts: 'prompts', settings: 'settings',
        workers: 'workers', tools: 'tools',
      })[this.activePage] || 'dashboard';
    },

    async loadScriptOnce(src) {
      if (this._loadedScripts[src]) return this._loadedScripts[src];
      this._loadedScripts[src] = new Promise((resolve, reject) => {
        const existing = document.querySelector(`script[data-lazy-src="${src}"]`);
        if (existing?.dataset.loaded === 'true') { resolve(); return; }
        const script = existing || document.createElement('script');
        script.src = src;
        script.async = false;
        script.dataset.lazySrc = src;
        script.addEventListener('load', () => { script.dataset.loaded = 'true'; resolve(); }, { once: true });
        script.addEventListener('error', () => reject(new Error(`Failed to load ${src}`)), { once: true });
        if (!existing) document.head.appendChild(script);
      });
      return this._loadedScripts[src];
    },

    async ensureGraphRuntime() {
      for (const src of [
        '/static/vendor/dagre.min.js', '/static/vendor/cytoscape.min.js',
        '/static/vendor/cytoscape-dagre.js', '/static/vendor/elk.bundled.js',
        '/static/vendor/cytoscape-elk.js', '/static/vendor/cytoscape-html-label.js',
      ]) await this.loadScriptOnce(src);
    },

    async ensureMarkdownRuntime() {
      if (!window.marked) await this.loadScriptOnce('/static/vendor/marked.min.js');
    },

    // 产物预览消毒。
    // markdown 允许内联 HTML，但预览是把渲染结果 x-html 注入主文档的：
    // 产物里只要带一个 <style>，它的选择器就会作用到整个界面
    // （实测产物内嵌 body{width:60vw;margin:15vh auto} + div{opacity:.8}
    //   把整个 GUI 压成中间窄列并层层压淡，几乎不可读）。
    // 渲染后必须剥掉会外溢的标签与事件属性，再交给 x-html。
    //
    // 注意：必须用 <template> 做「片段」解析。用 DOMParser 按整篇文档解析时，
    // 产物里若内嵌 <html>/<head>/<body>，解析器会重构文档结构并把后面的正文丢掉
    // （实测 SSRF 证据里内嵌的 example.com 正文被整段吞掉）。
    sanitizePreviewHtml(html) {
      if (typeof html !== 'string' || !html) return '';
      const blocked = new Set(['STYLE', 'SCRIPT', 'LINK', 'META', 'BASE', 'TITLE',
        'IFRAME', 'FRAME', 'FRAMESET', 'OBJECT', 'EMBED', 'APPLET', 'FORM']);
      let tpl;
      try {
        tpl = document.createElement('template');
        tpl.innerHTML = html;
      } catch (_) {
        return html.replace(
          /<\s*(style|script|link|meta|base|title|iframe|object|embed|form)\b[\s\S]*?(<\s*\/\s*\1\s*>|$)/gi, '');
      }
      const root = tpl.content || tpl;
      root.querySelectorAll('*').forEach((el) => {
        if (blocked.has(el.tagName)) { el.remove(); return; }
        for (const attr of [...el.attributes]) {
          const name = attr.name.toLowerCase();
          const value = String(attr.value || '').replace(/[\u0000-\u0020]/g, '').toLowerCase();
          if (name.startsWith('on')) { el.removeAttribute(attr.name); continue; }
          if ((name === 'href' || name === 'src' || name === 'xlink:href')
            && (value.startsWith('javascript:') || value.startsWith('data:text/html'))) {
            el.removeAttribute(attr.name);
          }
        }
      });
      return tpl.innerHTML;
    },

    // markdown 里的原始 HTML 一律按「文本」输出，不当 HTML 执行。
    // 预览是把渲染结果 x-html 注入主文档的：产物里只要有 <style>（未闭合更糟）
    // 就会污染整个界面；有 <img onerror> 更会直接执行脚本。
    // 这里覆盖 marked 的 html renderer，把 HTML 转义成可见文本。
    markdownEscapeHtml() {
      if (!window.marked || this._mdHtmlEscaped) return;
      this._mdHtmlEscaped = true;
      window.marked.use({
        renderer: {
          html(token) {
            const raw = (token && typeof token === 'object')
              ? (token.text != null ? token.text : token.raw)
              : token;
            return String(raw == null ? '' : raw)
              .replace(/&/g, '&amp;')
              .replace(/</g, '&lt;')
              .replace(/>/g, '&gt;')
              .replace(/"/g, '&quot;');
          },
        },
      });
    },

    // 统一的预览渲染入口：确保 marked 就绪 → 转义原始 HTML → 再消毒兜底。
    renderMarkdown(content) {
      if (typeof marked === 'undefined') return '';
      this.markdownEscapeHtml();
      return this.sanitizePreviewHtml(marked.parse(content || ''));
    },

    showPageSkeleton() {
      const host = document.getElementById('page-host');
      if (!host) return;
      if (window.Alpine && host.firstElementChild) window.Alpine.destroyTree(host);
      host.innerHTML = '<div class="page-skeleton"><span></span><span></span><span></span></div>';
      this.mountedPageTemplate = '';
    },

    cleanupMountedPage() {
      this._pageAbortController?.abort();
      this._pageAbortController = new AbortController();
      if (this._minimapRaf) cancelAnimationFrame(this._minimapRaf);
      this._minimapRaf = null;
      if (this.cy) { this.teardownAutoFit(); this.stopGraphActivityAnimation?.(); this.cy.destroy(); this.cy = null; }
    },

    async mountCurrentPage() {
      const name = this.pageTemplateName();
      const generation = ++this._pageMountGeneration;
      const host = document.getElementById('page-host');
      if (!host) return;
      this.pageMountLoading = true;
      this.cleanupMountedPage();
      if (window.Alpine && host.firstElementChild) window.Alpine.destroyTree(host);
      host.innerHTML = '<div class="page-skeleton"><span></span><span></span><span></span></div>';
      if (name === 'graph-view') await this.ensureGraphRuntime();
      const html = await this.loadTemplate(name);
      if (generation !== this._pageMountGeneration) return;
      host.innerHTML = html;
      this.mountedPageTemplate = name;
      if (window.Alpine) window.Alpine.initTree(host);
      this.pageMountLoading = false;
      await this.activateMountedPage(name);
    },

    async activateMountedPage(name) {
      if (name === 'dashboard') {
        await Promise.all([
          this.loadProjectGroups(), this.loadProjects(),
          this.loadDashboardSummary?.(),
        ]);
      } else if (name === 'task-list') {
        await Promise.all([this.loadProjects(), this.loadDispatchSettings()]);
        this.$nextTick(() => this.restoreProjectListScroll?.());
      } else if (name === 'vulnerabilities') {
        await Promise.all([this.loadProjectGroups(), this.loadProjects()]);
        if (this.factViewMode === 'result') await this.loadRedTeamResultPage?.();
        else await this.loadVulnerabilityPage?.();
      } else if (name === 'project-mgmt') {
        await Promise.all([this.loadProjectGroups(), this.loadProjects()]);
        if (this.selectedProjectManagementId) await this.loadProjectCredentials(this.selectedProjectManagementId);
      } else if (name === 'guard-audit') await this.loadGuardAuditEvents();
      else if (name === 'skills') await this.loadSkills();
      else if (name === 'files') await this.loadArtifactCenter();
      else if (name === 'mcp') await this.loadMcpPage();
      else if (name === 'settings') await this.loadSettings();
      else if (name === 'workers') await this.loadAxiomWorkersPage();
      else if (name === 'tools') await this.loadAxiomToolsPage();
      else if (name === 'prompts') this.loadPrompts();
      else if (name === 'graph-view') this.$nextTick(() => {
        if (!this.cy && this.project && this.taskDetailSectionReady?.('graph')) this.initGraph();
      });
      else if (name === 'archive-view') await this.loadArchives();
    },

    async checkAuthStatus() {
      try {
        const token = localStorage.getItem('axiom_token');
        const headers = token ? { 'Authorization': `Bearer ${token}` } : {};
        const resp = await fetch('/api/v1/auth/status', { headers });
        if (resp.ok) {
          const data = await resp.json();
          if (data.authenticated) {
            this.isAuthenticated = true;
            this.currentUser = data.username;
          } else {
            this.isAuthenticated = false;
            this.currentUser = null;
          }
        } else {
          this.isAuthenticated = false;
          this.currentUser = null;
        }
      } catch (e) {
        console.error('Failed to check auth status:', e);
        this.isAuthenticated = false;
      } finally {
        this.authLoading = false;
      }
    },

    async performLogin() {
      if (!this.loginForm.username || !this.loginForm.password) {
        this.loginForm.error = '请输入用户名和密码';
        return;
      }
      this.loginForm.loading = true;
      this.loginForm.error = '';
      try {
        const resp = await fetch('/api/v1/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: this.loginForm.username,
            password: this.loginForm.password,
            remember: this.loginForm.remember,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data.error?.message || '登录失败，请检查账号密码');
        }
        localStorage.setItem('axiom_token', data.token);
        this.isAuthenticated = true;
        this.currentUser = data.username;
        this.loginForm.password = '';
        this.showToast('登录成功，欢迎回来！');
        this.handleRoute();
      } catch (err) {
        this.loginForm.error = err.message || '登录失败';
      } finally {
        this.loginForm.loading = false;
      }
    },

    async performLogout() {
      try {
        await fetch('/api/v1/auth/logout', { method: 'POST' });
      } catch (e) {}
      localStorage.removeItem('axiom_token');
      this.isAuthenticated = false;
      this.currentUser = null;
      this.showToast('已安全退出登录');
    },

    openChangePasswordDialog() {
      this.changePasswordDialog = {
        open: true,
        oldPassword: '',
        newPassword: '',
        confirmPassword: '',
        error: '',
        success: '',
        loading: false,
      };
    },

    async submitChangePassword() {
      const d = this.changePasswordDialog;
      if (!d.oldPassword) {
        d.error = '请输入原密码';
        return;
      }
      if (!d.newPassword) {
        d.error = '请输入新密码';
        return;
      }
      if (d.newPassword.length < 6) {
        d.error = '新密码长度至少需要 6 位';
        return;
      }
      if (d.newPassword !== d.confirmPassword) {
        d.error = '两次输入的新密码不一致';
        return;
      }
      d.loading = true;
      d.error = '';
      d.success = '';
      try {
        const token = localStorage.getItem('axiom_token');
        const resp = await fetch('/api/v1/auth/change-password', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': token ? `Bearer ${token}` : '',
          },
          body: JSON.stringify({
            old_password: d.oldPassword,
            new_password: d.newPassword,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data.error?.message || '修改密码失败');
        }
        if (data.token) {
          localStorage.setItem('axiom_token', data.token);
        }
        d.success = '密码修改成功！请妥善保管新密码';
        this.showToast('密码修改成功');
        setTimeout(() => {
          d.open = false;
        }, 1200);
      } catch (err) {
        d.error = err.message || '修改失败';
      } finally {
        d.loading = false;
      }
    },

    async init() {
      this._panelResizeMove = (e) => this.onPanelResize(e);
      this._panelResizeStop = () => this.stopPanelResize();
      window.addEventListener('pointermove', this._panelResizeMove);
      window.addEventListener('pointerup', this._panelResizeStop);
      this.loadLocalPrefs();
      this.sidebarCollapsed = localStorage.getItem('axiom.sidebarCollapsed') === 'true';
      this.initDarkMode();
      this.loadPrompts();
      this._pageAbortController = new AbortController();
      
      // 鉴权检测
      await this.checkAuthStatus();
      if (this.isAuthenticated) {
        this.startPolling();
      }

      // 全局 401 兜底：凭据失效时切回登录态
      window.addEventListener('axiom:unauthorized', () => {
        this.isAuthenticated = false;
        this.currentUser = null;
        this.authLoading = false;
        this.stopPolling?.();
        try { localStorage.removeItem('axiom_token'); } catch (_) {}
      });

      window.addEventListener('hashchange', () => {
        if (this.isAuthenticated) {
          this.handleRoute();
        }
      });
      window.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); this.openGlobalSearch(); }
        if (e.key === 'Escape') {
          if (this.showGlobalSearch) this.closeGlobalSearch();
          if (this.errorDetail) this.errorDetail = null;
        }
      });
      if (this.isAuthenticated) {
        this.handleRoute();
      }
    },

    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      localStorage.setItem('axiom.sidebarCollapsed', String(this.sidebarCollapsed));
      this.$nextTick(() => {
        if (this.cy) { this.cy.resize(); this.drawMinimap(); }
      });
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
      this.$nextTick(() => this.applyGraphTheme?.());
    },

    async openGlobalSearch() {
      this.showGlobalSearch = true;
      await Promise.all([
        this.projectsLoaded ? Promise.resolve() : this.loadProjects(),
        this.projectGroups.length ? Promise.resolve() : this.loadProjectGroups(),
      ]);
      this.$nextTick(() => this.$refs.globalSearchInput?.focus());
    },

    closeGlobalSearch() {
      clearTimeout(this._globalSearchTimer);
      this._globalSearchAbortController?.abort();
      this._globalSearchAbortController = null;
      this._globalSearchGeneration += 1;
      this.showGlobalSearch = false;
      this.globalSearchQuery = '';
      this.globalSearchVulnerabilities = [];
      this.globalSearchLoading = false;
    },

    scheduleGlobalSearch() {
      clearTimeout(this._globalSearchTimer);
      const query = this.globalSearchQuery.trim();
      if (!query) {
        this._globalSearchAbortController?.abort();
        this._globalSearchAbortController = null;
        this._globalSearchGeneration += 1;
        this.globalSearchVulnerabilities = [];
        this.globalSearchLoading = false;
        return;
      }
      this._globalSearchTimer = setTimeout(() => this.searchGlobalVulnerabilities(query), 250);
    },

    async searchGlobalVulnerabilities(query) {
      const generation = ++this._globalSearchGeneration;
      this._globalSearchAbortController?.abort();
      const controller = new AbortController();
      this._globalSearchAbortController = controller;
      this.globalSearchLoading = true;
      try {
        const params = new URLSearchParams({ search: query, limit: '10', offset: '0' });
        const response = await fetch(`/api/v1/vulnerabilities?${params}`, {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const items = await response.json();
        if (generation === this._globalSearchGeneration && query === this.globalSearchQuery.trim()) {
          this.globalSearchVulnerabilities = items;
        }
      } catch (error) {
        if (error?.name !== 'AbortError') console.debug('Global vulnerability search failed', error);
      } finally {
        if (generation === this._globalSearchGeneration) {
          this.globalSearchLoading = false;
          this._globalSearchAbortController = null;
        }
      }
    },

    globalSearchResults() {
      if (!this.globalSearchQuery.trim()) return [];
      const q = this.globalSearchQuery.toLowerCase();
      const results = [];
      for (const p of this.projects || []) {
        const title = (p.title || '').toLowerCase();
        const id = (p.id || '').toLowerCase();
        if (title.includes(q) || id.includes(q)) {
          results.push({type: 'project', id: p.id, title: p.title || p.id, subtitle: p.id, action: () => this.openTaskOverview(p.id)});
        }
      }
      for (const g of this.projectGroups || []) {
        if ((g.title || '').toLowerCase().includes(q)) {
          results.push({type: 'group', id: g.id, title: g.title, subtitle: g.id, action: () => { this.navigateTo('projects'); this.selectManagedProject(g.id); }});
        }
      }
      for (const v of this.globalSearchVulnerabilities || []) {
        results.push({
          type: 'vuln', id: v.id, title: v.title, subtitle: v.task_id,
          action: () => {
            this.vulnFilter.projectId = v.task_id || '';
            this.vulnFilter.keyword = v.title || '';
            this.navigateTo('vulnerabilities');
          },
        });
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
      const routeText = hash.replace(/^#\/?/, '') || 'dashboard';
      const [routePath, routeQuery = ''] = routeText.split('?', 2);
      const currentTargetHash = location.hash || '#/dashboard';

      const recordHistory = (target) => {
        if (!this._navigatingBack && this._lastNavigatedHash && this._lastNavigatedHash !== target) {
          this.pageHistory.push(this._lastNavigatedHash);
          if (this.pageHistory.length > 50) this.pageHistory.shift();
        }
      };

      const taskMatch = routePath.match(/^tasks\/(.+)$/);
      const legacyProjectMatch = routePath.match(/^projects\/(.+)$/);
      if (taskMatch || legacyProjectMatch) {
        const id = decodeURIComponent((taskMatch || legacyProjectMatch)[1]);
        const queryParams = new URLSearchParams(routeQuery);
        const requestedView = queryParams.get('view') === 'graph' ? 'graph' : (queryParams.get('view') === 'archive' ? 'archive' : 'human');
        recordHistory(currentTargetHash);
        this._lastNavigatedHash = currentTargetHash;
        this._navigatingBack = false;
        this.activePage = 'task-detail';
        this.openProject(id, true, requestedView);
        return;
      }
      const page = routePath.split('/')[0];
      // 「重要事实」路由：#/facts 为新地址，#/vulnerabilities 保留兼容旧书签
      const normalizedPage = page === 'facts' ? 'vulnerabilities' : page;
      const allowed = ['dashboard', 'tasks', 'vulnerabilities', 'facts', 'projects', 'guard-audit', 'skills', 'files', 'mcp', 'settings', 'prompts', 'workers', 'tools'];
      const newPage = allowed.includes(normalizedPage) ? normalizedPage : 'dashboard';
      const sameMountedFilesPage = newPage === 'files'
        && this.activePage === 'files'
        && this.mountedPageTemplate === 'files';

      recordHistory(currentTargetHash);
      this._lastNavigatedHash = currentTargetHash;
      this._navigatingBack = false;
      this.activePage = newPage;
      this.mobileSidebarOpen = false;
      if (this.activePage !== 'task-detail' && this.view !== 'list') this.backToList(true);
      if (newPage === 'files') {
        this.applyArtifactRouteParams?.(new URLSearchParams(routeQuery));
        if (sameMountedFilesPage) {
          this.loadArtifactCenter?.();
          return;
        }
      }
      if (newPage === 'vulnerabilities') {
        this.applyVulnerabilityRouteParams?.(new URLSearchParams(routeQuery));
      }
      if (newPage === 'tasks') {
        this.applyTaskManagementRouteParams?.(new URLSearchParams(routeQuery));
      }
      if (newPage === 'workers') {
        this.applyWorkerRouteParams?.(new URLSearchParams(routeQuery));
      }
      this.mountCurrentPage();
    },

    navigateTo(page) {
      const routes = {
        dashboard: '#/dashboard',
        tasks: '#/tasks',
        vulnerabilities: '#/facts',
        projects: '#/projects',
        'guard-audit': '#/guard-audit',
        skills: '#/skills',
        files: '#/files',
        mcp: '#/mcp',
        settings: '#/settings',
        prompts: '#/prompts',
        workers: '#/workers',
        tools: '#/tools',
      };
      const target = routes[page] || '#/dashboard';
      if (location.hash === target) this.handleRoute();
      else location.hash = target;
    },

    goBack() {
      if (this.pageHistory.length === 0) return;
      const targetHash = this.pageHistory.pop();
      if (!targetHash) return;
      this._navigatingBack = true;
      if (location.hash === targetHash) this.handleRoute();
      else location.hash = targetHash;
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
          await Promise.all([this.loadProject(this.selectedProjectId), this.loadSeeEvents(true)]);
          if (this.view === 'archive') await this.loadArchives(this.selectedArchiveId);
          if (this.sideTab === 'board') await this.reloadBlackboardGroup(this.project?.project?.parent_project_id);
          if (this.view === 'graph') this.updateGraph();
        } else if (this.activePage === 'vulnerabilities') {
          await Promise.all([this.loadProjectGroups(), this.loadProjects()]);
          if (this.factViewMode === 'result') await this.loadRedTeamResultPage?.();
          else await this.loadVulnerabilityPage();
        } else if (this.activePage === 'projects') {
          await Promise.all([this.loadProjectGroups(), this.loadProjects()]);
          if (this.selectedProjectManagementTaskId) await this.loadProjectDetailForManagement(this.selectedProjectManagementTaskId);
          if (this.selectedProjectManagementId) await this.loadProjectCredentials(this.selectedProjectManagementId);
        } else if (this.activePage === 'skills') {
          await this.loadSkills();
          if (this.skillEditor?.name && !this.skillEditor.fileDirty) await this.loadSkillFiles();
        } else if (this.activePage === 'mcp') {
          await this.loadMcpPage();
        } else if (this.activePage === 'workers') {
          await this.loadAxiomWorkersPage();
        } else if (this.activePage === 'tools') {
          await this.loadAxiomToolsPage();
        } else if (this.activePage === 'files') {
          await this.loadArtifactCenter();
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
        if (raw) {
          const parsed = JSON.parse(raw);
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

    async copyYamlPreview() {
      if (!this.yamlPreviewText) return;
      try {
        await this.copyText(this.yamlPreviewText);
        this.showToast('已复制');
      } catch {
        this.showToast('复制失败', 'error');
      }
    },

    async viewCrawlerSnapshot(groupId, filename, title = '') {
      if (!groupId || !filename) return;
      try {
        const data = await this.api('GET', `/file-groups/${groupId}/crawler/${encodeURIComponent(filename)}`);
        const text = data.content || '';
        this.yamlPreviewTitle = `爬虫快照 · ${title || groupId} · ${filename}`;
        this.yamlPreviewText = text;
        this.yamlPreviewHtml = this.highlightYaml(text);
        this.showYamlModal = true;
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

  }
};
