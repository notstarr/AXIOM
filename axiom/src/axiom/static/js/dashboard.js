window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.dashboard = {
  methods: {
    dashboardStats() {
      const summary = this.dashboardSummary || {};
      const taskCounts = summary.task_status_counts || {};
      const severityCounts = summary.vulnerability_severity_counts || {};
      const tokenStats = summary.token_usage || {
        input: 0, input_uncached: 0, cache_read: 0, cache_write: 0, output: 0, total: 0,
      };
      return {
        runningProjects: taskCounts.running || 0,
        pausedProjects: taskCounts.stopped || 0,
        completedProjects: taskCounts.completed || 0,
        vulnTotal: Object.values(severityCounts).reduce((sum, value) => sum + Number(value || 0), 0),
        criticalVulns: severityCounts.critical || 0,
        highVulns: severityCounts.high || 0,
        workingIntents: summary.active_intent_count || 0,
        unclaimedIntents: summary.pending_intent_count || 0,
        eventTotal: (summary.recent_events || []).length,
        tokenInput: tokenStats.input_uncached ?? tokenStats.input ?? 0,
        tokenCacheRead: tokenStats.cache_read || 0,
        tokenCacheWrite: tokenStats.cache_write || 0,
        tokenOutput: tokenStats.output,
        tokenTotal: tokenStats.total,
        tokenCacheHitRate: tokenStats.cache_hit_rate,
      };
    },

    // 按任务统计 Token 消耗，按总量降序
    tokenByProject() {
      const totalAll = Number(this.dashboardSummary?.token_usage?.total || 0);
      return (this.dashboardSummary?.token_by_task || []).map(item => {
        const task = (this.projects || []).find(project => project.id === item.task_id);
        const status = task?.status || ({ running: 'active', stopped: 'stopped' }[item.status] || item.status || '');
        return {
          id: item.task_id,
          title: item.title || task?.title || item.task_id,
          status,
          input: item.input_uncached ?? item.input ?? 0,
          cacheRead: item.cache_read || 0,
          cacheWrite: item.cache_write || 0,
          output: item.output || 0,
          total: item.total || 0,
          cacheHitRate: item.cache_hit_rate,
          complete: item.complete !== false,
          incompleteSources: item.incomplete_sources || 0,
          pct: totalAll > 0 ? Math.round((item.total || 0) / totalAll * 100) : 0,
        };
      });
    },

    tokenRangeOptions() {
      return [
        { value: '7d', label: '7天' },
        { value: '30d', label: '30天' },
        { value: '3m', label: '3月' },
        { value: '6m', label: '6月' },
        { value: '1y', label: '1年' },
        { value: 'all', label: '全部' },
      ];
    },

    async setDashboardTokenRange(value) {
      if (!value || value === this.dashboardTokenRange) return;
      this.dashboardTokenRange = value;
      this.dashboardTokenHover = null;
      await this.loadDashboardSummary();
    },

    async setDashboardTokenModel(value) {
      const next = value || 'all';
      if (next === this.dashboardTokenModel) return;
      this.dashboardTokenModel = next;
      this.dashboardTokenHover = null;
      await this.loadDashboardSummary();
    },

    dashboardTokenSummary() {
      return this.dashboardSummary?.token_usage || {
        input: 0,
        input_uncached: 0,
        cache_read: 0,
        cache_write: 0,
        output: 0,
        total: 0,
        cache_hit_rate: null,
        complete: true,
        incomplete_sources: 0,
      };
    },

    dashboardTokenTaskCount() {
      return (this.dashboardSummary?.token_by_task || []).length;
    },

    tokenCacheHitRateLabel(value) {
      if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
      return `${(Number(value) * 100).toFixed(1).replace(/\.0$/, '')}%`;
    },

    tokenChartRows() {
      const series = this.dashboardSummary?.token_series || [];
      const maxTotal = Math.max(1, ...series.map(item => Number(item.total || 0)));
      return series.map((item, index) => {
        const input = Number(item.input_uncached ?? item.input ?? 0);
        const cacheRead = Number(item.cache_read || 0);
        const cacheWrite = Number(item.cache_write || 0);
        const output = Number(item.output || 0);
        const total = Number(item.total ?? (input + cacheRead + cacheWrite + output));
        const denominator = total || 1;
        return {
          ...item,
          index,
          input,
          cacheRead,
          cacheWrite,
          output,
          total,
          heightPct: total > 0 ? Math.max(2, total / maxTotal * 100) : 0,
          segments: [
            { key: 'input', label: '输入（未命中）', value: input, pct: input / denominator * 100 },
            { key: 'cache-read', label: '缓存命中', value: cacheRead, pct: cacheRead / denominator * 100 },
            { key: 'cache-write', label: '缓存写入', value: cacheWrite, pct: cacheWrite / denominator * 100 },
            { key: 'output', label: '输出', value: output, pct: output / denominator * 100 },
          ],
        };
      });
    },

    tokenChartMinWidth() {
      const count = this.dashboardSummary?.token_series?.length || 0;
      return Math.max(640, count * 30);
    },

    tokenChartBucketLabel(bucket) {
      const raw = String(bucket || '');
      if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw.slice(5);
      return raw;
    },

    tokenChartLabelVisible(index, count) {
      if (count <= 14) return true;
      const step = count <= 31 ? 5 : Math.ceil(count / 12);
      return index === 0 || index === count - 1 || index % step === 0;
    },

    // Token usage for the currently selected project (from its cached events)
    currentProjectTokenUsage() {
      const pid = this.selectedProjectId;
      if (!pid) return { input: 0, output: 0, total: 0 };
      const events = this.projectEventsCache[pid] || [];
      return events.reduce((acc, e) => {
        const u = this.extractTokenUsage(e.payload || {});
        if (!u) return acc;
        acc.input += u.input;
        acc.output += u.output;
        acc.total += u.total;
        return acc;
      }, { input: 0, output: 0, total: 0 });
    },

    recentGlobalEvents() {
      return (this.dashboardSummary?.recent_events || []).map(event => ({
        ...event,
        project_id: event.task_id || event.project_id || '',
      }));
    },

    placeholderPages() {
      return [];
    },

    settingsTabs() {
      return [
        { id: 'config', title: '配置管理', description: '并发、超时、心跳与租约' },
        { id: 'judge', title: 'Guard 管理', description: '监管模型、密钥与规则' },
        { id: 'dingtalk', title: '钉钉告警', description: '漏洞实时推送与群指令交互' },
        { id: 'storage', title: '存储治理', description: '磁盘占用总览与日志轮转' },
      ];
    },

    // tab 切换时显式触发对应面板的数据加载（模板动态挂载，x-effect 时机不可靠）。
    selectSettingsTab(id) {
      this.activeSettingsTab = id;
      if (id === 'storage') {
        this.queueStorageOverview();
        this.queueCleanupPlan();
      }
    },

    allVulnerabilities() {
      return this.vulnerabilityItems || [];
    },

    filteredVulnerabilities() {
      return this.allVulnerabilities();
    },

    _seeTokenUsageEvents() {
      return (this.seeEvents || [])
        .map(event => ({ event, usage: this._normalizeTokenUsage(event?.payload?.token_usage) }))
        .filter(item => item.usage);
    },

    seeCurrentRoundLabel() {
      const entries = this._seeTokenUsageEvents();
      if (entries.length === 0) return '—';
      const latest = entries[entries.length - 1];
      return `${latest.event.phase} · ${this._formatTokenPair(latest.usage)}`;
    },

    seeCumulativeLabel() {
      const entries = this._seeTokenUsageEvents();
      if (entries.length === 0) return '—';
      const sum = entries.reduce(
        (acc, item) => ({
          input: acc.input + item.usage.input,
          output: acc.output + item.usage.output,
          total: acc.total + item.usage.total,
        }),
        { input: 0, output: 0, total: 0 },
      );
      return this._formatTokenPair(sum);
    },

    seeTrackedPhaseCount() {
      const entries = this._seeTokenUsageEvents();
      const phases = new Set(entries.map(item => item.event.phase));
      return String(phases.size);
    },

    seePhaseBreakdownLabel() {
      const entries = this._seeTokenUsageEvents();
      if (entries.length === 0) return '分阶段：—';
      const byPhase = new Map();
      for (const item of entries) {
        const phase = item.event.phase || '未知';
        const current = byPhase.get(phase) || { input: 0, output: 0, total: 0 };
        current.input += item.usage.input;
        current.output += item.usage.output;
        current.total += item.usage.total;
        byPhase.set(phase, current);
      }
      const parts = Array.from(byPhase.entries()).map(([phase, usage]) => `${phase}: ${usage.total.toLocaleString()}`);
      return `分阶段：${parts.join(' | ')}`;
    },

    actionReplayWorkers() {
      const groups = new Map();
      for (const event of this.actionReplayEvents || []) {
        const worker = event.worker || 'unknown';
        if (!groups.has(worker)) groups.set(worker, []);
        groups.get(worker).push(event);
      }
      return Array.from(groups.entries()).map(([worker, events]) => ({
        worker,
        events,
        latest: events[events.length - 1],
        warnCount: events.filter(event => event.level === 'warn').length,
      }));
    },

    selectedActionReplayEvents() {
      if (!this.selectedActionReplayWorker) return [];
      return (this.actionReplayEvents || []).filter(event => (event.worker || 'unknown') === this.selectedActionReplayWorker);
    },

    actionReplayVisibleEvents() {
      return this.selectedActionReplayEvents().filter(event => this.actionReplayEventInput(event) || this.actionReplayEventOutput(event) || event.level === 'warn');
    },

    enabledPiTools() {
      return (this.tools || [])
        .filter(tool => tool && tool.enabled)
        .map(tool => tool.display_name || tool.name)
        .filter(Boolean);
    },

    filteredToolExecutions() {
      try {
        let items = Array.isArray(this.toolExecutions) ? this.toolExecutions : [];
        if (this.skillUsageFilter) {
          const skill = this.skillUsageFilter;
          items = items.filter(t => {
            if (!t || t.tool !== 'read') return false;
            return Array.isArray(t.argv) && t.argv.some(a => String(a || '').includes(`/skills/${skill}/SKILL.md`));
          });
        }
        if (this.toolFilterType) {
          items = items.filter(t => t && t.tool === this.toolFilterType);
        }
        if (this.toolFilterSearch) {
          const q = String(this.toolFilterSearch || '').toLowerCase();
          items = items.filter(t => {
            if (!t) return false;
            if (String(t.tool || '').toLowerCase().includes(q)) return true;
            if (Array.isArray(t.argv) && t.argv.some(a => String(a || '').toLowerCase().includes(q))) return true;
            if (String(t.stdout || '').toLowerCase().includes(q)) return true;
            return false;
          });
        }
        return items;
      } catch (e) { return this.toolExecutions || []; }
    },

    scrollReplayToBottom() {
      const el = this.$refs.actionReplayScroll;
      if (el) el.scrollTop = el.scrollHeight;
    },

    actionReplayEventToolLabel(event) {
      const payload = event?.payload || {};
      const command = this._normalizeActionReplayText(payload.command_preview || '');
      const rawTool = String(payload.tool || '').trim();
      const normalizedTool = rawTool.toLowerCase();
      if (['sh', '/bin/sh', 'bash', '/bin/bash', 'zsh', '/bin/zsh'].includes(normalizedTool) && /\bpi\b/.test(command)) {
        return 'pi';
      }
      if (rawTool) return rawTool;
      if (payload.tool_kind) return payload.tool_kind;
      return event?.task_type || '操作';
    },

    actionReplayEventInput(event) {
      const payload = event?.payload || {};
      const command = this._normalizeActionReplayText(payload.command_preview || '');
      if (!command) return '';
      const parts = command.match(/(?:[^\s"]+|"[^"]*")+/g) || [];
      if (parts.length <= 1) return command;
      let start = 0;
      const dropWords = new Set(['python', 'python3', 'bash', 'sh', 'zsh', 'uv', 'node', 'npm', 'npx']);
      const normalizeToken = (token) => String(token || '').replace(/^["']|["']$/g, '');
      if (dropWords.has(normalizeToken(parts[start])) || normalizeToken(parts[start]).includes('/')) start += 1;
      if (['run', 'exec'].includes(normalizeToken(parts[start]))) start += 1;
      if (normalizeToken(parts[start]) === String(payload.tool || '').trim()) start += 1;
      const stripped = parts.slice(start).join(' ').trim();
      return stripped || command;
    },

    actionReplayEventOutput(event) {
      const payload = event?.payload || {};
      const stdout = this._filterActionReplayOutput(payload.stdout_preview || '');
      const stderr = this._filterActionReplayOutput(payload.stderr_preview || '');
      const body = stdout || stderr;
      const meta = [];
      if (payload.returncode !== undefined && payload.returncode !== 0 && !body) meta.push(`退出码 ${payload.returncode}`);
      if (payload.timed_out) meta.push('命令执行超时');
      return [...meta, body].filter(Boolean).join('\n').trim();
    },

    _normalizeActionReplayText(text) {
      return String(text || '')
        .replace(/\u001b\[[0-9;]*m/g, '')
        .replace(/\r\n/g, '\n')
        .replace(/\r/g, '\n')
        .trim();
    },

    _filterActionReplayOutput(text) {
      const normalized = this._normalizeActionReplayText(text);
      if (!normalized) return '';
      const lines = normalized
        .split('\n')
        .map(line => line.trimEnd())
        .filter(line => line.trim())
        .filter(line => !/^(phase|worker|tool|command|returncode|timed_out)\b/i.test(line.trim()));
      const compact = lines.slice(0, 16).join('\n').trim();
      return compact;
    },

    // ── Tools Management ──
    async loadTools() {
      try {
        this.tools = await this.api('GET', '/tools');
        if (!this.selectedToolId && this.tools.length > 0) {
          this.selectTool(this.tools[0]);
        } else if (this.selectedToolId && !this.tools.some(tool => tool.id === this.selectedToolId)) {
          this.resetToolForm();
        }
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    filteredTools() {
      const keyword = (this.toolSearch || '').trim().toLowerCase();
      if (!keyword) return this.tools;
      return this.tools.filter(tool =>
        (tool.name || '').toLowerCase().includes(keyword) ||
        (tool.display_name || '').toLowerCase().includes(keyword) ||
        (tool.description || '').toLowerCase().includes(keyword)
      );
    },

    selectTool(tool) {
      this.selectedToolId = tool.id;
      this.toolForm = {
        id: tool.id,
        name: tool.name,
        display_name: tool.display_name || tool.name,
        description: tool.description || '',
        enabled: !!tool.enabled,
        builtin: !!tool.builtin,
        schema_text: JSON.stringify(tool.schema || {}, null, 2),
        config_text: JSON.stringify(tool.config || {}, null, 2),
      };
    },

    resetToolForm() {
      this.selectedToolId = '';
      this.toolForm = {
        id: '',
        name: '',
        display_name: '',
        description: '',
        enabled: true,
        builtin: false,
        schema_text: '{}',
        config_text: '{}',
      };
    },

    async saveTool() {
      try {
        const payload = {
          name: this.toolForm.name,
          display_name: this.toolForm.display_name,
          description: this.toolForm.description,
          enabled: !!this.toolForm.enabled,
          schema: this.parseJsonObjectText(this.toolForm.schema_text),
          config: this.parseJsonObjectText(this.toolForm.config_text),
        };
        if (this.selectedToolId) {
          await this.api('PUT', `/tools/${encodeURIComponent(this.selectedToolId)}`, payload);
        } else {
          await this.api('POST', '/tools', payload);
        }
        await this.loadTools();
        this.showToast('Tool 配置已保存');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async deleteTool() {
      if (!this.selectedToolId) return;
      try {
        await this.api('DELETE', `/tools/${encodeURIComponent(this.selectedToolId)}`);
        await this.loadTools();
        this.resetToolForm();
        this.showToast('Tool 已更新');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async syncPiTools() {
      try {
        this.tools = await this.api('POST', '/tools/sync-pi');
        if (this.tools.length > 0) this.selectTool(this.tools[0]);
        this.showToast('PI 内置工具已同步');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    // ── Vulnerability CRUD ──
    async openVulnEditor(v) {
      if (v) {
        this.vulnEditor = { open: true, isNew: false, project_id: v.project_id, id: v.id, title: v.title, type: v.type || '通用漏洞', affected_target: v.affected_target || v.affected_scope || '', description: v.description, prerequisites: v.prerequisites || '', impact: v.impact, remediation: v.remediation || v.recommendation || '', severity: v.severity || 'medium', status: v.status, status_reason: v.status_reason || '', finding_artifact_id: v.finding_artifact_id || '', evidence_artifact_ids: Array.isArray(v.evidence_artifact_ids) ? [...v.evidence_artifact_ids] : [], available_artifacts: [], artifacts_loading: false };
      } else {
        this.vulnEditor = { open: true, isNew: true, project_id: '', id: '', title: '', type: '通用漏洞', affected_target: '', description: '', prerequisites: '', impact: '', remediation: '', severity: 'medium', status: '待验证', status_reason: '', finding_artifact_id: '', evidence_artifact_ids: [], available_artifacts: [], artifacts_loading: false };
      }
      if (this.vulnEditor.project_id) await this.loadVulnEditorArtifacts();
    },

    async loadVulnEditorArtifacts() {
      const taskId = (this.vulnEditor.project_id || '').trim();
      if (!taskId) {
        this.vulnEditor.available_artifacts = [];
        return;
      }
      this.vulnEditor.artifacts_loading = true;
      try {
        this.vulnEditor.available_artifacts = await this.api('GET', `/projects/${taskId}/artifacts`);
      } catch (e) {
        this.vulnEditor.available_artifacts = [];
        this.showToast(e.message, 'error');
      } finally {
        this.vulnEditor.artifacts_loading = false;
      }
    },

    async saveVuln() {
      const ed = this.vulnEditor;
      if (!ed.title || !ed.project_id) { this.showToast('标题和任务 ID 必填', 'error'); return; }
      if (ed.status === '忽略' && (ed.status_reason || '').trim().length < 4) {
        this.showToast('忽略状态需填写不少于 4 字的忽略原因', 'error');
        return;
      }
      const body = { title: ed.title, type: ed.type || '通用漏洞', severity: ed.severity || 'medium', affected_target: ed.affected_target || '', description: ed.description, prerequisites: ed.prerequisites, impact: ed.impact, remediation: ed.remediation || '', status: ed.status, status_reason: (ed.status_reason || '').trim(), finding_artifact_id: ed.finding_artifact_id || null, evidence_artifact_ids: Array.isArray(ed.evidence_artifact_ids) ? ed.evidence_artifact_ids : [], source: 'manual', reported_by: this.actorName() };
      try {
        if (ed.isNew) {
          await this.api('POST', `/projects/${ed.project_id}/vulns`, body);
        } else {
          await this.api('PUT', `/projects/${ed.project_id}/vulns/${ed.id}`, body);
        }
        this.vulnEditor.open = false;
        await Promise.all([this.loadVulnerabilityPage(), this.loadDashboardSummary?.()]);
        this.showToast(ed.isNew ? '漏洞已创建' : '漏洞已更新');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    editVuln(v) { this.openVulnEditor(v); },

    vulnerabilitySortOptions() {
      return [
        { id: 'updated', label: '最近更新' },
        { id: 'severity', label: '严重度' },
        { id: 'unreviewed', label: '未阅优先' },
        { id: 'newest', label: '最新发现' },
      ];
    },

    async setVulnerabilitySort(sort) {
      if (!sort || sort === this.vulnerabilitySort) return;
      this.vulnerabilitySort = sort;
      try { localStorage.setItem('axiom-vuln-sort', sort); } catch (e) {}
      this.vulnerabilityPage = 0;
      await this.loadVulnerabilityPage({ selectFirst: false });
    },

    async toggleVulnReviewed(v) {
      if (!v?.project_id || !v?.id) return;
      const next = !v.human_reviewed;
      try {
        await this.api('PUT', `/projects/${v.project_id}/vulns/${v.id}/human-reviewed`, { value: next });
        v.human_reviewed = next;
        v.human_reviewed_at = next ? new Date().toISOString() : null;
        await this.loadVulnerabilityPage();
        await this.loadDashboardSummary?.();
        this.showToast(next ? '已标记人工已阅' : '已取消人工已阅');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async toggleVulnRedTeamExploitable(v) {
      if (!v?.project_id || !v?.id) return;
      const next = !v.red_team_exploitable;
      try {
        await this.api('PUT', `/projects/${v.project_id}/vulns/${v.id}/red-team-exploitable`, { value: next });
        v.red_team_exploitable = next;
        v.red_team_exploitable_at = next ? new Date().toISOString() : null;
        await this.loadVulnerabilityPage();
        await this.loadDashboardSummary?.();
        this.showToast(next ? '已标记红队可利用' : '已取消红队可利用');
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async deleteVuln(v) {
      if (!confirm(`删除 ${v.title}？`)) return;
      try {
        await this.api('DELETE', `/projects/${v.project_id}/vulns/${v.id}`);
        await Promise.all([this.loadVulnerabilityPage(), this.loadDashboardSummary?.()]);
        this.showToast('已删除');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    openVulnStatusQuick(v) {
      if (!v?.project_id || !v?.id) return;
      this.vulnStatusQuick = {
        open: true,
        project_id: v.project_id,
        id: v.id,
        title: v.title || '',
        status: v.status || '待验证',
        selected: v.status || '待验证',
        reason: '',
        reasonError: '',
      };
    },

    pickVulnStatus(s) {
      this.vulnStatusQuick.selected = s;
      if (s !== '忽略') {
        this.vulnStatusQuick.reason = '';
        this.vulnStatusQuick.reasonError = '';
      }
    },

    async confirmVulnStatus() {
      const s = this.vulnStatusQuick.selected;
      if (s === '忽略') {
        const reason = (this.vulnStatusQuick.reason || '').trim();
        if (reason.length < 4) {
          this.vulnStatusQuick.reasonError = '忽略原因不少于 4 个字';
          return;
        }
        await this.saveVulnStatusQuick('忽略', reason);
      } else {
        await this.saveVulnStatusQuick(s);
      }
    },

    closeVulnStatusQuick() {
      this.vulnStatusQuick.open = false;
    },

    async saveVulnStatusQuick(nextStatus, reason) {
      const quick = this.vulnStatusQuick;
      const current = this.allVulnerabilities().find(
        (item) => item.project_id === quick.project_id && item.id === quick.id,
      );
      if (!current) {
        this.showToast('未找到漏洞记录', 'error');
        return;
      }
      const body = { status: nextStatus, status_reason: (reason || '').trim() };
      try {
        await this.api('PUT', `/projects/${current.project_id}/vulns/${current.id}`, body);
        this.closeVulnStatusQuick();
        await this.loadVulnerabilityPage();
        await this.loadDashboardSummary?.();
        this.showToast(`状态已更新为 ${nextStatus}`);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async openVulnerabilityArtifact(v, artifact) {
      if (!v?.project_id || !artifact?.id) return;
      await this.openArtifactLocation(v.project_id, artifact);
    },

    async exportSingleVulnerability(v) {
      if (!v?.task_id || !v?.id) return;
      try {
        const token = localStorage.getItem('axiom_token');
        const headers = token ? { 'Authorization': `Bearer ${token}` } : {};
        const res = await fetch(`/api/v1/tasks/${v.task_id}/vulnerabilities/${v.id}/export?format=zip`, {
          method: 'GET',
          headers
        });
        if (!res.ok) {
          const errText = await res.text();
          throw new Error(errText || `导出失败 (HTTP ${res.status})`);
        }
        const blob = await res.blob();
        const downloadUrl = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = downloadUrl;
        a.download = `${v.task_id}_${v.id}_export.zip`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(downloadUrl);
        this.showToast(`已成功导出 ${v.id} 漏洞完整证据包`, 'success');
      } catch (e) {
        this.showToast(`导出失败: ${e.message}`, 'error');
      }
    },
  }
};
