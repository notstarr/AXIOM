window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.mcp = {
  methods: {
    async loadMcpPage() {
      try {
        const [servers, workers] = await Promise.all([
          this.api('GET', '/mcp/servers'),
          this.api('GET', '/mcp/workers'),
        ]);
        this.mcpServers = (servers || []).map(item => ({ ...item, _toolQuery: '', _discovering: false }));
        this.mcpWorkers = (workers || []).map(item => ({
          ...item,
          worker: item.worker || item.worker_name,
          _toolsText: (item.allowed_tools || []).join(', '),
        }));
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    mcpTransportLabel(server) {
      if (server.url) return server.transport === 'sse' ? 'SSE' : 'HTTP';
      return 'stdio';
    },
    mcpServerCommand(server) {
      if (server.url) return server.url;
      return [server.command, ...(server.args || [])].join(' ').trim();
    },
    mcpDiscoveryLabel(server) {
      return ({ ready: '已发现', error: '发现失败', discovering: '发现中', pending: '待发现' })[server.discovery_status] || '待发现';
    },
    mcpDiscoveryClass(server) {
      return ({ ready: 'bg-emerald-50 text-emerald-600', error: 'bg-rose-50 text-rose-600', discovering: 'bg-blue-50 text-blue-600', pending: 'bg-amber-50 text-amber-600' })[server.discovery_status] || 'bg-slate-100 text-slate-500';
    },
    mcpFilteredTools(server) {
      const query = String(server._toolQuery || '').trim().toLowerCase();
      if (!query) return server.tools || [];
      return (server.tools || []).filter(tool => [tool.name, tool.title, tool.description].some(value => String(value || '').toLowerCase().includes(query)));
    },
    mcpSchemaType(schema) {
      if (!schema || typeof schema !== 'object') return 'any';
      if (Array.isArray(schema.type)) return schema.type.join(' | ');
      if (schema.type === 'array') return `array<${this.mcpSchemaType(schema.items || {})}>`;
      if (schema.type === 'object' && schema.properties) return 'object';
      if (schema.type) return schema.type;
      const variants = schema.anyOf || schema.oneOf;
      if (Array.isArray(variants)) return variants.map(item => this.mcpSchemaType(item)).join(' | ');
      if (Array.isArray(schema.enum)) return schema.enum.map(value => JSON.stringify(value)).join(' | ');
      return 'any';
    },
    mcpToolParameters(tool) {
      const schema = tool.input_schema || {};
      const properties = schema.properties && typeof schema.properties === 'object' ? schema.properties : {};
      const required = new Set(Array.isArray(schema.required) ? schema.required : []);
      return Object.entries(properties).map(([name, value]) => ({
        name, required: required.has(name), type: this.mcpSchemaType(value),
        description: value?.description || '', hasDefault: value && Object.prototype.hasOwnProperty.call(value, 'default'),
        defaultValue: value?.default,
      }));
    },
    async discoverMcpTools(server, quiet = false) {
      server._discovering = true;
      try {
        const updated = await this.api('POST', `/mcp/servers/${server.id}/discover`);
        Object.assign(server, updated);
        if (!quiet) this.showToast(updated.discovery_status === 'ready' ? `已发现 ${updated.tools.length} 个工具` : `工具发现失败：${updated.discovery_error}`, updated.discovery_status === 'ready' ? 'success' : 'error');
        return updated;
      } catch (error) {
        if (!quiet) this.showToast(error.message, 'error');
        return null;
      } finally { server._discovering = false; }
    },
    async toggleMcpTool(server, tool) {
      const next = !tool.enabled;
      try {
        const updated = await this.api('PATCH', `/mcp/servers/${server.id}/tools/${tool.id}`, { enabled: next });
        Object.assign(tool, updated);
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    openMcpEditor(server = null) {
      this.mcpServerEditor = {
        id: server?.id || '', name: server?.name || '', command: server?.command || '',
        url: server?.url || '', args: JSON.stringify(server?.args || [], null, 2),
        env: JSON.stringify(server?.env || {}, null, 2), disabled: !!server?.disabled,
      };
      this.showMcpEditor = true;
    },
    async saveMcpEditor() {
      const form = this.mcpServerEditor;
      let args, env;
      try {
        args = JSON.parse(form.args || '[]'); env = JSON.parse(form.env || '{}');
        if (!Array.isArray(args) || !env || Array.isArray(env)) throw new Error('args 必须是数组，env 必须是对象');
      } catch (error) { this.showToast(`配置格式错误：${error.message}`, 'error'); return; }
      const body = { name: form.name.trim(), command: form.command.trim(), url: form.url.trim(), args, env, disabled: !!form.disabled, source_format: 'json', raw_config: {} };
      try {
        let saved;
        if (form.id) {
          saved = await this.api('PUT', `/mcp/servers/${form.id}`, body);
          saved = await this.api('POST', `/mcp/servers/${saved.id}/discover`);
        } else saved = await this.api('POST', '/mcp/servers', body);
        this.showMcpEditor = false; await this.loadMcpPage();
        this.showToast(saved.discovery_status === 'ready' ? `MCP Server 已保存，发现 ${saved.tools.length} 个工具` : `Server 已保存，但工具发现失败：${saved.discovery_error}`, saved.discovery_status === 'ready' ? 'success' : 'error');
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    async importMcpConfig() {
      if (!this.mcpImportContent.trim()) return this.showToast('请粘贴 MCP 配置', 'error');
      this.mcpImportBusy = true;
      try {
        const result = await this.api('POST', '/mcp/import', { format: this.mcpImportFormat, content: this.mcpImportContent, replace: false });
        this.mcpImportContent = ''; await this.loadMcpPage();
        const ready = result.filter(item => item.discovery_status === 'ready').length;
        const failed = result.filter(item => item.discovery_status === 'error').length;
        this.showToast(`已导入 ${result.length} 个 Server；${ready} 个发现成功${failed ? `，${failed} 个发现失败` : ''}`, failed ? 'error' : 'success');
      } catch (error) { this.showToast(error.message, 'error'); }
      finally { this.mcpImportBusy = false; }
    },
    async toggleMcpPageServer(server) {
      try {
        await this.api('PUT', `/mcp/servers/${server.id}`, { name: server.name, command: server.command, url: server.url, args: server.args, env: server.env, disabled: !server.disabled, source_format: server.source_format, raw_config: {} });
        await this.loadMcpPage();
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    async deleteMcpPageServer(server) {
      if (!confirm(`删除 MCP Server「${server.name}」？`)) return;
      try { await this.api('DELETE', `/mcp/servers/${server.id}`); await this.loadMcpPage(); this.showToast('已删除'); }
      catch (error) { this.showToast(error.message, 'error'); }
    },
    mcpWorkerHasServer(worker, server) {
      return (worker.server_names || []).includes(server.name);
    },
    async toggleMcpWorkerServer(worker, server) {
      const selected = new Set(worker.server_names || []);
      selected.has(server.name) ? selected.delete(server.name) : selected.add(server.name);
      const ids = this.mcpServers.filter(item => selected.has(item.name)).map(item => item.id);
      try {
        const updated = await this.api('PUT', `/mcp/workers/${encodeURIComponent(worker.worker)}`, { enabled: worker.enabled, server_ids: ids, allowed_tools: worker.allowed_tools || [] });
        Object.assign(worker, updated); this.showToast('Worker 的 MCP 绑定已更新');
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    async toggleMcpWorker(worker) {
      const ids = this.mcpServers.filter(item => (worker.server_names || []).includes(item.name)).map(item => item.id);
      try { Object.assign(worker, await this.api('PUT', `/mcp/workers/${encodeURIComponent(worker.worker)}`, { enabled: !worker.enabled, server_ids: ids, allowed_tools: worker.allowed_tools || [] })); }
      catch (error) { this.showToast(error.message, 'error'); }
    },
    async saveMcpWorkerTools(worker) {
      try { Object.assign(worker, await this.api('PUT', `/mcp/workers/${encodeURIComponent(worker.worker)}`, { enabled: worker.enabled, server_ids: this.mcpServers.filter(item => (worker.server_names || []).includes(item.name)).map(item => item.id), allowed_tools: String(worker._toolsText || '').split(/[\n,]+/).map(item => item.trim()).filter(Boolean) })); this.showToast('工具权限已保存'); }
      catch (error) { this.showToast(error.message, 'error'); }
    },
  },
};
