window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.skills = {
  methods: {
    async loadSkills() {
      try {
        this.skills = await this.api('GET', '/skills');
      } catch (e) { /* ignore */ }
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
      this.skillEditor = { name: sk.name, description: sk.description || '', content: sk.content || '', isNew: false, builtin: sk.builtin, enabled: sk.enabled, activeFile: '', fileContent: '', fileDirty: false };
      this.showNewSkillFileInput = false;
      this.newSkillFilePath = '';
      await this.loadSkillFiles();
      if (this.skillFiles.length > 0) {
        await this.openSkillFile('SKILL.md');
      }
    },

    async loadSkillFiles() {
      if (!this.skillEditor.name) { this.skillFiles = []; return; }
      try {
        this.skillFiles = await this.api('GET', `/skills/${this.skillEditor.name}/files`);
      } catch(e) { this.skillFiles = []; }
    },

    async openSkillFile(path) {
      if (this.skillEditor.fileDirty) {
        if (!confirm('当前文件未保存，是否切换？')) return;
      }
      try {
        if (path === 'SKILL.md') {
          const sk = this.skills.find(s => s.name === this.skillEditor.name);
          if (sk) {
            const fm = (sk.content || '').match(/^---\s*\n(.*?)\n---/s);
            let body = sk.content || '';
            if (fm) body = (sk.content || '').replace(/^---\s*\n.*?\n---\s*\n?/s, '');
            this.skillEditor.activeFile = path;
            this.skillEditor.fileContent = body;
            this.skillEditor.fileDirty = false;
            return;
          }
        }
        const resp = await this.api('GET', `/skills/${this.skillEditor.name}/files/${path}`);
        this.skillEditor.activeFile = path;
        this.skillEditor.fileContent = resp.content;
        this.skillEditor.fileDirty = false;
      } catch(e) { this.showToast(e.message, 'error'); }
    },

    async saveSkillFile() {
      if (!this.skillEditor.activeFile) return;
      if (!this.skillEditor.name) { this.showToast('name 不能为空', 'error'); return; }
      const path = this.skillEditor.activeFile;
      const content = this.skillEditor.fileContent || '';
      try {
        if (path === 'SKILL.md') {
          const name = this.skillEditor.name.trim();
          const desc = (this.skillEditor.description || '').trim();
          if (!desc) { this.showToast('description 不能为空', 'error'); return; }
          const fullContent = `---\nname: ${name}\ndescription: ${desc}\n---\n\n${content}`;
          if (this.skillEditor.isNew) {
            await this.api('POST', '/skills', { name, display_name: name, description: desc, content: fullContent, enabled: true });
            this.skillEditor.isNew = false;
          } else {
            await this.api('PUT', `/skills/${this.skillEditor.name}`, { description: desc, content: fullContent });
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
      if (!confirm(`删除文件 ${path}？`)) return;
      try {
        await this.api('DELETE', `/skills/${this.skillEditor.name}/files/${path}`);
        await this.loadSkillFiles();
        if (this.skillEditor.activeFile === path) {
          this.skillEditor.activeFile = '';
          this.skillEditor.fileContent = '';
        }
        this.showToast('已删除');
      } catch(e) { this.showToast(e.message, 'error'); }
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
      this.skillEditor = { name: '', description: '', content: '', isNew: true, builtin: false, enabled: true, activeFile: 'SKILL.md', fileContent: '', fileDirty: true };
      this.skillFiles = [{ path: 'SKILL.md', size: 0 }];
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
          this.skillEditor = { id: '', name, description: desc, content: '', isNew: true, builtin: false, enabled: true, activeFile: 'SKILL.md', fileContent: body, fileDirty: true };
          this.skillFiles = [{ path: 'SKILL.md', size: text.length }];
        };
        reader.readAsText(file);
      } else {
        const dirs = new Set(files.map(f => f.webkitRelativePath?.split('/')[0] || 'uploaded-skill'));
        const skillName = [...dirs][0].replace(/[^a-z0-9-]/g, '-').toLowerCase();
        try {
          const exists = this.skills.find(s => s.name === skillName);
          if (!exists) {
            await this.api('POST', '/skills', { name: skillName, display_name: skillName, description: '上传的 Skill', content: '---\nname: ' + skillName + '\ndescription: 上传的 Skill\n---\n\n请编辑此文件', enabled: true });
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
                body = content.replace(/^---\s*\n.*?\n---\s*\n?/s, '');
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
          this.skillEditor = { name: '', description: '', content: '', isNew: false, builtin: false, enabled: true, activeFile: '', fileContent: '', fileDirty: false };
          this.skillFiles = [];
        }
        await this.loadSkills();
        this.showToast('已删除');
      } catch (e) { this.showToast(e.message, 'error'); }
    },

    async toggleSkill(sk) {
      await this.setSkillEnabled(sk, !sk.enabled);
    },
  }
};
