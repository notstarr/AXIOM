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

    // ── 二档：分组折叠 + 勾选 ──

    TIER2_GROUP_META: {
      runs: { prefix: 't2_task_runs', title: '任务执行产物', desc: 'runs/ 下已完结租约的产物文件（findings、资产清单等）与会话记录。' },
      contexts: { prefix: 't2_task_contexts', title: '上下文快照', desc: 'contexts/ 下各任务的会话上下文快照，删除后无法回放该任务历史上下文。' },
      artifacts: { prefix: 't2_task_artifacts', title: '归档任务产物文件', desc: 'tasks/ 下归档任务的 artifacts 文件本体，删除后文件中心对应产物无法下载（数据库记录保留）。' },
      sqlite: { prefix: 't2_sqlite_backups', title: '旧 SQLite 备份', desc: 'backups/ 与根目录下超过保留期的旧库备份。' },
      maintenance: { prefix: 't2_maintenance', title: '维护快照', desc: 'maintenance/ 下的人工维护快照，确认不再需要后可删。' },
      pg: { prefix: 't2_pg_backups', title: '旧 PostgreSQL 备份', desc: 'postgresql-backups/ 中超出最新保留份数的更旧备份。' },
    },

    tier2Groups() {
      const plan = this.cleanupPlan;
      const groups = [];
      for (const [key, meta] of Object.entries(this.TIER2_GROUP_META)) {
        const items = (plan?.tier2 || []).filter(item => item.key.startsWith(meta.prefix));
        const selectedItems = items.filter(item => this.cleanupTier2Selection[item.key] && item.bytes > 0);
        const allSelected = items.length > 0 && selectedItems.length === items.length;
        groups.push({
          key,
          title: meta.title,
          desc: meta.desc,
          items,
          bytes: items.reduce((acc, item) => acc + (item.bytes || 0), 0),
          selectedCount: selectedItems.length,
          selectedBytes: selectedItems.reduce((acc, item) => acc + (item.bytes || 0), 0),
          allSelected,
        });
      }
      return groups.filter(group => group.items.length > 0);
    },

    toggleTier2Group(groupKey, checked) {
      const meta = this.TIER2_GROUP_META[groupKey];
      if (!meta) return;
      for (const item of (this.cleanupPlan?.tier2 || [])) {
        if (item.key.startsWith(meta.prefix) && item.bytes > 0) {
          this.cleanupTier2Selection[item.key] = checked;
        }
      }
    },

    selectedTier2Keys() {
      return (this.cleanupPlan?.tier2 || [])
        .filter(item => item.bytes > 0 && this.cleanupTier2Selection[item.key])
        .map(item => item.key);
    },

    selectedTier2Bytes() {
      return (this.cleanupPlan?.tier2 || [])
        .filter(item => item.bytes > 0 && this.cleanupTier2Selection[item.key])
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

    async openCleanupPreview(keys) {
      const keyList = Array.isArray(keys) ? keys : [keys];
      if (keyList.length === 0) { this.showToast('请先勾选要清理的项目', 'error'); return; }
      this.cleanupPreviewBusy = true;
      try {
        const data = await this._requestJson('POST', '/api/v1/storage/cleanup/preview', { keys: keyList });
        this.cleanupPreviewItems = data?.items || [];
        this.cleanupPreviewChecked = {};
        for (const item of this.cleanupPreviewItems) {
          for (const target of (item.targets || [])) {
            this.cleanupPreviewChecked[target.path] = true;
          }
        }
      } catch (e) {
        this.showToast(e.message || '生成预览失败', 'error');
      } finally {
        this.cleanupPreviewBusy = false;
      }
    },

    cleanupPreviewAllTargets() {
      return this.cleanupPreviewItems.flatMap(item => item.targets || []);
    },

    cleanupPreviewCheckedCount() {
      return this.cleanupPreviewAllTargets().filter(target => this.cleanupPreviewChecked[target.path]).length;
    },

    cleanupPreviewTotalCount() {
      return this.cleanupPreviewAllTargets().length;
    },

    cleanupPreviewProtectedCount() {
      return this.cleanupPreviewItems.reduce((acc, item) => acc + (item.protected_count || 0), 0);
    },

    cleanupPreviewSelectedBytes() {
      return this.cleanupPreviewAllTargets()
        .filter(target => this.cleanupPreviewChecked[target.path])
        .reduce((acc, target) => acc + (target.bytes || 0), 0);
    },

    async confirmCleanupExecute() {
      if (this.cleanupPreviewItems.length === 0) return;
      const checkedCount = this.cleanupPreviewCheckedCount();
      if (checkedCount === 0) { this.showToast('请至少勾选一项', 'error'); return; }
      const excluded = this.cleanupPreviewAllTargets()
        .filter(target => !this.cleanupPreviewChecked[target.path])
        .map(target => target.path);
      const freed = this.humanBytes(this.cleanupPreviewSelectedBytes());
      // eslint-disable-next-line no-alert
      if (!confirm(`确认删除选中的 ${checkedCount} 项？预计释放 ${freed}。该操作不可撤销。`)) return;
      this.cleanupExecuteBusy = true;
      const actor = this.actorName ? this.actorName() : 'human.frontend';
      try {
        const keys = this.cleanupPreviewItems.map(item => item.key);
        const result = await this._requestJson('POST', '/api/v1/storage/cleanup/execute', {
          keys, excluded, actor,
        });
        const skipped = (result.skipped || []).filter(entry => entry.reason !== 'excluded by operator');
        this.showToast(`已删除 ${result.removed.length} 项，释放 ${this.humanBytes(result.freed_bytes)}${skipped.length ? `（${skipped.length} 项跳过）` : ''}`);
        this.cleanupPreviewItems = [];
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
