window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.helpers = {
  methods: {
    cloneData(value) {
      try {
        return JSON.parse(JSON.stringify(value));
      } catch (error) {
        if (typeof structuredClone === 'function') return structuredClone(value);
        throw error;
      }
    },

    isValidLayoutMode(mode) {
      return ['dagre_tb', 'dagre_lr', 'elk_tb', 'elk_lr'].includes(mode);
    },

    layoutEngine(mode = this.layoutMode) {
      if (mode.startsWith('elk')) return 'elk';
      return mode.startsWith('elk') ? 'elk' : 'dagre';
    },

    layoutDirection(mode = this.layoutMode) {
      return mode.endsWith('_lr') ? 'LR' : 'TB';
    },

    actorName() {
      return '人工';
    },

    projectStatusBadgeClass(status) {
      const classes = {
        active: 'bg-teal-50 text-teal-600',
        stopped: 'bg-amber-50 text-amber-700',
        completed: 'bg-slate-100 text-slate-500',
      };
      return classes[status] || 'bg-slate-100 text-slate-500';
    },

    projectStatusLabel(status) {
      const labels = {
        active: '运行中',
        stopped: '已暂停',
        completed: '已完成',
      };
      return labels[status] || status || '未知';
    },

    reasonBadgeText(reason, compact = false) {
      if (!reason) return '';
      if (compact) return `reason · ${reason.worker}`;
      const trigger = reason.trigger ? `（${reason.trigger}）` : '';
      return `reason 中 · ${reason.worker}${trigger}`;
    },

    workingIntentBadgeText(count) {
      return `explore 中 · ${count}`;
    },

    isBootstrapIntent(intent) {
      return Boolean(
        intent
        && intent.description === 'bootstrap'
        && intent.creator === 'dispatcher.bootstrap'
        && Array.isArray(intent.from)
        && intent.from.length === 1
        && intent.from[0] === 'origin'
        && intent.to === null,
      );
    },

    intentLifecycleState(intent) {
      const status = String(intent?.status || '').trim().toLowerCase();
      const states = {
        completed: 'completed',
        done: 'completed',
        concluded: 'completed',
        cancelled: 'cancelled',
        canceled: 'cancelled',
        failed: 'failed',
        rejected: 'failed',
        retry_exhausted: 'failed',
        claimed: 'running',
        running: 'running',
        concluding: 'running',
        in_progress: 'running',
        working: 'running',
        active: 'running',
        pending: 'pending',
        queued: 'pending',
      };
      if (states[status]) return states[status];

      // Only legacy records without a known lifecycle status may fall back to
      // graph/worker fields. A retained worker never makes a V2 pending or
      // completed Intent active again.
      if (intent?.to || intent?.concluded_at) return 'completed';
      if (!status && intent?.worker) return 'running';
      return 'pending';
    },

    openIntentNodeType(intent) {
      const state = this.intentLifecycleState(intent);
      if (state === 'completed') return 'intent_completed';
      if (state === 'cancelled') return 'intent_cancelled';
      if (state === 'failed') return 'intent_failed';
      if (this.isBootstrapIntent(intent)) return state === 'running' ? 'bootstrap_running' : 'bootstrap_pending';
      return state === 'running' ? 'in_progress' : 'unclaimed';
    },

    openIntentNodeLabel(intent) {
      const state = this.intentLifecycleState(intent);
      if (state === 'completed') return '✓';
      if (state === 'cancelled') return '×';
      if (state === 'failed') return '!';
      if (this.isBootstrapIntent(intent)) return state === 'running' ? '运行中' : '等待中';
      return state === 'running' ? '…' : '?';
    },

    openIntentNodeSize(intent) {
      const nodeType = this.openIntentNodeType(intent);
      if (['bootstrap_pending', 'bootstrap_running'].includes(nodeType)) return { width: 92, height: 48 };
      if (nodeType === 'in_progress') return { width: 26, height: 26 };
      if (nodeType.startsWith('intent_')) return { width: 24, height: 24 };
      return { width: 22, height: 22 };
    },

    intentDotClass(i) {
      const state = this.intentLifecycleState(i);
      if (state === 'completed') return 'bg-teal-400';
      if (state === 'cancelled') return 'bg-slate-300';
      if (state === 'failed') return 'bg-rose-400';
      if (this.isBootstrapIntent(i)) return state === 'running' ? 'bg-orange-400' : 'bg-orange-200';
      if (state === 'running') return 'bg-amber-400';
      if (i.next_retry_at || i.last_failure_kind || i.retry_count || i.infrastructure_retry_count) return 'bg-amber-300';
      return 'bg-slate-300';
    },
    intentStatusClass(i) {
      const state = this.intentLifecycleState(i);
      if (state === 'completed') return 'text-teal-600';
      if (state === 'cancelled') return 'text-slate-500';
      if (state === 'failed') return 'text-rose-600';
      if (this.isBootstrapIntent(i)) return state === 'running' ? 'text-orange-600' : 'text-orange-400';
      if (state === 'running') return 'text-amber-600';
      if (i.next_retry_at || i.last_failure_kind || i.retry_count || i.infrastructure_retry_count) return 'text-amber-600';
      return 'text-slate-400';
    },
    intentStatusLabel(i) {
      const state = this.intentLifecycleState(i);
      if (state === 'completed') return '已完成';
      if (state === 'cancelled') return '已取消';
      if (state === 'failed' && i.last_failure_kind === 'retry_exhausted') return '失败·待 Reason 重规划';
      if (state === 'failed') return '执行失败';
      if (state === 'running') {
        if (this.isBootstrapIntent(i)) return 'Bootstrap 运行中';
        return '进行中';
      }
      if (i.next_retry_at || i.last_failure_kind || i.retry_count || i.infrastructure_retry_count) return '等待重试';
      if (this.isBootstrapIntent(i)) return 'Bootstrap 等待中';
      return '等待 Worker';
    },

    vulnStatusClass(status) {
      const key = this.vulnStatusKey(status);
      if (key === 'verified') return 'bg-brand-100 text-brand-700';
      if (key === 'pending') return 'bg-amber-100 text-amber-700';
      if (key === 'duplicate') return 'bg-rose-100 text-rose-700';
      if (key === 'ignored') return 'bg-slate-100 text-slate-600';
      return 'bg-slate-100 text-slate-600';
    },

    vulnStatusLabel(status) {
      const labels = {
        pending: '待验证',
        verified: '已验证',
        ignored: '忽略',
        duplicate: '重复',
      };
      const key = this.vulnStatusKey(status);
      return labels[key] || status || '未知';
    },

    vulnStatusKey(status) {
      const raw = String(status || '').trim();
      const lower = raw.toLowerCase();
      if (raw === '待验证' || raw === '待确认' || raw === '待审核' || lower === 'pending') return 'pending';
      if (raw === '已验证' || raw === '已确认' || lower === 'confirmed') return 'verified';
      if (raw === '忽略' || raw === '已忽略' || lower === 'ignored') return 'ignored';
      if (raw === '重复' || raw === '已拒绝' || lower === 'rejected') return 'duplicate';
      return lower || '';
    },

    vulnSeverityClass(severity) {
      const key = String(severity || '').toLowerCase();
      if (key === 'critical') return 'bg-red-100 text-red-700';
      if (key === 'high') return 'bg-orange-100 text-orange-700';
      if (key === 'medium') return 'bg-yellow-100 text-yellow-700';
      if (key === 'low') return 'bg-blue-100 text-blue-700';
      if (key === 'info') return 'bg-slate-100 text-slate-600';
      return 'bg-slate-100 text-slate-600';
    },

    vulnSeverityLabel(severity) {
      const labels = {
        critical: '严重',
        high: '高危',
        medium: '中危',
        low: '低危',
        info: '信息',
      };
      const key = String(severity || '').toLowerCase();
      return labels[key] || severity || '未知';
    },

    vulnerabilityKey(vuln) {
      return `${vuln?.project_id || ''}:${vuln?.id || ''}`;
    },

    formatTime(ts) { if (!ts) return ''; return new Date(ts).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}); },
    formatDate(ts) { if (!ts) return ''; const d = new Date(ts); return d.toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); },
    formatShortDate(ts) { if (!ts) return ''; const d = new Date(ts); return d.toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}); },
    formatTimelineDate(ts) { if (!ts) return ''; return new Date(ts).toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}); },
    // 大模型输出常把换行写成字面量 "\n" 存进 DB;前端 CSS `whitespace-pre-wrap`
    // 只认真换行,需要先把字面 `\n`/`\\n` 归一化成真换行,排版才正常。
    formatMultiline(text) {
      if (text === null || text === undefined) return '';
      // 大模型输出常把换行写成字面量 "\r\n"/"\n"(两个字符:反斜杠+n)存进 DB;
      // CSS whitespace-pre-wrap 只认真换行,需先归一化成真换行。
      return String(text).replace(/\\r\\n|\\n/g, '\n');
    },
    formatDurationMs(ms) {
      const totalSeconds = Math.max(0, Math.round(ms / 1000));
      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const seconds = totalSeconds % 60;
      if (hours > 0) return `${hours}小时 ${minutes}分`;
      if (minutes > 0) return `${minutes}分 ${seconds}秒`;
      return `${seconds}秒`;
    },

    _normalizeTokenUsage(raw) {
      if (!raw || typeof raw !== 'object') return null;
      // Support both pi format (input/output/totalTokens) and OpenAI format (input_tokens/output_tokens)
      const input = Number(raw.input_tokens ?? raw.input ?? 0);
      const output = Number(raw.output_tokens ?? raw.output ?? 0);
      const total = Number(raw.total_tokens ?? raw.totalTokens ?? (input + output));
      if (!Number.isFinite(input) || !Number.isFinite(output) || !Number.isFinite(total)) return null;
      if (input < 0 || output < 0 || total < 0) return null;
      return {
        input: Math.floor(input),
        output: Math.floor(output),
        total: Math.floor(total),
      };
    },

    extractTokenUsage(payload) {
      if (!payload || typeof payload !== 'object') return null;
      return this._normalizeTokenUsage(payload.token_usage || payload.usage || payload);
    },

    _formatTokenPair(usage) {
      if (!usage) return '—';
      return `输入 ${usage.input.toLocaleString()} / 输出 ${usage.output.toLocaleString()} / 总计 ${usage.total.toLocaleString()}`;
    },

    escapeHtml(text) {
      return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    },

    parseJsonObjectText(text) {
      const raw = (text || '').trim();
      if (!raw) return {};
      try {
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
      } catch (e) {
        this.showToast('额外请求头必须是 JSON 对象', 'error');
        throw e;
      }
    },

    linesFromText(text) {
      return String(text || '').split('\n').map(item => item.trim()).filter(Boolean);
    },

    downloadTextFile(filename, text) {
      const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },

    async copyText(text) {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        return;
      }
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.top = '0';
      textarea.style.left = '-9999px';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      textarea.setSelectionRange(0, textarea.value.length);
      try {
        if (!document.execCommand('copy')) {
          throw new Error('copy command rejected');
        }
      } finally {
        document.body.removeChild(textarea);
      }
    },

    highlightYamlScalar(value) {
      const escaped = this.escapeHtml(value);
      if (!value.trim()) return escaped;
      if (/^\s*#.*$/.test(value)) return `<span style="color:#64748b">${escaped}</span>`;
      if (/^\s*['"].*['"]\s*$/.test(value)) return `<span style="color:#15803d">${escaped}</span>`;
      if (/^\s*\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})\s*$/.test(value)) return `<span style="color:#b45309">${escaped}</span>`;
      if (/^\s*(true|false|null|~)\s*$/i.test(value)) return `<span style="color:#b91c1c">${escaped}</span>`;
      if (/^\s*-?\d+(\.\d+)?\s*$/.test(value)) return `<span style="color:#0f766e">${escaped}</span>`;
      if (/^\s*(origin|goal|f\d+|i\d+)\s*$/i.test(value)) return `<span style="color:#6d28d9">${escaped}</span>`;
      return `<span style="color:#0f172a">${escaped}</span>`;
    },

    highlightYamlLine(line) {
      if (/^\s*$/.test(line)) return '';
      if (/^\s*#/.test(line)) return `<span style="color:#64748b">${this.escapeHtml(line)}</span>`;

      const listKeyMatch = line.match(/^(\s*-\s+)([^:#\n][^:]*):(.*)$/);
      if (listKeyMatch) {
        const [, prefix, key, rest] = listKeyMatch;
        return `${this.escapeHtml(prefix)}<span style="color:#7dd3fc">${this.escapeHtml(key)}</span>:${this.highlightYamlScalar(rest)}`;
      }

      const keyMatch = line.match(/^(\s*)([^:#\n][^:]*):(.*)$/);
      if (keyMatch) {
        const [, indent, key, rest] = keyMatch;
        return `${this.escapeHtml(indent)}<span style="color:#7dd3fc">${this.escapeHtml(key)}</span>:${this.highlightYamlScalar(rest)}`;
      }

      const listValueMatch = line.match(/^(\s*-\s+)(.*)$/);
      if (listValueMatch) {
        const [, prefix, rest] = listValueMatch;
        return `${this.escapeHtml(prefix)}${this.highlightYamlScalar(rest)}`;
      }

      return this.highlightYamlScalar(line);
    },

    yamlSectionTint(sectionName) {
      const tints = {
        project: { header: 'var(--status-info-bg)', body: 'var(--surface-card)', itemA: 'var(--surface-inset)', itemB: 'var(--surface-hover)' },
        hints: { header: 'var(--status-warning-bg)', body: 'var(--surface-card)', itemA: 'var(--surface-inset)', itemB: 'var(--surface-hover)' },
        facts: { header: 'var(--status-purple-bg)', body: 'var(--surface-card)', itemA: 'var(--surface-inset)', itemB: 'var(--surface-hover)' },
        intents: { header: 'var(--status-success-bg)', body: 'var(--surface-card)', itemA: 'var(--surface-inset)', itemB: 'var(--surface-hover)' },
      };
      return tints[sectionName] || { header: 'var(--surface-hover)', body: 'var(--surface-card)', itemA: 'var(--surface-inset)', itemB: 'var(--surface-hover)' };
    },

    highlightYaml(text) {
      const lines = String(text ?? '').split('\n');
      let currentSection = '';
      let activeItemIndent = null;
      let activeItemStripe = 0;
      let nextItemStripe = 0;

      return lines.map((line) => {
        const topLevelMatch = line.match(/^([A-Za-z_][A-Za-z0-9_-]*):\s*$/);
        if (topLevelMatch) {
          currentSection = topLevelMatch[1];
          activeItemIndent = null;
          activeItemStripe = 0;
          nextItemStripe = 0;
        }

        const tint = this.yamlSectionTint(currentSection);
        const indent = (line.match(/^(\s*)/) || ['',''])[1].length;
        const isBlank = /^\s*$/.test(line);
        const isListItem = /^(\s*)-\s+/.test(line);
        const isTopLevelItem = isListItem && indent === 0;

        if (isTopLevelItem) {
          activeItemIndent = indent;
          activeItemStripe = nextItemStripe;
          nextItemStripe = nextItemStripe === 0 ? 1 : 0;
        } else if (!isBlank && activeItemIndent !== null && indent === 0) {
          activeItemIndent = null;
        }

        const isNestedListItem = isListItem && activeItemIndent !== null && indent > activeItemIndent;
        const isItemContinuation = !isListItem && !isBlank && activeItemIndent !== null && indent > activeItemIndent;
        const isBlankWithinItem = isBlank && activeItemIndent !== null;
        const isItemLine = isTopLevelItem || isNestedListItem || isItemContinuation || isBlankWithinItem;
        const lineHtml = isBlank ? '&nbsp;' : this.highlightYamlLine(line);
        let background = currentSection ? tint.body : 'transparent';
        let fontWeight = '400';

        if (topLevelMatch) {
          background = tint.header;
          fontWeight = '700';
        } else if (isItemLine) {
          background = activeItemStripe === 0 ? tint.itemA : tint.itemB;
        }

        return `<div style="white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;padding:0 16px;background:${background};font-weight:${fontWeight};color:var(--text-primary)">${lineHtml}</div>`;
      }).join('');
    },

    highlightTimeline(text) {
      const lines = String(text ?? '').split('\n');
      const stripes = ['var(--surface-inset)', 'var(--surface-hover)'];
      let blockIndex = -1;
      return lines.map((line) => {
        if (/^\[/.test(line)) blockIndex++;
        const bg = blockIndex < 0 ? stripes[0] : stripes[blockIndex % 2];
        const isBlank = /^\s*$/.test(line);
        return `<div style="white-space:pre;padding:0 16px;background:${bg};color:var(--text-primary)">${isBlank ? '&nbsp;' : this.escapeHtml(line)}</div>`;
      }).join('');
    },

    projectLabel(projectId, projectTitle) {
      return projectTitle ? `${projectId} - ${projectTitle}` : projectId;
    },

    methodBadge(method) {
      const m = { GET: 'bg-blue-50 text-blue-600', POST: 'bg-emerald-50 text-emerald-600', PUT: 'bg-amber-50 text-amber-600', DELETE: 'bg-rose-50 text-rose-600', PATCH: 'bg-violet-50 text-violet-600' };
      return m[method] || 'bg-slate-100 text-slate-500';
    },
    statusBadge(status) {
      const s = { safe: 'bg-emerald-50 text-emerald-600', blocked: 'bg-slate-100 text-slate-500', vulnerable: 'bg-rose-50 text-rose-600', untested: 'bg-amber-50 text-amber-600' };
      return s[status] || 'bg-slate-100 text-slate-500';
    },

    pageTypeColor(type) {
      return {
        list: 'bg-blue-50 text-blue-500',
        detail: 'bg-violet-50 text-violet-500',
        article: 'bg-violet-50 text-violet-500',
        form: 'bg-amber-50 text-amber-500',
        search: 'bg-sky-50 text-sky-500',
        auth: 'bg-rose-50 text-rose-500',
        landing: 'bg-emerald-50 text-emerald-500',
      }[type] || 'bg-slate-100 text-slate-400';
    },
  }
};
