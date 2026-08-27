window.AxiomModules = window.AxiomModules || {};
// 存储治理面板。注意：core.js 的 api() 不补 /api/v1 前缀，本模块直接使用全路径。
window.AxiomModules.storage = {
  methods: {
    async loadStorageOverview(force = false) {
      if (this.storageLoading) return;
      if (!force && this.storageOverview) return;
      this.storageLoading = true;
      try {
        this.storageOverview = await this.api('GET', '/api/v1/storage/overview');
      } catch (e) {
        this.showToast(e.message || '加载存储概览失败', 'error');
      } finally {
        this.storageLoading = false;
      }
    },

    async rotateStorageLogs() {
      if (this.storageRotating) return;
      // eslint-disable-next-line no-alert
      if (!confirm('确认立即轮转日志？当前 server / runner 日志会压缩归档并清空，正在写入的进程不受影响。')) return;
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
