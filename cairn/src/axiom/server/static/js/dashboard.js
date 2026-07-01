window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.dashboard = {
  methods: {
    dashboardStats() {
      const details = this.allProjectDetails();
      const vulns = this.allVulnerabilities();
      const events = Object.values(this.projectEventsCache || {}).flat();
      const tokenStats = events.reduce((acc, event) => {
        const usage = this.extractTokenUsage(event.payload || {});
        if (!usage) return acc;
        acc.input += usage.input;
        acc.output += usage.output;
        acc.total += usage.total;
        return acc;
      }, { input: 0, output: 0, total: 0 });
      const intents = details.flatMap(detail => detail?.intents || []);
      return {
        runningProjects: this.countProjectsByStatus('active'),
        pausedProjects: this.countProjectsByStatus('stopped'),
        completedProjects: this.countProjectsByStatus('completed'),
        vulnTotal: vulns.length,
        criticalVulns: vulns.filter(v => String(v.severity || '').toLowerCase() === 'critical').length,
        highVulns: vulns.filter(v => String(v.severity || '').toLowerCase() === 'high').length,
        workingIntents: intents.filter(intent => intent.worker && !intent.concluded_at).length,
        unclaimedIntents: intents.filter(intent => !intent.worker && !intent.concluded_at).length,
        eventTotal: events.length,
        tokenInput: tokenStats.input,
        tokenOutput: tokenStats.output,
        tokenTotal: tokenStats.total,
      };
    },

    // 按任务统计 Token 消耗，按总量降序
    tokenByProject() {
      const cache = this.projectEventsCache || {};
      const totalAll = Object.values(cache).flat().reduce((sum, e) => {
        const u = this.extractTokenUsage(e.payload || {});
        return sum + (u ? u.total : 0);
      }, 0);
      const rows = Object.entries(cache).map(([projectId, events]) => {
        const usage = (events || []).reduce((acc, e) => {
          const u = this.extractTokenUsage(e.payload || {});
          if (!u) return acc;
          acc.input += u.input;
          acc.output += u.output;
          acc.total += u.total;
          return acc;
        }, { input: 0, output: 0, total: 0 });
        if (usage.total === 0) return null;
        const proj = this.projects.find(p => p.id === projectId);
        return {
          id: projectId,
          title: proj?.title || projectId,
          status: proj?.status || '',
          input: usage.input,
          output: usage.output,
          total: usage.total,
          pct: totalAll > 0 ? Math.round(usage.total / totalAll * 100) : 0,
        };
      }).filter(Boolean);
      rows.sort((a, b) => b.total - a.total);
      return rows;
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
      return Object.entries(this.projectEventsCache || {})
        .flatMap(([projectId, events]) => (events || []).map(event => ({ ...event, project_id: projectId })))
        .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
        .slice(0, 8);
    },

    placeholderPages() {
      return [
        { id: 'mcp', title: 'MCP 集成', subtitle: '外部能力接入预留', description: '后续将在这里配置 MCP Server、能力授权、连接状态和调用审计。' },
      ];
    },

    settingsTabs() {
      return [
        { id: 'workers', title: 'Worker 管理', description: 'PI worker、模型、密钥' },
        { id: 'config', title: '配置管理', description: '并发、超时、健康检查' },
        { id: 'proxy', title: '代理', description: 'SOCKS5 和认证' },
        { id: 'crawler', title: '爬虫 Worker', description: '爬虫专用模型配置' },
      ];
    },

    allVulnerabilities() {
      return this.allProjectDetails().flatMap((detail) => {
        const projectId = detail?.project?.id || '';
        const projectTitle = detail?.project?.title || projectId;
        const parentProjectId = detail?.project?.parent_project_id || '';
        return (detail?.vulns || []).map(vuln => ({
          ...vuln,
          project_id: projectId,
          project_title: projectTitle,
          task_id: projectId,
          task_title: projectTitle,
          parent_project_id: parentProjectId,
          project_group_title: this.projectGroupTitle(parentProjectId),
        }));
      });
    },

    filteredVulnerabilities() {
      const keyword = (this.vulnFilter.keyword || '').trim().toLowerCase();
      const projectId = this.vulnFilter.projectId;
      return this.allVulnerabilities().filter((vuln) => {
        if (projectId && vuln.project_id !== projectId) return false;
        if (!keyword) return true;
        return [vuln.title, vuln.description, vuln.prerequisites, vuln.impact, vuln.project_title, vuln.project_group_title]
          .some(value => String(value || '').toLowerCase().includes(keyword));
      });
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
    openVulnEditor(v) {
      if (v) {
        this.vulnEditor = { open: true, isNew: false, project_id: v.project_id, id: v.id, title: v.title, description: v.description, prerequisites: v.prerequisites || '', impact: v.impact, evidence: (v.evidence||[]).join('\n'), reproduction_steps: (v.reproduction_steps||[]).join('\n'), severity: v.severity || 'medium', status: v.status };
      } else {
        this.vulnEditor = { open: true, isNew: true, project_id: '', id: '', title: '', description: '', prerequisites: '', impact: '', evidence: '', reproduction_steps: '', severity: 'medium', status: '待确认' };
      }
    },

    async saveVuln() {
      const ed = this.vulnEditor;
      if (!ed.title || !ed.project_id) { this.showToast('标题和项目必填', 'error'); return; }
      const body = { title: ed.title, severity: ed.severity || 'medium', description: ed.description, prerequisites: ed.prerequisites, impact: ed.impact, evidence: ed.evidence.split('\n').filter(Boolean), reproduction_steps: ed.reproduction_steps.split('\n').filter(Boolean), status: ed.status, source: 'manual', reported_by: this.actorName() };
      try {
        if (ed.isNew) {
          await this.api('POST', `/projects/${ed.project_id}/vulns`, body);
        } else {
          await this.api('PUT', `/projects/${ed.project_id}/vulns/${ed.id}`, body);
        }
        this.vulnEditor.open = false;
        await this.loadAllProjectDetails(true);
        this.showToast(ed.isNew ? '漏洞已创建' : '漏洞已更新');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    editVuln(v) { this.openVulnEditor(v); },

    async deleteVuln(v) {
      if (!confirm(`删除 ${v.title}？`)) return;
      try {
        await this.api('DELETE', `/projects/${v.project_id}/vulns/${v.id}`);
        await this.loadAllProjectDetails(true);
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
        status: v.status || '待确认',
      };
    },

    closeVulnStatusQuick() {
      this.vulnStatusQuick.open = false;
    },

    async saveVulnStatusQuick(nextStatus) {
      const quick = this.vulnStatusQuick;
      const current = this.allVulnerabilities().find(
        (item) => item.project_id === quick.project_id && item.id === quick.id,
      );
      if (!current) {
        this.showToast('未找到漏洞记录', 'error');
        return;
      }
      const body = {
        title: current.title,
        type: current.type || '',
        severity: current.severity || 'medium',
        description: current.description || '',
        prerequisites: current.prerequisites || '',
        impact: current.impact || '',
        evidence: Array.isArray(current.evidence) ? current.evidence : [],
        reproduction_steps: Array.isArray(current.reproduction_steps) ? current.reproduction_steps : [],
        recommendation: current.recommendation || '',
        affected_scope: current.affected_scope || '',
        status: nextStatus,
        source: current.source || 'manual',
        reported_by: current.reported_by || this.actorName(),
      };
      try {
        await this.api('PUT', `/projects/${current.project_id}/vulns/${current.id}`, body);
        this.closeVulnStatusQuick();
        await this.loadAllProjectDetails(true);
        this.showToast(`状态已更新为 ${nextStatus}`);
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    async exportVulnMd(v) {
      if (!v?.project_id || !v?.id) return;
      try {
        const text = await this.fetchText(`/projects/${v.project_id}/vulns/${v.id}/export`);
        const filename = `${v.id}_${(v.title || 'vuln').replace(/[^\w\u4e00-\u9fff]/g, '_').slice(0, 40)}.md`;
        this.downloadTextFile(filename, text);
        this.showToast('已导出');
      } catch(e) { this.showToast(e.message, 'error'); }
    },
  }
};
