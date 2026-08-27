window.AxiomModules = window.AxiomModules || {};
// 存储治理面板。注意：core.js 的 api() 不补 /api/v1 前缀，本模块直接使用全路径。
// 概览接口恒定毫秒级返回缓存；大目录重算在后台线程，前端轮询 refreshing 标志自动补拉。
window.AxiomModules.storage = {
  methods: {
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
        const data = await this.api('GET', '/api/v1/storage/overview');
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
        await this.api('POST', '/api/v1/storage/refresh', {});
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
        const result = await this.api('POST', `/api/v1/storage/logs/rotate?actor=${encodeURIComponent(actor)}`, {});
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
  },
};
