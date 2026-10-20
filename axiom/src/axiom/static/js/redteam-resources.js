window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.redteamResources = {
  methods: {
    rr: {
      view: 'webshell',
      taskFilter: '',
      wsList: [],
      wsSelected: '',
      wsTab: 'terminal',
      wsTerm: [],
      wsCmd: '',
      wsBusy: false,
      wsPath: '',
      wsFileOut: '',
      wsWritePath: '',
      wsWriteContent: '',
      wsLogs: [],
      wsForm: { name: '', url: '', family: 'auto', request: '', response: '', remark: '' },
      wsCreateOpen: false,
      wsCreate: { task_id: '', name: '', url: '', remark: '' },
      c2Listeners: [],
      c2Sessions: [],
      c2Tasks: [],
      c2Listener: '',
      c2Session: '',
      c2Cmd: '',
      c2Wait: 30,
      c2Out: '',
      c2Oneliner: null,
      c2CreateOpen: false,
      c2Create: { task_id: '', name: '', kind: 'http', host: '0.0.0.0', port: 4444 },
      loading: false,
    },

    rrRedTeamTasks() {
      return (this.projects || []).filter(t => (t.engagement_mode || 'vulnerability') === 'red_team' && !t.archived);
    },
    rrScopeTasks() {
      const f = this.rr.taskFilter;
      if (f) return (this.projects || []).filter(t => t.id === f);
      return this.rrRedTeamTasks();
    },
    async rrLoad() {
      if (!(this.projects || []).length) await this.loadProjects();
      this.rr.loading = true;
      try {
        if (this.rr.view === 'webshell') await this.rrLoadWs();
        else await this.rrLoadC2();
      } finally {
        this.rr.loading = false;
      }
    },

    // ---------------------------------------------------- webshell
    rrWsKey(ws) { return `${ws.task_id}/${ws.id}`; },
    rrWsCurrent() { return this.rr.wsList.find(w => this.rrWsKey(w) === this.rr.wsSelected) || null; },
    async rrLoadWs() {
      const lists = await Promise.all(this.rrScopeTasks().map(t => this.v2Request('GET', `/tasks/${t.id}/webshells`).catch(() => [])));
      this.rr.wsList = lists.flat();
      if (this.rr.wsSelected && !this.rr.wsList.some(w => this.rrWsKey(w) === this.rr.wsSelected)) {
        this.rr.wsSelected = '';
        this.rr.wsTerm = [];
      }
    },
    async rrSelectWs(ws) {
      this.rr.wsSelected = this.rrWsKey(ws);
      this.rr.wsTab = 'terminal';
      await this.rrTabEnter('terminal');
    },
    async rrTabEnter(tab) {
      const ws = this.rrWsCurrent();
      if (!ws) return;
      if (tab === 'terminal') {
        const logs = await this.v2Request('GET', `/tasks/${ws.task_id}/webshells/${ws.id}/logs?limit=50`).catch(() => []);
        this.rr.wsLogs = logs;
        this.rr.wsTerm = [...logs].reverse().map(l => ({
          cmd: l.command,
          out: l.output,
          meta: `  · ${l.actor}${l.intent_id ? ' · ' + l.intent_id : ''} · ${l.ok ? 'ok' : 'fail'}`,
        }));
        this.$nextTick(() => { const el = this.$refs.rrTerm; if (el) el.scrollTop = el.scrollHeight; });
      } else if (tab === 'config') {
        this.rr.wsForm = {
          name: ws.name,
          url: ws.url,
          family: ws.family,
          request: JSON.stringify(ws.request || {}, null, 1),
          response: JSON.stringify(ws.response || {}, null, 1),
          remark: ws.remark || '',
        };
      } else if (tab === 'logs') {
        this.rr.wsLogs = await this.v2Request('GET', `/tasks/${ws.task_id}/webshells/${ws.id}/logs?limit=100`).catch(() => []);
      }
    },
    async rrWsExec() {
      const ws = this.rrWsCurrent();
      const cmd = (this.rr.wsCmd || '').trim();
      if (!ws || !cmd) return;
      this.rr.wsBusy = true;
      try {
        const r = await this.v2Request('POST', `/tasks/${ws.task_id}/webshells/${ws.id}/exec?actor=human.frontend`, { command: cmd, timeout_seconds: 30 });
        this.rr.wsTerm.push({ cmd, out: r.output, meta: `  · ${r.ok ? 'ok' : 'fail'} · ${r.duration_ms}ms` });
        this.rr.wsCmd = '';
        this.$nextTick(() => { const el = this.$refs.rrTerm; if (el) el.scrollTop = el.scrollHeight; });
        await this.rrLoadWs();
      } catch (e) {
        this.showToast(e.message, 'error');
      } finally {
        this.rr.wsBusy = false;
      }
    },
    async rrFile(op) {
      const ws = this.rrWsCurrent();
      if (!ws) return;
      const path = op === 'write' ? this.rr.wsWritePath : this.rr.wsPath;
      if (!path) { this.showToast('先填路径', 'error'); return; }
      try {
        const r = await this.v2Request('POST', `/tasks/${ws.task_id}/webshells/${ws.id}/file?actor=human.frontend`, {
          op,
          path,
          content: op === 'write' ? this.rr.wsWriteContent : '',
        });
        this.rr.wsFileOut = r.output;
        if (op === 'write') this.showToast(r.ok ? '已写入' : '写入失败', r.ok ? 'success' : 'error');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },
    async rrWsTest() {
      const ws = this.rrWsCurrent();
      if (!ws) return;
      try {
        const r = await this.v2Request('POST', `/tasks/${ws.task_id}/webshells/${ws.id}/test?actor=human.frontend`);
        this.rr.wsTerm.push({ cmd: '# test', out: r.output, meta: `  · ${r.ok ? 'ok' : 'fail'}` });
        this.showToast(r.ok ? '连接可用' : '连接失败', r.ok ? 'success' : 'error');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
      await this.rrLoadWs();
    },
    async rrWsSave() {
      const ws = this.rrWsCurrent();
      if (!ws) return;
      let request; let response;
      try {
        request = JSON.parse(this.rr.wsForm.request || '{}');
        response = JSON.parse(this.rr.wsForm.response || '{}');
      } catch (e) {
        this.showToast('模板 JSON 不合法', 'error');
        return;
      }
      try {
        await this.v2Request('PATCH', `/tasks/${ws.task_id}/webshells/${ws.id}`, {
          name: this.rr.wsForm.name,
          url: this.rr.wsForm.url,
          family: this.rr.wsForm.family,
          request,
          response,
          remark: this.rr.wsForm.remark,
        });
        this.showToast('已保存');
        await this.rrLoadWs();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },
    async rrWsArchive() {
      const ws = this.rrWsCurrent();
      if (!ws || !confirm(`归档 ${ws.id} ${ws.name}？`)) return;
      await this.v2Request('DELETE', `/tasks/${ws.task_id}/webshells/${ws.id}`);
      this.rr.wsSelected = '';
      this.showToast('已归档');
      await this.rrLoadWs();
    },
    async rrWsCreateSubmit() {
      const f = this.rr.wsCreate;
      if (!f.task_id || !f.name || !f.url) { this.showToast('任务/名称/URL 必填', 'error'); return; }
      try {
        await this.v2Request('POST', `/tasks/${f.task_id}/webshells?actor=human.frontend`, {
          name: f.name, url: f.url, remark: f.remark,
        });
        this.rr.wsCreateOpen = false;
        this.rr.wsCreate = { task_id: '', name: '', url: '', remark: '' };
        this.showToast('已创建');
        await this.rrLoadWs();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    // ---------------------------------------------------- c2
    rrC2Key(l) { return `${l.task_id}/${l.id}`; },
    rrSessKey(s) { return `${s.task_id}/${s.id}`; },
    rrSessionCurrent() { return this.rr.c2Sessions.find(s => this.rrSessKey(s) === this.rr.c2Session) || null; },
    rrSessionsFiltered() {
      const f = this.rr.c2Listener;
      if (!f) return this.rr.c2Sessions;
      return this.rr.c2Sessions.filter(s => `${s.task_id}/${s.listener_id}` === f);
    },
    async rrLoadC2() {
      const tasks = this.rrScopeTasks();
      const ls = await Promise.all(tasks.map(t => this.v2Request('GET', `/tasks/${t.id}/c2/listeners`).catch(() => [])));
      this.rr.c2Listeners = ls.flat();
      if (this.rr.c2Listener && !this.rr.c2Listeners.some(l => this.rrC2Key(l) === this.rr.c2Listener)) this.rr.c2Listener = '';
      await this.rrLoadSessions();
    },
    async rrLoadSessions() {
      const tasks = this.rrScopeTasks();
      const ss = await Promise.all(tasks.map(t => this.v2Request('GET', `/tasks/${t.id}/c2/sessions`).catch(() => [])));
      this.rr.c2Sessions = ss.flat();
      if (this.rr.c2Session && !this.rr.c2Sessions.some(s => this.rrSessKey(s) === this.rr.c2Session)) {
        this.rr.c2Session = '';
        this.rr.c2Tasks = [];
      }
      if (this.rr.c2Session) await this.rrLoadTasks();
    },
    async rrListenerStart(l) {
      try { await this.v2Request('POST', `/tasks/${l.task_id}/c2/listeners/${l.id}/start`); this.showToast('已启动'); } catch (e) { this.showToast(e.message, 'error'); }
      await this.rrLoadC2();
    },
    async rrListenerStop(l) {
      try { await this.v2Request('POST', `/tasks/${l.task_id}/c2/listeners/${l.id}/stop`); this.showToast('已停止'); } catch (e) { this.showToast(e.message, 'error'); }
      await this.rrLoadC2();
    },
    async rrListenerArchive(l) {
      if (!confirm(`归档 ${l.id} ${l.name}？`)) return;
      await this.v2Request('DELETE', `/tasks/${l.task_id}/c2/listeners/${l.id}`);
      await this.rrLoadC2();
    },
    async rrOneliner(l) {
      try { this.rr.c2Oneliner = await this.v2Request('GET', `/tasks/${l.task_id}/c2/listeners/${l.id}/oneliner`); } catch (e) { this.showToast(e.message, 'error'); }
    },
    async rrCreateListenerSubmit() {
      const f = this.rr.c2Create;
      if (!f.task_id || !f.name || !f.port) { this.showToast('任务/名称/端口必填', 'error'); return; }
      try {
        await this.v2Request('POST', `/tasks/${f.task_id}/c2/listeners?actor=human.frontend`, {
          name: f.name, kind: f.kind, host: f.host, port: Number(f.port),
        });
        this.rr.c2CreateOpen = false;
        this.showToast('已创建，需手动启动');
        await this.rrLoadC2();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },
    async rrSelectSession(s) {
      this.rr.c2Session = this.rrSessKey(s);
      await this.rrLoadTasks();
    },
    async rrLoadTasks() {
      const s = this.rrSessionCurrent();
      if (!s) { this.rr.c2Tasks = []; return; }
      this.rr.c2Tasks = await this.v2Request('GET', `/tasks/${s.task_id}/c2/tasks?session_id=${s.id}&limit=50`).catch(() => []);
    },
    async rrC2Exec() {
      const s = this.rrSessionCurrent();
      const cmd = (this.rr.c2Cmd || '').trim();
      if (!s || !cmd) return;
      try {
        const t = await this.v2Request('POST', `/tasks/${s.task_id}/c2/sessions/${s.id}/exec?actor=human.frontend`, {
          command: cmd, wait_seconds: Number(this.rr.c2Wait),
        });
        this.rr.c2Out = `[${t.status}] ${t.output}`;
        this.rr.c2Cmd = '';
        await this.rrLoadTasks();
        await this.rrLoadSessions();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },
    rrTaskOut(t) { this.rr.c2Out = `[${t.status}] ${t.output || '(无输出)'}`; },
    async rrSleep(s) {
      try {
        await this.v2Request('PUT', `/tasks/${s.task_id}/c2/sessions/${s.id}/sleep`, { sleep_seconds: Number(s.sleep_seconds) });
        this.showToast('已设置');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },
    async rrKill(s) {
      if (!confirm(`kill 会话 ${s.id}？`)) return;
      await this.v2Request('POST', `/tasks/${s.task_id}/c2/sessions/${s.id}/kill`);
      await this.rrLoadSessions();
    },
  },
};
