window.AxiomModules = window.AxiomModules || {};

Object.assign(window.AxiomModules.state.state, {
  archives: [],
  archiveStatusFilter: 'active',
  archiveLoading: false,
  selectedArchiveId: '',
  selectedArchive: null,
  archivePreview: null,
  archivePreviewLoading: false,
  archiveSubmitting: false,
  archiveReason: '',
  showArchivePreviewModal: false,
});

window.AxiomModules.archives = {
  methods: {
    async loadArchives(selectId = '') {
      if (!this.selectedProjectId) return;
      this.archiveLoading = true;
      try {
        const suffix = this.archiveStatusFilter ? `?status=${encodeURIComponent(this.archiveStatusFilter)}` : '';
        this.archives = await this.api('GET', `/tasks/${this.selectedProjectId}/archives${suffix}`);
        const wanted = selectId || this.selectedArchiveId;
        const selected = this.archives.find(item => item.id === wanted) || this.archives[0] || null;
        this.selectedArchiveId = selected?.id || '';
        this.selectedArchive = selected;
      } catch (error) {
        this.showToast(error.message, 'error');
      } finally {
        this.archiveLoading = false;
      }
    },

    async changeArchiveStatusFilter() {
      this.selectedArchiveId = '';
      this.selectedArchive = null;
      await this.loadArchives();
    },

    async selectArchive(archiveId) {
      if (!archiveId || !this.selectedProjectId) return;
      this.selectedArchiveId = archiveId;
      try {
        this.selectedArchive = await this.api('GET', `/tasks/${this.selectedProjectId}/archives/${archiveId}`);
      } catch (error) {
        this.showToast(error.message, 'error');
      }
    },

    async openArchivePreview(rootType, rootId) {
      if (!this.selectedProjectId || !rootId) return;
      this.archivePreviewLoading = true;
      this.archivePreview = null;
      this.archiveReason = '';
      this.showArchivePreviewModal = true;
      try {
        this.archivePreview = await this.api(
          'POST',
          `/tasks/${this.selectedProjectId}/archives/preview`,
          { root_type: rootType, root_id: rootId },
        );
      } catch (error) {
        this.showArchivePreviewModal = false;
        this.showToast(error.message, 'error');
      } finally {
        this.archivePreviewLoading = false;
      }
    },

    closeArchivePreview() {
      if (this.archiveSubmitting) return;
      this.showArchivePreviewModal = false;
      this.archivePreview = null;
      this.archiveReason = '';
    },

    async confirmArchive() {
      const preview = this.archivePreview;
      if (!preview || preview.blockers?.length || this.archiveSubmitting) return;
      this.archiveSubmitting = true;
      try {
        const batch = await this.api('POST', `/tasks/${this.selectedProjectId}/archives`, {
          root_type: preview.root_type,
          root_id: preview.root_id,
          expected_closure_hash: preview.closure_hash,
          reason: this.archiveReason,
          actor: this.actorName(),
        });
        this.closeArchivePreview();
        await this.loadProject(this.selectedProjectId);
        await this.loadArchives(batch.id);
        if (this.view === 'graph') this.updateGraph();
        this.showToast(`已归档 ${batch.members.length} 项`, 'success');
      } catch (error) {
        this.showToast(error.message, 'error');
      } finally {
        this.archiveSubmitting = false;
      }
    },

    async restoreArchive(batch = null) {
      const target = batch || this.selectedArchive;
      if (!target || target.status !== 'active') return;
      if (!confirm(
        `恢复归档批次 ${target.id}？实际恢复 ${target.restored_count || 0} 项，`
        + `${target.retained_archived_count || 0} 项仍会被其他批次覆盖。恢复后的 pending Intent 可能重新被领取。`,
      )) return;
      try {
        const restored = await this.api(
          'POST',
          `/tasks/${this.selectedProjectId}/archives/${target.id}/restore`,
          { actor: this.actorName() },
        );
        await this.loadProject(this.selectedProjectId);
        await this.loadArchives();
        if (this.view === 'graph') this.updateGraph();
        this.showToast(
          `已恢复 ${restored.restored_count} 项，${restored.retained_archived_count} 项仍被其他批次覆盖`,
          'success',
        );
      } catch (error) {
        this.showToast(error.message, 'error');
      }
    },

    archiveMembers(entityType) {
      return (this.selectedArchive?.members || []).filter(item => item.entity_type === entityType);
    },

    archiveEntityLabel(type) {
      return ({ fact: 'Fact', intent: 'Intent', artifact: '产物', vulnerability: '漏洞', todo: 'Todo' })[type] || type;
    },

    archiveRelationLabel(relation) {
      return ({ root: '归档根', producer: '产生来源', output: '产出', descendant: '下游', related: '关联' })[relation] || relation;
    },

    async previewArchivedArtifact(member) {
      if (!member || !this.selectedArchive) return;
      try {
        const result = await this.api(
          'GET',
          `/tasks/${this.selectedProjectId}/archives/${this.selectedArchive.id}/artifacts/${member.entity_id}/content`,
        );
        this.yamlPreviewTitle = `${member.entity_id} · ${member.title || '归档产物'}`;
        this.yamlPreviewText = result.content || '';
        this.yamlPreviewHtml = `<pre class="whitespace-pre-wrap break-words">${this.escapeHtml(this.yamlPreviewText)}</pre>`;
        this.showYamlModal = true;
      } catch (error) {
        this.showToast(error.message, 'error');
      }
    },

    archivedArtifactDownloadUrl(member) {
      if (!member || !this.selectedArchive) return '#';
      return `/api/v1/tasks/${encodeURIComponent(this.selectedProjectId)}/archives/${encodeURIComponent(this.selectedArchive.id)}/artifacts/${encodeURIComponent(member.entity_id)}/download`;
    },
  },
};
