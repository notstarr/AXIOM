window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.batchUrlJudge = {
  methods: {
    addBatchUrlRow() {
      const s = this.batchUrlJudge;
      s.rows.push({ id: s.nextId++, method: 'GET', url: '', body: '' });
    },

    removeBatchUrlRow(id) {
      const s = this.batchUrlJudge;
      s.rows = s.rows.filter(r => r.id !== id);
      if (s.rows.length === 0) s.rows.push({ id: s.nextId++, method: 'GET', url: '', body: '' });
    },

    clearBatchUrlRows() {
      const s = this.batchUrlJudge;
      s.rows = [{ id: s.nextId++, method: 'GET', url: '', body: '' }];
      s.results = null;
      s.error = '';
    },

    loadBatchUrlSample() {
      const s = this.batchUrlJudge;
      s.rows = [
        { id: s.nextId++, method: 'GET', url: 'https://example.com/api/users?id=1', body: '' },
        { id: s.nextId++, method: 'POST', url: 'https://example.com/api/login', body: '{"username":"admin","password":"123456"}' },
        { id: s.nextId++, method: 'GET', url: 'https://example.com/admin/config', body: '' },
        { id: s.nextId++, method: 'POST', url: 'https://example.com/api/comment', body: '<script>alert(1)</script>' },
        { id: s.nextId++, method: 'DELETE', url: 'https://example.com/api/users/all', body: '' },
        { id: s.nextId++, method: 'GET', url: 'https://example.com/products?q=%27%20OR%20%271%27%3D%271', body: '' },
      ];
      s.results = null;
      s.error = '';
    },

    async runBatchUrlJudge() {
      const s = this.batchUrlJudge;
      const items = s.rows
        .map(r => ({ method: (r.method || 'GET').toUpperCase(), url: (r.url || '').trim(), body: r.body || '' }))
        .filter(r => r.url);
      if (items.length === 0) {
        this.showToast('请至少填写一个 URL', 'error');
        return;
      }
      s.loading = true;
      s.error = '';
      try {
        const data = await this.api('POST', '/settings/judge/batch-url-test', { items });
        s.results = data;
        s.expanded = {};
        const blocked = (data.results || []).filter(r => r.decision === 'BLOCK').length;
        this.showToast(`判定完成：${data.allowed} 放行 / ${data.blocked} 拦截`, blocked > 0 ? 'info' : 'success');
      } catch (e) {
        s.error = e.message || String(e);
        this.showToast('批量判定失败: ' + s.error, 'error');
      } finally {
        s.loading = false;
      }
    },

    toggleBatchUrlDetail(idx) {
      const s = this.batchUrlJudge;
      s.expanded[idx] = !s.expanded[idx];
    },

    batchUrlDecisionClass(decision) {
      return decision === 'BLOCK'
        ? 'bg-rose-50 text-rose-600 border-rose-100'
        : 'bg-emerald-50 text-emerald-600 border-emerald-100';
    },

    batchUrlMethodClass(method) {
      const m = (method || '').toUpperCase();
      if (m === 'GET') return 'bg-emerald-50 text-emerald-600';
      if (m === 'POST') return 'bg-amber-50 text-amber-600';
      if (m === 'PUT' || m === 'PATCH') return 'bg-sky-50 text-sky-600';
      if (m === 'DELETE') return 'bg-rose-50 text-rose-500';
      return 'bg-slate-100 text-slate-500';
    },

    batchUrlSourceLabel(source) {
      const map = {
        whitelist: '白名单',
        hard_rule: '硬规则',
        model: '模型判定',
        fail_open: '放行失败(open)',
        disabled: '已禁用',
      };
      return map[source] || source || '—';
    },
  },
};
