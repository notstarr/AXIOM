const SKILL_FILE_PREVIEW_LIMIT = 512 * 1024;

function stripSkillFrontmatter(value) {
  let body = String(value || '').replace(/^\uFEFF/, '');
  while (body.startsWith('---')) {
    const match = body.match(/^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/);
    if (!match || !/^(?:name|description):/m.test(match[1])) break;
    body = body.slice(match[0].length).replace(/^(?:\r?\n)+/, '');
  }
  return body;
}

const SKILL_BINARY_EXTENSIONS = new Set([
  '7z', 'bin', 'class', 'dll', 'doc', 'docx', 'elf', 'exe', 'gif', 'gz',
  'ico', 'jpeg', 'jpg', 'macho', 'mp3', 'mp4', 'o', 'pdf', 'png', 'so',
  'swf', 'tar', 'wav', 'webp', 'xls', 'xlsx', 'zip',
]);

function skillPathParts(path) {
  return String(path || '').replace(/^\/+|\/+$/g, '').split('/').filter(Boolean);
}

window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.skills = {
  methods: {
    async loadSkills() {
      try {
        const openedName = this.skillEditor?.isNew ? '' : (this.skillEditor?.name || '');
        const openedFile = this.skillEditor?.activeFile || '';
        const preserveUnsavedFile = Boolean(this.skillEditor?.fileDirty);
        const [skills] = await Promise.all([
          this.api('GET', '/skills'),
          this.loadPhaseCapabilities?.(),
        ]);
        this.skills = skills || [];
        const refreshed = openedName
          ? this.skills.find(skill => skill.name === openedName)
          : null;
        if (refreshed && this.skillEditor) {
          Object.assign(this.skillEditor, {
            description: refreshed.description || '',
            content: refreshed.content || '',
            builtin: refreshed.builtin,
            enabled: refreshed.enabled,
            source: refreshed.source,
            bundledHash: refreshed.bundled_hash || '',
            effectiveHash: refreshed.effective_hash || '',
            sync_status: refreshed.sync_status || (refreshed.source === 'bundled' ? 'bundled' : 'custom'),
            bundledUpdateAvailable: Boolean(refreshed.bundled_update_available),
          });
          await this.loadSkillFiles();
          if (
            openedFile
            && !preserveUnsavedFile
            && (this.skillFiles || []).some(file => file.path === openedFile)
          ) {
            await this.openSkillFile(openedFile);
          }
        }
      } catch (e) { /* ignore */ }
    },

    selectedSkillCapabilityRecord() {
      if (!this.skillEditor?.name || this.skillEditor?.isNew) return null;
      return this.phaseCapabilityRecord?.('skill', this.skillEditor.name) || null;
    },

    skillCapabilityRecord(skill) {
      return skill?.name ? this.phaseCapabilityRecord?.('skill', skill.name) || null : null;
    },

    async uploadHarFile(event) {
      const file = event.target.files[0];
      if (!file) return;
      this._harFile = file;
      this.newProject.origin = file.name;
      this.showToast(`已选择: ${file.name} (${Math.round(file.size/1024)}KB)，创建任务后自动上传`);
      event.target.value = '';
    },

    async selectSkill(sk) {
      this.skillEditor = { name: sk.name, description: sk.description || '', content: sk.content || '', isNew: false, builtin: sk.builtin, enabled: sk.enabled, source: sk.source, bundledHash: sk.bundled_hash || '', effectiveHash: sk.effective_hash || '', sync_status: sk.sync_status || (sk.source === 'bundled' ? 'bundled' : 'custom'), bundledUpdateAvailable: !!sk.bundled_update_available, activeFile: '', fileContent: '', fileDirty: false, fileTruncated: false };
      this.skillFileTree = [];
      this.skillExpandedDirs = {};
      this.showNewSkillFileInput = false;
      this.newSkillFilePath = '';
      await this.loadSkillFiles();
      if (this.skillFiles.length > 0) {
        await this.openSkillFile('SKILL.md');
      }
    },

    buildSkillFileTree(files) {
      const roots = [];
      const directories = new Map();
      const normalizedFiles = [];
      for (const file of Array.isArray(files) ? files : []) {
        const parts = skillPathParts(file?.path);
        if (parts.length === 0) continue;
        const path = parts.join('/');
        if (normalizedFiles.some(item => item.path === path)) continue;
        normalizedFiles.push({ ...file, path });

        let children = roots;
        let parentPath = '';
        for (const part of parts.slice(0, -1)) {
          const directoryPath = parentPath ? `${parentPath}/${part}` : part;
          let directory = directories.get(directoryPath);
          if (!directory) {
            directory = { type: 'directory', name: part, path: directoryPath, children: [] };
            directories.set(directoryPath, directory);
            children.push(directory);
          }
          children = directory.children;
          parentPath = directoryPath;
        }
        children.push({ ...file, path, name: parts[parts.length - 1], type: 'file' });
      }

      const sortChildren = (children) => {
        children.sort((left, right) => {
          if (left.type !== right.type) return left.type === 'directory' ? -1 : 1;
          return left.name.localeCompare(right.name, undefined, { sensitivity: 'base', numeric: true });
        });
        children.filter(item => item.type === 'directory').forEach(item => sortChildren(item.children));
      };
      sortChildren(roots);
      this.skillFiles = normalizedFiles;
      this.skillFileTree = roots;

      const hasExpansionState = Object.keys(this.skillExpandedDirs || {}).length > 0;
      const validDirectories = new Set(directories.keys());
      const expanded = {};
      if (hasExpansionState) {
        for (const [path, value] of Object.entries(this.skillExpandedDirs || {})) {
          if (validDirectories.has(path)) expanded[path] = Boolean(value);
        }
      } else {
        for (const path of ['references', 'references/Web-Fuzzing-Box']) {
          if (validDirectories.has(path)) expanded[path] = true;
        }
      }
      this.skillExpandedDirs = expanded;
    },

    skillFileTreeRows() {
      const rows = [];
      const visit = (nodes, depth) => {
        for (const node of nodes || []) {
          rows.push({ ...node, depth });
          if (node.type === 'directory' && this.skillExpandedDirs?.[node.path]) {
            visit(node.children, depth + 1);
          }
        }
      };
      visit(this.skillFileTree, 0);
      return rows;
    },

    toggleSkillDirectory(path) {
      if (!path) return;
      this.skillExpandedDirs = {
        ...(this.skillExpandedDirs || {}),
        [path]: !this.skillExpandedDirs?.[path],
      };
    },

    skillDirectoryExpanded(path) {
      return Boolean(this.skillExpandedDirs?.[path]);
    },

    skillFilePreviewable(fileOrPath) {
      const file = typeof fileOrPath === 'string'
        ? (this.skillFiles || []).find(item => item.path === fileOrPath)
        : fileOrPath;
      const path = String(file?.path || fileOrPath || '');
      const extension = path.includes('.') ? path.split('.').pop().toLowerCase() : '';
      return !SKILL_BINARY_EXTENSIONS.has(extension);
    },

    formatSkillFileSize(size) {
      const bytes = Number(size) || 0;
      if (bytes < 1024) return `${bytes} B`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    },

    async loadSkillFiles() {
      if (!this.skillEditor.name) {
        this.skillFiles = [];
        this.skillFileTree = [];
        this.skillExpandedDirs = {};
        return;
      }
      try {
        const files = await this.api('GET', `/skills/${this.skillEditor.name}/files`);
        this.buildSkillFileTree(files);
      } catch(e) {
        this.skillFiles = [];
        this.skillFileTree = [];
        this.skillExpandedDirs = {};
      }
    },

    async openSkillFile(path) {
      if (!this.skillFilePreviewable(path)) {
        this.showToast('该文件为二进制或不支持在线编辑，请在 Workspace 中使用。', 'error');
        return;
      }
      if (this.skillEditor.fileDirty) {
        if (!confirm('当前文件未保存，是否切换？')) return;
      }
      try {
        if (path === 'SKILL.md') {
          const sk = this.skills.find(s => s.name === this.skillEditor.name);
          if (sk) {
            this.skillEditor.activeFile = path;
            this.skillEditor.fileContent = stripSkillFrontmatter(sk.content);
            this.skillEditor.fileDirty = false;
            this.skillEditor.fileTruncated = false;
            return;
          }
        }
        const metadata = (this.skillFiles || []).find(file => file.path === path);
        const isLargeFile = Number(metadata?.size || 0) > SKILL_FILE_PREVIEW_LIMIT;
        const previewQuery = isLargeFile ? `?preview_bytes=${SKILL_FILE_PREVIEW_LIMIT}` : '';
        const resp = await this.api('GET', `/skills/${this.skillEditor.name}/files/${path}${previewQuery}`);
        this.skillEditor.activeFile = path;
        this.skillEditor.fileContent = resp.content || '';
        this.skillEditor.fileDirty = false;
        this.skillEditor.fileTruncated = Boolean(resp.truncated);
        if (this.skillEditor.fileTruncated) {
          this.showToast('文件过大，仅显示前 512 KB；完整文件仍保留在 Skill 目录中。', 'info');
        }
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async saveSkillFile() {
      if (!this.skillEditor.activeFile) return;
      if (this.skillEditor.fileTruncated) {
        this.showToast('当前为大文件截断预览，不允许直接保存。', 'error');
        return;
      }
      if (!this.skillEditor.name) { this.showToast('name 不能为空', 'error'); return; }
      const path = this.skillEditor.activeFile;
      const content = this.skillEditor.fileContent || '';
      try {
        if (path === 'SKILL.md') {
          const name = this.skillEditor.name.trim();
          const desc = (this.skillEditor.description || '').trim();
          if (!desc) { this.showToast('description 不能为空', 'error'); return; }
          const body = stripSkillFrontmatter(content);
          if (this.skillEditor.isNew) {
            await this.api('POST', '/skills', { name, display_name: name, description: desc, content: body, enabled: true });
            this.skillEditor.isNew = false;
          } else {
            await this.api('PUT', `/skills/${this.skillEditor.name}`, { description: desc, content: body });
          }
          await this.loadSkills();
        } else {
          await this.api('PUT', `/skills/${this.skillEditor.name}/files/${path}`, { content });
        }
        this.skillEditor.fileDirty = false;
        this.showToast('已保存');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async createSkillFile() {
      if (!this.skillEditor.name || !this.newSkillFilePath) return;
      const path = this.newSkillFilePath.trim();
      try {
        await this.api('POST', `/skills/${this.skillEditor.name}/files`, { path, content: '' });
        this.showNewSkillFileInput = false;
        this.newSkillFilePath = '';
        await this.loadSkillFiles();
        await this.openSkillFile(path);
        this.showToast('文件已创建');
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async deleteSkillFile(path) {
      if (this.skillFileDeleting) return;
      if (!confirm(`删除文件 ${path}？`)) return;
      this.skillFileDeleting = path;
      try {
        await this.api('DELETE', `/skills/${this.skillEditor.name}/files/${path}`);
        await this.loadSkillFiles();
        if (this.skillEditor.activeFile === path) {
          this.skillEditor.activeFile = '';
          this.skillEditor.fileContent = '';
          this.skillEditor.fileDirty = false;
          this.skillEditor.fileTruncated = false;
        }
        this.showToast('已删除');
      } catch(e) { this.showToast(e.message, 'error'); }
      finally { this.skillFileDeleting = ''; }
    },

    async setSkillEnabled(sk, enabled) {
      if (!sk?.name || this.skillToggleLoading) return;
      this.skillToggleLoading = sk.name;
      try {
        const updated = await this.api(
          'PATCH',
          `/skills/${encodeURIComponent(sk.name)}/enabled`,
          { enabled: Boolean(enabled) },
        );
        const listed = this.skills.find(item => item.name === sk.name);
        if (listed) listed.enabled = updated.enabled;
        if (this.skillEditor.name === sk.name) {
          this.skillEditor.enabled = updated.enabled;
        }
        await this.loadPhaseCapabilities?.();
        this.showToast(`${sk.name} 已${updated.enabled ? '启用' : '禁用'}`);
        return updated;
      } catch(e) { this.showToast(e.message, 'error'); }
      finally { this.skillToggleLoading = ''; }
    },

    async toggleSkillFromEditor() {
      const sk = this.skills.find(s => s.name === this.skillEditor.name);
      if (!sk) return;
      await this.setSkillEnabled(sk, !sk.enabled);
    },

    openSkillEditor() {
      this.skillEditor = { name: '', description: '', content: '', isNew: true, builtin: false, enabled: true, activeFile: 'SKILL.md', fileContent: '', fileDirty: true, fileTruncated: false };
      this.skillFiles = [{ path: 'SKILL.md', size: 0 }];
      this.skillFileTree = [{ type: 'file', name: 'SKILL.md', path: 'SKILL.md', size: 0 }];
      this.skillExpandedDirs = {};
      this.showNewSkillFileInput = false;
    },

    fillSkillPreset() {
      this.skillEditor.fileContent = `## 触发条件

当 AI 准备...

## 操作步骤

1. ...
2. ...

## 规则

- 规则 1`;
      this.skillEditor.fileDirty = true;
    },

    async uploadSkillFiles(event) {
      const files = Array.from(event.target.files || []);
      if (files.length === 0) return;

      const mdFiles = files.filter(f => f.name.endsWith('.md'));
      if (mdFiles.length === 1 && files.length === 1) {
        const file = mdFiles[0];
        const reader = new FileReader();
        reader.onload = async (e) => {
          const text = e.target.result;
          const fmMatch = text.match(/^---\s*\nname:\s*(\S+)/m);
          const name = fmMatch ? fmMatch[1] : file.name.replace(/\.md$/, '').replace(/[^a-z0-9-]/g, '-');
          const descMatch = text.match(/^description:\s*(.+)$/m);
          const desc = descMatch ? descMatch[1].trim() : '';
          let body = text;
          const fm = text.match(/^---\s*\n.*?\n---\s*\n?/s);
          if (fm) body = text.replace(fm, '');
          this.skillEditor = { id: '', name, description: desc, content: '', isNew: true, builtin: false, enabled: true, activeFile: 'SKILL.md', fileContent: body, fileDirty: true, fileTruncated: false };
          this.skillFiles = [{ path: 'SKILL.md', size: text.length }];
          this.skillFileTree = [{ type: 'file', name: 'SKILL.md', path: 'SKILL.md', size: text.length }];
          this.skillExpandedDirs = {};
        };
        reader.readAsText(file);
      } else {
        const dirs = new Set(files.map(f => f.webkitRelativePath?.split('/')[0] || 'uploaded-skill'));
        const skillName = [...dirs][0].replace(/[^a-z0-9-]/g, '-').toLowerCase();
        try {
          const exists = this.skills.find(s => s.name === skillName);
          if (!exists) {
            await this.api('POST', '/skills', { name: skillName, display_name: skillName, description: '上传的 Skill', content: '请编辑此文件', enabled: true });
          }
          for (const file of files) {
            const relPath = file.webkitRelativePath?.split('/').slice(1).join('/') || file.name;
            if (!relPath || relPath.endsWith('/')) continue;
            const content = await file.text();
            if (relPath === 'SKILL.md') {
              const fmMatch = content.match(/^---\s*\n(.*?)\n---\s*\n?/s);
              let name = skillName, desc = '上传的 Skill', body = content;
              if (fmMatch) {
                const nm = fmMatch[1].match(/^name:\s*(\S+)/m);
                const dm = fmMatch[1].match(/^description:\s*(.+)$/m);
                if (nm) name = nm[1].trim();
                if (dm) desc = dm[1].trim();
                body = stripSkillFrontmatter(content);
                const fullContent = `---\nname: ${name}\ndescription: ${desc}\n---\n\n${body}`;
                await this.api('PUT', `/skills/${skillName}/files/SKILL.md`, { content: fullContent });
                continue;
              }
            }
            await this.api('PUT', `/skills/${skillName}/files/${relPath}`, { content });
          }
          await this.loadSkills();
          const sk = this.skills.find(s => s.name === skillName);
          if (sk) await this.selectSkill(sk);
          this.showToast(`已上传 ${files.length} 个文件到 ${skillName}`);
        } catch(e) { this.showToast(e.message, 'error'); }
      }
      event.target.value = '';
    },

    async saveSkill() {
      await this.saveSkillFile();
    },

    async deleteSkill(sk) {
      if (!confirm(`确定要删除 "${sk.name}" 吗？`)) return;
      try {
        await this.api('DELETE', `/skills/${sk.name}`);
        if (this.skillEditor.name === sk.name) {
          this.skillEditor = { name: '', description: '', content: '', isNew: false, builtin: false, enabled: true, activeFile: '', fileContent: '', fileDirty: false, fileTruncated: false };
          this.skillFiles = [];
        }
        await this.loadSkills();
        this.showToast('已删除');
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    async restoreBundledSkill() {
      const name = this.skillEditor?.name;
      if (!name || this.skillEditor?.source !== 'managed' || !this.skillEditor?.bundledHash) return;
      if (!confirm(`放弃运行时定制并恢复 ${name} 的最新内置版本吗？`)) return;
      try {
        const restored = await this.api('POST', `/skills/${encodeURIComponent(name)}/restore-bundled`);
        await this.loadSkills();
        await this.selectSkill(restored);
        this.showToast(`已恢复 ${name} 的最新内置版本`);
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    async toggleSkill(sk) {
      await this.setSkillEnabled(sk, !sk.enabled);
    },
  }
};
