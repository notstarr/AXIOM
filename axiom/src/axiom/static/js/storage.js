window.AxiomModules = window.AxiomModules || {};
// 存储治理面板。
// 注意：v2-adapter 会把 core.methods.api 替换为旧版路径翻译器（对未映射的
// /api/v1 全路径直接抛“尚未映射”），因此本模块自带直连 fetch 的请求器，
// 不经过 this.api。概览接口恒定毫秒级返回缓存；重算在后台线程，
// 前端轮询 refreshing 标志自动补拉。
window.AxiomModules.storage = {
  methods: {
    async _requestJson(method, url, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json' } };
      if (body !== undefined) opts.body = JSON.stringify(body);
      const response = await fetch(url, opts);
      const text = await response.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }
      if (!response.ok) {
        let message = `HTTP ${response.status}`;
        if (data && typeof data.detail === 'string') message = data.detail;
        else if (Array.isArray(data && data.detail)) message = data.detail.map(item => item.msg).join('; ');
        else if (text) message = text.slice(0, 200);
        throw new Error(message);
      }
      return response.status === 204 ? null : (data ?? {});
    },

    // x-effect 的依赖收集只覆盖同步执行段；setTimeout 把响应式读写挪出追踪，
    // 避免 loading 翻转引发 effect 重放。
    queueStorageOverview() {
      setTimeout(() => { this.loadStorageOverview(); }, 0);
    },

    async loadStorageOverview(force = false) {
      if (!force && this._storageInflight) return this._storageInflight;
      if (!force && this.storageOverview && !this.storageOverview.refreshing) return this.storageOverview;
      const task = this._fetchOverview().finally(() => { this._storageInflight = null; });
      if (!force) this._storageInflight = task;
      return task;
    },

    async _fetchOverview(attempt = 0) {
      this.storageLoading = attempt === 0;
      try {
        const data = await this._requestJson('GET', '/api/v1/storage/overview');
        this.storageOverview = data;
        if (data.refreshing && attempt < 40) {
          setTimeout(() => { this._fetchOverview(attempt + 1); }, 4000);
        }
        return data;
      } catch (e) {
        this.showToast(e.message || '加载存储概览失败', 'error');
        return null;
      } finally {
        this.storageLoading = false;
      }
    },

    async refreshStorageOverview() {
      if (this.storageRotating) return;
      this.storageRotating = true;
      try {
        await this._requestJson('POST', '/api/v1/storage/refresh', {});
        this.showToast('统计已重建');
        await this.loadStorageOverview(true);
      } catch (e) {
        this.showToast(e.message || '刷新失败', 'error');
      } finally {
        this.storageRotating = false;
      }
    },

    async rotateStorageLogs() {
      if (this.storageRotating) return;
      // eslint-disable-next-line no-alert
      if (!confirm('确认立即轮转日志？当前 server / runner 日志会压缩归档并清空，正在写入的进程不受影响。积压较多时压缩可能需要一分钟左右。')) return;
      this.storageRotating = true;
      const actor = this.actorName ? this.actorName() : 'human.frontend';
      try {
        const result = await this._requestJson('POST', `/api/v1/storage/logs/rotate?actor=${encodeURIComponent(actor)}`, {});
        const freed = this.humanBytes(result.bytes_freed_total || 0);
        this.showToast(`已轮转 ${result.rotated.length} 个日志、清理 ${result.purged_archives.length} 个过期归档，释放 ${freed}`);
        await this.loadStorageOverview(true);
      } catch (e) {
        this.showToast(e.message || '轮转失败', 'error');
      } finally {
        this.storageRotating = false;
      }
    },

    humanBytes(value) {
      let v = Number(value) || 0;
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
      return `${i === 0 || v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
    },

    // ── 清理中心（三档） ──

    queueCleanupPlan() {
      setTimeout(() => { this.loadCleanupPlan(); }, 0);
    },

    async loadCleanupPlan(force = false) {
      if (!force && this._cleanupPlanInflight) return this._cleanupPlanInflight;
      if (!force && this.cleanupPlan && !this.cleanupPlan.refreshing) return this.cleanupPlan;
      const task = this._fetchCleanupPlan().finally(() => { this._cleanupPlanInflight = null; });
      if (!force) this._cleanupPlanInflight = task;
      return task;
    },

    async _fetchCleanupPlan(attempt = 0) {
      this.cleanupPlanLoading = attempt === 0;
      try {
        const data = await this._requestJson('GET', '/api/v1/storage/cleanup/plan');
        this.cleanupPlan = data;
        for (const item of (data.tier1 || [])) {
          if (this.cleanupTier1Selection[item.key] === undefined) {
            this.cleanupTier1Selection[item.key] = !!item.default_selected && item.bytes > 0;
          }
        }
        if (data.refreshing && attempt < 40) {
          setTimeout(() => { this._fetchCleanupPlan(attempt + 1); }, 4000);
        }
        return data;
      } catch (e) {
        this.showToast(e.message || '加载清理计划失败', 'error');
        return null;
      } finally {
        this.cleanupPlanLoading = false;
      }
    },

    async refreshCleanupPlan() {
      if (this.cleanupExecuteBusy) return;
      this.cleanupExecuteBusy = true;
      try {
        await this._requestJson('POST', '/api/v1/storage/cleanup/plan/refresh', {});
        this.showToast('清理计划已重建');
        await this.loadCleanupPlan(true);
      } catch (e) {
        this.showToast(e.message || '重建失败', 'error');
      } finally {
        this.cleanupExecuteBusy = false;
      }
    },

    selectedTier1Keys() {
      const plan = this.cleanupPlan;
      if (!plan) return [];
      return (plan.tier1 || [])
        .filter(item => item.bytes > 0 && this.cleanupTier1Selection[item.key])
        .map(item => item.key);
    },

    selectedTier1Bytes() {
      const plan = this.cleanupPlan;
      if (!plan) return 0;
      return (plan.tier1 || [])
        .filter(item => item.bytes > 0 && this.cleanupTier1Selection[item.key])
        .reduce((acc, item) => acc + (item.bytes || 0), 0);
    },

    async executeTier1Cleanup() {
      const keys = this.selectedTier1Keys();
      if (keys.length === 0) { this.showToast('请先勾选要清理的项目', 'error'); return; }
      const freed = this.humanBytes(this.selectedTier1Bytes());
      // eslint-disable-next-line no-alert
      if (!confirm(`确认清理一档项目？预计释放 ${freed}。一档为可再生数据/纯流水日志，不影响系统运行。`)) return;
      this.cleanupExecuteBusy = true;
      const actor = this.actorName ? this.actorName() : 'human.frontend';
      try {
        const result = await this._requestJson('POST', '/api/v1/storage/cleanup/execute', { keys, excluded: [], actor });
        this.showToast(`已清理 ${result.removed.length} 项，释放 ${this.humanBytes(result.freed_bytes)}`);
        await this.loadCleanupPlan(true);
        await this.loadStorageOverview(true);
      } catch (e) {
        this.showToast(e.message || '清理失败', 'error');
      } finally {
        this.cleanupExecuteBusy = false;
      }
    },

    async openCleanupPreview(key) {
      this.cleanupPreviewBusy = true;
      try {
        const data = await this._requestJson('POST', '/api/v1/storage/cleanup/preview', { keys: [key] });
        const item = (data?.items || [])[0] || null;
        this.cleanupPreview = item;
        this.cleanupPreviewChecked = {};
        for (const target of (item?.targets || [])) {
          this.cleanupPreviewChecked[target.path] = true;
        }
      } catch (e) {
        this.showToast(e.message || '生成预览失败', 'error');
      } finally {
        this.cleanupPreviewBusy = false;
      }
    },

    cleanupPreviewSelectedBytes() {
      const item = this.cleanupPreview;
      if (!item) return 0;
      return (item.targets || [])
        .filter(target => this.cleanupPreviewChecked[target.path])
        .reduce((acc, target) => acc + (target.bytes || 0), 0);
    },

    async confirmCleanupExecute() {
      const item = this.cleanupPreview;
      if (!item) return;
      const selectedCount = (item.targets || []).filter(t => this.cleanupPreviewChecked[t.path]).length;
      if (selectedCount === 0) { this.showToast('请至少勾选一项', 'error'); return; }
      const excluded = (item.targets || [])
        .filter(target => !this.cleanupPreviewChecked[target.path])
        .map(target => target.path);
      const freed = this.humanBytes(this.cleanupPreviewSelectedBytes());
      // eslint-disable-next-line no-alert
      if (!confirm(`确认删除选中的 ${selectedCount} 项？预计释放 ${freed}。该操作不可撤销。`)) return;
      this.cleanupExecuteBusy = true;
      const actor = this.actorName ? this.actorName() : 'human.frontend';
      try {
        const result = await this._requestJson('POST', '/api/v1/storage/cleanup/execute', {
          keys: [item.key], excluded, actor,
        });
        const skipped = (result.skipped || []).filter(entry => entry.reason !== 'excluded by operator');
        this.showToast(`已删除 ${result.removed.length} 项，释放 ${this.humanBytes(result.freed_bytes)}${skipped.length ? `（${skipped.length} 项跳过）` : ''}`);
        this.cleanupPreview = null;
        await this.loadCleanupPlan(true);
        await this.loadStorageOverview(true);
      } catch (e) {
        this.showToast(e.message || '清理失败', 'error');
      } finally {
        this.cleanupExecuteBusy = false;
      }
    },
  },
};
