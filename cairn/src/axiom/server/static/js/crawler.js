window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.crawler = {
  methods: {
    async loadCrawlerWorkerSettings() {
      try {
        const d = await this.api('GET', '/crawler/worker');
        this.crawlerWorkerForm = { ...d, pi_api_key: '' };
      } catch(e) {}
    },

    async saveCrawlerWorkerSettings() {
      try {
        await this.api('PUT', '/crawler/worker', this.crawlerWorkerForm);
        this.showToast('爬虫配置已保存', 'success');
        this.crawlerWorkerForm.pi_api_key = '';
      } catch(e) {
        this.showToast('保存失败: ' + (e.message || e), 'error');
      }
    },

    async crawlerFetchModels() {
      if (!this.crawlerWorkerForm.pi_base_url) return;
      this.crawlerLoadingModels = true;
      this.crawlerModelsError = '';
      this.crawlerAvailableModels = [];
      try {
        const params = new URLSearchParams({ base_url: this.crawlerWorkerForm.pi_base_url });
        if (this.crawlerWorkerForm.pi_api_key) params.set('api_key', this.crawlerWorkerForm.pi_api_key);
        const data = await this.api('GET', `/crawler/worker/models?${params}`);
        if (data.error) {
          this.crawlerModelsError = data.error;
        } else {
          this.crawlerAvailableModels = data.models || [];
          if (this.crawlerAvailableModels.length > 0 && !this.crawlerWorkerForm.pi_model) {
            this.crawlerWorkerForm.pi_model = this.crawlerAvailableModels[0].id;
            this.crawlerAutoFillContext();
          }
        }
      } catch(e) {
        this.crawlerModelsError = e.message || '获取模型列表失败';
      } finally {
        this.crawlerLoadingModels = false;
      }
    },

    crawlerAutoFillContext() {
      const m = this.crawlerAvailableModels.find(x => x.id === this.crawlerWorkerForm.pi_model);
      if (m && m.context_window) {
        this.crawlerWorkerForm.pi_context_window = String(m.context_window);
      }
    },

    async crawlerTestConnection() {
      this.crawlerTesting = true;
      this.crawlerTestResult = null;
      try {
        const result = await this.api('POST', '/crawler/worker/test', {
          pi_base_url: this.crawlerWorkerForm.pi_base_url,
          pi_model: this.crawlerWorkerForm.pi_model,
          pi_api_key: this.crawlerWorkerForm.pi_api_key || '',
          pi_provider_api: this.crawlerWorkerForm.pi_provider_api,
        });
        this.crawlerTestResult = result;
        this.showToast(result.message, result.ok ? 'info' : 'error');
      } catch(e) {
        this.crawlerTestResult = { ok: false, message: e.message };
      } finally {
        this.crawlerTesting = false;
      }
    },

    async loadCrawlerTasks() {
      try {
        this.crawlerTasks = await this.api('GET', '/crawler/tasks');
      } catch(e) { /* ignore */ }
    },

    selectedCrawlerTask() {
      return this.crawlerTasks.find(t => t.id === this.selectedCrawlerTaskId) || null;
    },

    async selectCrawlerTask(taskId) {
      this.selectedCrawlerTaskId = taskId;
      this.crawlerStatus = null;
      this.crawlerResultsPageData = null;
      this.crawlerTargetUrl = '';
      await this.loadCrawlerStatus();
      await this.loadCrawlerResultsPage();
    },

    async createCrawlerTask() {
      if (!this.newCrawlerTask.title || !this.newCrawlerTask.target_url) return;
      try {
        const task = await this.api('POST', '/crawler/tasks', this.newCrawlerTask);
        this.crawlerTasks.unshift(task);
        this.showCrawlerTaskForm = false;
        this.newCrawlerTask = { title: '', target_url: '' };
        this.selectCrawlerTask(task.id);
        this.showToast('爬虫任务已创建');
      } catch(e) {
        this.showToast(e.message || '创建失败', 'error');
      }
    },

    async deleteCrawlerTask(taskId, title) {
      if (!confirm(`删除爬虫任务 ${title || taskId}？`)) return;
      try {
        await this.api('DELETE', `/crawler/tasks/${taskId}`);
        this.crawlerTasks = this.crawlerTasks.filter(t => t.id !== taskId);
        if (this.selectedCrawlerTaskId === taskId) {
          this.selectedCrawlerTaskId = '';
          this.crawlerStatus = null;
          this.crawlerResultsPageData = null;
        }
        this.showToast('已删除');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async loadCrawlerStatus() {
      if (!this.selectedCrawlerTaskId) return;
      try {
        this.crawlerStatus = await this.api('GET', `/crawler/tasks/${this.selectedCrawlerTaskId}/status`);
      } catch(e) { /* ignore */ }
    },

    async startCrawler() {
      if (!this.selectedCrawlerTaskId || !this.crawlerTargetUrl) return;
      this.crawlerStarting = true;
      try {
        this.crawlerStatus = await this.api('POST', `/crawler/tasks/${this.selectedCrawlerTaskId}/start`, {
          target_url: this.crawlerTargetUrl,
        });
        this.showToast('爬虫已启动', 'success');
        await this.loadCrawlerTasks();
      } catch(e) {
        this.showToast('启动爬虫失败: ' + (e.message || e), 'error');
      } finally {
        this.crawlerStarting = false;
      }
    },

    async stopCrawler() {
      if (!this.selectedCrawlerTaskId) return;
      try {
        await this.api('POST', `/crawler/tasks/${this.selectedCrawlerTaskId}/stop`);
        this.showToast('爬虫已停止', 'success');
        await this.loadCrawlerStatus();
      } catch(e) {
        this.showToast('停止失败: ' + (e.message || e), 'error');
      }
    },

    async loadCrawlerResultsPage() {
      if (!this.selectedCrawlerTaskId) { this.crawlerResultsPageData = null; return; }
      try {
        this.crawlerResultsPageData = await this.api('GET', `/crawler/tasks/${this.selectedCrawlerTaskId}/results`);
      } catch(e) {
        this.crawlerResultsPageData = { pages: [], apis: [], mappings: [], counts: { pages: 0, apis: 0, mappings: 0 } };
      }
    },

    crawlerBusinessTree() {
      const data = this.crawlerResultsPageData;
      if (!data) return [];
      const pages = data.pages || [];
      const mappings = data.mappings || [];
      return pages.map(page => {
        const map = mappings.find(m => m.page_url === page.url || m.page_id === page.id);
        const onLoadApis = (map?.on_load || []).map(a => ({ method: a.method, path: a.path, note: a.note || '' }));
        const actions = (map?.actions || []).map(act => ({
          action_id: act.action_id,
          action_name: act.action_name || act.trigger_label,
          trigger_type: act.trigger_type,
          result: act.result,
          apis: (act.apis || []).map(a => ({ method: a.method, path: a.path }))
        }));
        const apiCount = onLoadApis.length + actions.reduce((s, a) => s + a.apis.length, 0);
        return {
          pageId: page.id,
          pageName: page.name || page.id,
          pageUrl: page.url || '',
          pageType: page.page_type || '',
          exhausted: page.exhausted || false,
          onLoadApis,
          actions,
          apiCount,
        };
      });
    },

    toggleTreeNode(pageId) {
      if (!this.treeExpanded) this.treeExpanded = {};
      this.treeExpanded[pageId] = !this.treeExpanded[pageId];
    },

    isTreeNodeExpanded(pageId) {
      return this.treeExpanded && this.treeExpanded[pageId];
    },

    async onCrawlerTaskSelectForProject() {
      if (this.newProject.crawlerTaskId) {
        this.newProject.origin = `crawler-task:${this.newProject.crawlerTaskId}`;
        if (!this.newProject.title) {
          const task = this.crawlerTasks.find(t => t.id === this.newProject.crawlerTaskId);
          if (task) this.newProject.title = task.title || task.id;
        }
      } else {
        this.newProject.origin = '';
      }
    },
  }
};
