window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.files = {
  methods: {
    async loadFileTree() {
      this.fileGroups = [];
      if (this.projectGroups.length === 0) await this.loadProjectGroups();
      if (this.projects.length === 0) await this.loadProjects();
      const map = {};
      for (const p of this.projects) {
        const gid = p.parent_project_id || 'unknown';
        if (!map[gid]) map[gid] = [];
        map[gid].push(p);
      }
      // Build group nodes, check if group-level crawler/ exists
      const groups = [];
      for (const g of (this.projectGroups || [])) {
        // Check group-level crawler
        let hasCrawler = false;
        try {
          const r = await fetch(`/file-groups/${g.id}/crawler`);
          if (r.ok) hasCrawler = true;
        } catch(e) {}
        // Build task nodes, check which subfolders exist
        const tasks = [];
        for (const p of (map[g.id] || [])) {
          let folders = [];
          try {
            const r = await fetch(`/files/${p.id}`);
            if (r.ok) {
              const d = await r.json();
              if ((d.human || []).length > 0) folders.push('human');
              if ((d.ai || []).length > 0) folders.push('ai');
              // Always show human and ai as folders (even if empty) to allow upload
              if (!folders.includes('human')) folders.push('human');
              if (!folders.includes('ai')) folders.push('ai');
            }
          } catch(e) { folders = ['human', 'ai']; }
          tasks.push({ id: p.id, title: p.title, folders });
        }
        groups.push({ id: g.id, title: g.title, hasCrawler, tasks });
      }
      this.fileGroups = groups;
    },

    async selectFolder(groupId, taskId, folder, isGroupLevel) {
      this.fileGroupId = groupId;
      this.fileTaskId = taskId;
      this.fileFolder = folder;
      this.fileIsGroupLevel = isGroupLevel;
      this.fileList = [];
      this.expandedDirs = {};
      this.filePreview = { open: false, name: '', path: '', content: '', isMarkdown: false, isImage: false, rawUrl: '', rendered: '' };
      try {
        if (isGroupLevel) {
          const r = await fetch(`/file-groups/${groupId}/${folder}`);
          if (r.ok) { const d = await r.json(); this.fileList = d.files || []; }
        } else {
          const r = await fetch(`/files/${taskId}`);
          if (r.ok) { const d = await r.json(); this.fileList = d[folder] || []; }
        }
      } catch(e) {}
    },

    // Keep selectFileTask for backward compat (projects page crawler uses different state)
    async selectFileTask(taskId, groupId) {
      await this.selectFolder(groupId, taskId, 'human', false);
    },

    isImageFile(name) {
      return /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i.test(name);
    },
    isMdFile(name) {
      return /\.md$/i.test(name);
    },
    buildFileTree(files) {
      const root = { children: {} };
      for (const f of files) {
        const parts = f.path.split('/');
        let node = root;
        for (let i = 0; i < parts.length; i++) {
          const part = parts[i];
          const isLast = i === parts.length - 1;
          if (isLast) {
            node.children[part] = { name: part, path: f.path, type: 'file', size: f.size };
          } else {
            if (!node.children[part] || node.children[part].type === 'file') {
              node.children[part] = { name: part, path: parts.slice(0, i+1).join('/'), type: 'dir', children: {} };
            }
            node = node.children[part];
          }
        }
      }
      const toArray = (n) => Object.values(n.children).map(c => c.type === 'dir' ? { ...c, children: toArray(c) } : c)
        .sort((a, b) => a.type !== b.type ? (a.type === 'dir' ? -1 : 1) : a.name.localeCompare(b.name));
      return toArray(root);
    },
    flattenFileTree(nodes, depth = 0) {
      const result = [];
      for (const node of nodes) {
        result.push({ ...node, depth });
        if (node.type === 'dir' && this.expandedDirs[node.path]) {
          result.push(...this.flattenFileTree(node.children, depth + 1));
        }
      }
      return result;
    },
    toggleDir(path) {
      this.expandedDirs[path] = !this.expandedDirs[path];
    },
    async previewFile(path, size) {
      const name = path.split('/').pop();
      const isImage = this.isImageFile(name);
      const isMd = this.isMdFile(name);
      const rawUrl = this.fileIsGroupLevel
        ? `/file-groups-raw/${this.fileGroupId}/${this.fileFolder}/${encodeURIComponent(path)}`
        : `/files-raw/${this.fileTaskId}/${this.fileFolder}/${encodeURIComponent(path)}`;
      if (isImage) {
        this.filePreview = { open: true, name, path, content: '', isMarkdown: false, isImage: true, rawUrl, rendered: '' };
        return;
      }
      if (size > 5 * 1024 * 1024) {
        this.filePreview = { open: true, name, path, content: `[文件过大 (${(size/1024/1024).toFixed(1)}MB)，不支持预览]`, isMarkdown: false, isImage: false, rawUrl, rendered: '' };
        return;
      }
      try {
        const url = this.fileIsGroupLevel
          ? `/file-groups/${this.fileGroupId}/${this.fileFolder}/${encodeURIComponent(path)}`
          : `/files/${this.fileTaskId}/${this.fileFolder}/${encodeURIComponent(path)}`;
        const resp = await fetch(url);
        if (resp.ok) {
          const data = await resp.json();
          if (isMd) await this.ensureMarkdownRuntime();
          const rendered = isMd ? this.renderMarkdown(data.content || '') : '';
          this.filePreview = { open: true, name, path, content: data.content, isMarkdown: isMd, isImage: false, rawUrl, rendered };
        }
      } catch(e) {}
    },

    async deleteCurrentFile(path) {
      if (!confirm(`删除 ${path}？`)) return;
      try {
        const url = this.fileIsGroupLevel
          ? `/file-groups/${this.fileGroupId}/${this.fileFolder}/${encodeURIComponent(path)}`
          : `/files/${this.fileTaskId}/${this.fileFolder}/${encodeURIComponent(path)}`;
        await this.api('DELETE', url);
        if (this.filePreview.path === path) {
          this.filePreview = { open: false, name: '', path: '', content: '', isMarkdown: false, isImage: false, rawUrl: '', rendered: '' };
        }
        await this.selectFolder(this.fileGroupId, this.fileTaskId, this.fileFolder, this.fileIsGroupLevel);
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async deleteFile(name) { await this.deleteCurrentFile(name); },

    async uploadFileToTask(event) {
      const file = event.target.files[0];
      if (!file) return;
      const form = new FormData();
      form.append('file', file);
      try {
        const resp = await fetch(`/files/${this.fileTaskId}/upload`, { method: 'POST', body: form });
        if (resp.ok) {
          await this.selectFileTask(this.fileTaskId, this.fileGroupOpen);
        }
      } catch(e) { this.showToast('上传失败', 'error'); }
      event.target.value = '';
    },
  }
};
