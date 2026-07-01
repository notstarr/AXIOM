window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.prompts = {
  methods: {
    loadPrompts() {
      try {
        const raw = localStorage.getItem('axiom_prompts');
        this.prompts = raw ? JSON.parse(raw) : [];
      } catch { this.prompts = []; }
    },

    savePromptsToStorage() {
      localStorage.setItem('axiom_prompts', JSON.stringify(this.prompts));
    },

    promptCategories() {
      const cats = new Set();
      this.prompts.forEach(p => { if (p.category) cats.add(p.category); });
      return Array.from(cats).sort();
    },

    filteredPrompts() {
      let list = this.prompts;
      if (this.promptFilter) list = list.filter(p => p.category === this.promptFilter);
      if (this.promptSearch) {
        const q = this.promptSearch.toLowerCase();
        list = list.filter(p =>
          p.title.toLowerCase().includes(q) ||
          p.content.toLowerCase().includes(q) ||
          (p.category && p.category.toLowerCase().includes(q))
        );
      }
      return list;
    },

    async copyPromptContent(prompt) {
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(prompt.content);
        } else {
          const ta = document.createElement('textarea');
          ta.value = prompt.content;
          document.body.appendChild(ta);
          ta.select();
          document.execCommand('copy');
          document.body.removeChild(ta);
        }
        prompt._copied = true;
        this.showToast('已复制到剪贴板', 'success');
        setTimeout(() => { prompt._copied = false; }, 2000);
      } catch { this.showToast('复制失败', 'error'); }
    },

    editPrompt(prompt) {
      this.editingPrompt = prompt;
      this.newPrompt = { title: prompt.title, category: prompt.category || '通用', content: prompt.content };
      this.showPromptEditor = true;
    },

    async savePrompt() {
      if (!this.newPrompt.title || !this.newPrompt.content) return;
      if (this.editingPrompt) {
        // Update existing
        const idx = this.prompts.findIndex(p => p.id === this.editingPrompt.id);
        if (idx !== -1) {
          this.prompts[idx].title = this.newPrompt.title;
          this.prompts[idx].category = this.newPrompt.category;
          this.prompts[idx].content = this.newPrompt.content;
          this.prompts[idx].updated_at = new Date().toISOString();
        }
        this.showToast('提示词已更新', 'success');
      } else {
        // Create new
        this.prompts.unshift({
          id: 'pm_' + Date.now(),
          title: this.newPrompt.title,
          category: this.newPrompt.category || '通用',
          content: this.newPrompt.content,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        });
        this.showToast('提示词已保存', 'success');
      }
      this.savePromptsToStorage();
      this.showPromptEditor = false;
      this.editingPrompt = null;
      this.newPrompt = { title: '', category: '通用', content: '' };
    },

    deletePrompt(id) {
      if (!confirm('确定删除这条提示词？')) return;
      this.prompts = this.prompts.filter(p => p.id !== id);
      this.savePromptsToStorage();
      this.showToast('已删除');
    },
  }
};
