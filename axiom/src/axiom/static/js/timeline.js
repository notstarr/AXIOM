window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.timeline = {
  methods: {
    timelineEvents() {
      if (this.replay.active) return this.replay.visibleEvents;
      if (!this.project) return [];
      if (this._timelineEventsCacheProject === this.project) return this._timelineEventsCache;

      const events = [];
      let order = 0;
      const origin = this.getFactRecord('origin');
      const goal = this.getFactRecord('goal');

      events.push({
        id: `project-created-${this.project.project.id}`,
        type: 'project_created',
        timestamp: this.project.project.created_at,
        actor: '系统',
        title: this.project.project.title,
        meta: [origin ? origin.description : null, goal ? `目标：${goal.description}` : null].filter(Boolean),
        targetType: 'fact',
        targetId: 'origin',
        order: order++,
        intentId: null,
        producedFactId: null,
        sourceFactIds: [],
      });

      for (const hint of this.project.hints) {
        events.push({
          id: `hint-${hint.id}`,
          type: 'hint_added',
          timestamp: hint.created_at,
          actor: hint.creator,
          title: hint.content,
          meta: [],
          targetType: 'hints',
          targetId: hint.id,
          order: order++,
          intentId: null,
          producedFactId: null,
          sourceFactIds: [],
        });
      }

      for (const intent of this.project.intents) {
        events.push({
          id: `intent-declared-${intent.id}`,
          type: 'intent_declared',
          timestamp: intent.created_at,
          actor: intent.creator,
          title: intent.description,
          meta: [intent.id, `基于 ${intent.from.join(', ')}`],
          targetType: 'intent',
          targetId: intent.id,
          order: order++,
          intentId: intent.id,
          producedFactId: null,
          sourceFactIds: [...intent.from],
        });

        if (!intent.concluded_at) continue;

        if (!intent.to) {
          if (this.intentLifecycleState(intent) !== 'completed') continue;
          events.push({
            id: `intent-concluded-${intent.id}`,
            type: 'intent_concluded',
            timestamp: intent.concluded_at,
            actor: intent.worker || intent.creator,
            title: intent.description,
            meta: [intent.id, '未产出 Fact', `基于 ${intent.from.join(', ')}`],
            targetType: 'intent',
            targetId: intent.id,
            order: order++,
            intentId: intent.id,
            producedFactId: null,
            sourceFactIds: [...intent.from],
          });
          continue;
        }

        if (intent.to === 'goal') {
          events.push({
            id: `project-completed-${intent.id}`,
            type: 'project_completed',
            timestamp: intent.concluded_at,
            actor: intent.worker || intent.creator,
            title: intent.description,
            meta: [intent.id, `基于 ${intent.from.join(', ')}`],
            targetType: 'fact',
            targetId: 'goal',
            order: order++,
            intentId: intent.id,
            producedFactId: 'goal',
            sourceFactIds: [...intent.from],
          });
          continue;
        }

        const fact = this.getFactRecord(intent.to);
        events.push({
          id: `intent-concluded-${intent.id}`,
          type: 'intent_concluded',
          timestamp: intent.concluded_at,
          actor: intent.worker || intent.creator,
          title: fact ? fact.description : intent.description,
          meta: [intent.id, `产出 ${intent.to}`, `基于 ${intent.from.join(', ')}`],
          targetType: 'fact',
          targetId: intent.to,
          order: order++,
          intentId: intent.id,
          producedFactId: intent.to,
          sourceFactIds: [...intent.from],
        });
      }

      const chronological = [...events].sort((a, b) =>
        a.timestamp.localeCompare(b.timestamp) || a.order - b.order
      );

      const resolved = [];
      for (let i = 0; i < chronological.length;) {
        const bucket = [chronological[i]];
        let j = i + 1;
        while (j < chronological.length && chronological[j].timestamp === chronological[i].timestamp) {
          bucket.push(chronological[j]);
          j += 1;
        }
        resolved.push(...this.resolveTimelineBucket(bucket));
        i = j;
      }

      const cached = resolved.map((entry, index) => ({ ...entry, isLast: index === resolved.length - 1 }));
      this._timelineEventsCacheProject = this.project;
      this._timelineEventsCache = cached;
      return cached;
    },

    resolveTimelineBucket(bucket) {
      if (bucket.length <= 1) return bucket;

      const eventById = new Map(bucket.map(event => [event.id, event]));
      const declareEventIdByIntent = new Map();
      const produceEventIdByFact = new Map();
      const outgoing = new Map(bucket.map(event => [event.id, new Set()]));
      const incomingCount = new Map(bucket.map(event => [event.id, 0]));

      const addDependency = (beforeId, afterId) => {
        if (!beforeId || !afterId || beforeId === afterId) return;
        const deps = outgoing.get(beforeId);
        if (!deps || deps.has(afterId)) return;
        deps.add(afterId);
        incomingCount.set(afterId, (incomingCount.get(afterId) || 0) + 1);
      };

      for (const event of bucket) {
        if (event.type === 'intent_declared') declareEventIdByIntent.set(event.intentId, event.id);
        if (event.producedFactId) produceEventIdByFact.set(event.producedFactId, event.id);
      }

      for (const event of bucket) {
        if ((event.type === 'intent_concluded' || event.type === 'project_completed') && event.intentId) {
          addDependency(declareEventIdByIntent.get(event.intentId), event.id);
        }

        if (!event.sourceFactIds?.length) continue;
        for (const factId of event.sourceFactIds) {
          addDependency(produceEventIdByFact.get(factId), event.id);
        }
      }

      const ready = bucket
        .filter(event => incomingCount.get(event.id) === 0)
        .sort((a, b) => a.order - b.order);
      const ordered = [];

      while (ready.length > 0) {
        const event = ready.shift();
        ordered.push(event);
        for (const nextId of outgoing.get(event.id) || []) {
          incomingCount.set(nextId, incomingCount.get(nextId) - 1);
          if (incomingCount.get(nextId) === 0) {
            ready.push(eventById.get(nextId));
            ready.sort((a, b) => a.order - b.order);
          }
        }
      }

      if (ordered.length === bucket.length) return ordered;

      const remainingIds = bucket
        .filter(event => !ordered.some(placed => placed.id === event.id))
        .sort((a, b) => a.order - b.order);
      return [...ordered, ...remainingIds];
    },

    timelineEventBadge(entry) {
      const labels = {
        project_created: '项目',
        hint_added: '提示',
        reason_started: 'reason',
        intent_declared: '意图',
        intent_running: '执行',
        intent_concluded: '结束',
        project_completed: '完成',
      };
      return labels[entry.type] || '事件';
    },

    timelineEventBadgeClass(entry) {
      const classes = {
        project_created: 'bg-slate-100 text-slate-600',
        hint_added: 'bg-amber-50 text-amber-700',
        reason_started: 'bg-sky-50 text-sky-700',
        intent_declared: 'bg-violet-50 text-violet-700',
        intent_running: 'bg-amber-50 text-amber-700',
        intent_concluded: 'bg-teal-50 text-teal-700',
        project_completed: 'bg-rose-50 text-rose-700',
      };
      return classes[entry.type] || 'bg-slate-100 text-slate-600';
    },

    timelineEventIsInteractive(entry) {
      return entry?.targetType === 'fact' || entry?.targetType === 'intent' || entry?.targetType === 'hints' || entry?.targetType === 'reason';
    },

    timelineEventTriggersGraphFocus(entry) {
      return entry?.targetType === 'fact' || entry?.targetType === 'intent';
    },

    timelineEventDotClass(entry) {
      const classes = {
        project_created: 'bg-slate-400',
        hint_added: 'bg-amber-400',
        reason_started: 'bg-sky-400',
        intent_declared: 'bg-violet-400',
        intent_running: 'bg-amber-400',
        intent_concluded: 'bg-teal-400',
        project_completed: 'bg-rose-400',
      };
      return classes[entry.type] || 'bg-slate-300';
    },

    timelineEntryButtonClass(entry) {
      const base = this.timelineEventIsInteractive(entry) ? 'cursor-pointer hover:bg-slate-50/70' : 'cursor-default';
      return this.timelineEntryIsSelected(entry)
        ? `${base} bg-brand-50/80 ring-1 ring-brand-200`
        : base;
    },

    timelineEntryStyle(entry) {
      const styles = {
        project_created: 'background:#dbeafe;color:#1d4ed8;',
        hint_added: 'background:#fef3c7;color:#92400e;',
        intent_declared: 'background:#ede9fe;color:#6d28d9;',
        intent_running: 'background:#fef3c7;color:#b45309;',
        intent_concluded: 'background:#d1fae5;color:#047857;',
        project_completed: 'background:#fee2e2;color:#b91c1c;',
      };
      return styles[entry.type] || 'background:#f1f5f9;color:#475569;';
    },

    timelineEntryDomId(entryId) {
      return `timeline-entry-${entryId}`;
    },

    timelineEntryElement(entryId) {
      return document.getElementById(this.timelineEntryDomId(entryId));
    },

    timelineTargetEntryIdForGraphSelection(allowMultiFact = false) {
      const events = this.timelineEvents();
      if (this.selectedNode?.type === 'intent') {
        const selectedIntentId = this.selectedNode.id;
        return events.find(entry => entry.type === 'intent_declared' && entry.intentId === selectedIntentId)?.id
          || events.find(entry => entry.intentId === selectedIntentId)?.id
          || null;
      }

      if (this.selectedNode?.type === 'fact') {
        if (!allowMultiFact && this.selectedFacts.length > 1) return null;
        const selectedFactId = this.selectedNode.id;
        return events.find(entry => entry.targetType === 'fact' && entry.targetId === selectedFactId)?.id || null;
      }

      return null;
    },

    activeTimelineEntryId(allowMultiFact = false) {
      return this.selectedTimelineEntryId || this.timelineTargetEntryIdForGraphSelection(allowMultiFact);
    },

    latestTimelineEntryId() {
      const events = this.timelineEvents();
      return events.length > 0 ? events[events.length - 1].id : null;
    },

    timelineEntryIsSelected(entry) {
      return !!entry && this.activeTimelineEntryId(false) === entry.id;
    },

    selectedTimelineSummary() {
      const targetEntryId = this.activeTimelineEntryId(false);
      if (!targetEntryId) return null;

      const events = this.timelineEvents();
      const index = events.findIndex(entry => entry.id === targetEntryId);
      if (index < 0) return null;

      const entry = events[index];
      const total = events.length;
      const sequencePercent = total <= 1 ? 100 : Math.round((index / (total - 1)) * 100);
      const replayMode = this.replay.active;
      const totalDuration = replayMode
        ? this.replayTimelineElapsedDurationMs(events, Math.max(0, total - 1))
        : Math.max(0, Date.parse(events[total - 1].timestamp) - Date.parse(events[0].timestamp));
      const elapsedDuration = replayMode
        ? this.replayTimelineElapsedDurationMs(events, index)
        : Math.max(0, Date.parse(entry.timestamp) - Date.parse(events[0].timestamp));
      const timePercent = totalDuration === 0 ? 100 : Math.round((elapsedDuration / totalDuration) * 100);

      return {
        sequencePercent,
        sequenceLabel: `${index + 1} / ${total} · ${sequencePercent}%`,
        timePercent,
        timeLabel: `${this.formatDurationMs(elapsedDuration)} / ${this.formatDurationMs(totalDuration)} · ${timePercent}%`,
      };
    },

    scrollTimelineToEntry(entryId, options = {}) {
      const { flash = true } = options;
      if (!entryId) return;
      this.$nextTick(() => {
        requestAnimationFrame(() => {
          const entry = this.timelineEntryElement(entryId);
          if (!entry) return;
          const panel = entry.closest('.overflow-y-auto');
          if (!panel) return;
          const top = entry.offsetTop - panel.clientHeight / 2 + entry.clientHeight / 2;
          const targetTop = Math.max(0, top);
          panel.scrollTo({ top: targetTop, behavior: 'smooth' });
          if (!flash) return;
          const travel = Math.abs(panel.scrollTop - targetTop);
          const flashDelay = Math.min(450, Math.max(140, travel * 0.18));
          setTimeout(() => {
            entry.classList.remove('timeline-flash');
            void entry.offsetWidth;
            entry.classList.add('timeline-flash');
            setTimeout(() => entry.classList.remove('timeline-flash'), 1000);
          }, flashDelay);
        });
      });
    },

    followReplayTimelineTail() {
      if (!this.replay.active) return;
      this.scrollTimelineToEntry(this.latestTimelineEntryId(), { flash: false });
    },

    scrollTimelineToSelection() {
      return;
      this.scrollTimelineToEntry(this.activeTimelineEntryId(true));
    },

    openHintTimelineEntry(hintId) {
      if (!hintId) return;
      const entry = this.timelineEvents().find(event => event.type === 'hint_added' && event.targetId === hintId);
      if (!entry) return;
      this.sideTab = 'hints';
      this.openTimelineEntry(entry);
      this.scrollTimelineToEntry(entry.id);
    },

    openTimelineEntry(entry, options = {}) {
      const { centerGraph = true } = options;
      if (!entry || !this.timelineEventIsInteractive(entry)) return;
      if (!this.timelineEventTriggersGraphFocus(entry)) {
        this.selectedTimelineEntryId = entry.id;
        this.clearGraphSelection(true);
        return;
      }
      if (entry.targetType === 'intent' && entry.targetId) {
        this.selectIntent(entry.targetId);
        this.selectedTimelineEntryId = entry.id;
        if (centerGraph) this.centerGraphOnIntent(entry.targetId);
        return;
      }
      if (entry.targetType === 'fact' && entry.targetId) {
        this.selectedFacts = [entry.targetId];
        this.selectFact(entry.targetId);
        this.selectedTimelineEntryId = entry.id;
        if (centerGraph) this.centerGraphOnFact(entry.targetId);
      }
    },

    bottomTimelineMarkers() {
      const events = this.timelineEvents();
      if (events.length === 0) return [];
      const times = events.map(e => new Date(e.timestamp).getTime()).filter(t => !isNaN(t));
      if (times.length === 0) return [];
      const minT = Math.min(...times);
      const maxT = Math.max(...times);
      const range = Math.max(1, maxT - minT);
      const colorMap = {
        project_created: 'bg-teal-400',
        hint_added: 'bg-amber-400',
        intent_declared: 'bg-slate-300',
        intent_concluded: 'bg-emerald-400',
        project_completed: 'bg-rose-400',
        bootstrap: 'bg-orange-400',
      };
      return events.map((e, i) => {
        const t = new Date(e.timestamp).getTime();
        const pct = isNaN(t) ? (i / Math.max(1, events.length - 1)) * 100 : ((t - minT) / range) * 100;
        return {
          id: e.id,
          pct: Math.max(2, Math.min(98, pct)),
          color: colorMap[e.type] || 'bg-slate-300',
          title: e.title || e.type,
          targetType: e.targetType,
          targetId: e.targetId,
        };
      });
    },

    onTimelineMarkerClick(entry) {
      this.selectedTimelineEntryId = entry.id;
      if (entry.targetType === 'fact') {
        this.selectFact(entry.targetId);
        this.centerGraphOnFact(entry.targetId);
        this.sideTab = 'detail';
      } else if (entry.targetType === 'intent') {
        this.selectIntent(entry.targetId);
        this.centerGraphOnIntent(entry.targetId);
        this.sideTab = 'detail';
      } else if (entry.targetType === 'hints') {
        this.sideTab = 'hints';
      }
      this.$nextTick(() => {
        const bar = this.$refs.timelineBar;
        if (bar) {
          const el = bar.querySelector(`[title="${entry.title}"]`);
          if (el) el.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
        }
      });
    },

    // ── Replay methods ──
    replayProgressLabel() {
      if (!this.replay.active || this.replay.frames.length === 0) return '回放';
      return `回放 ${Math.min(this.replay.frameIndex + 1, this.replay.frames.length)} / ${this.replay.frames.length}`;
    },

    stopReplayTimer() {
      if (!this.replay.timer) return;
      clearTimeout(this.replay.timer);
      this.replay.timer = null;
    },

    updateReplaySpeed() {
      if (!this.replay.active || !this.replay.playing) return;
      this.stopReplayTimer();
      this.scheduleReplayTick();
    },

    replayEventWeight(event) {
      const type = event?.type;
      if (type === 'reason_started') return 1.6;
      if (type === 'intent_declared') return 1.05;
      if (type === 'intent_running') return 0.75;
      if (type === 'intent_concluded' || type === 'project_completed') return 1.6;
      if (type === 'project_created' || type === 'hint_added') return 0.8;
      return 1.0;
    },

    replayEventDurationMs(event) {
      const base = Number(this.replay.stepMs) || 1100;
      return Math.round(base * this.replayEventWeight(event));
    },

    replayTimelineElapsedDurationMs(events, index) {
      let elapsed = 0;
      for (let i = 0; i < index; i += 1) {
        elapsed += this.replayEventDurationMs(events[i]);
      }
      return elapsed;
    },

    scheduleReplayTick() {
      if (!this.replay.active || !this.replay.playing) return;
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.replay.playing = false;
        return;
      }
      this.stopReplayTimer();
      const currentFrame = this.replay.frames[this.replay.frameIndex];
      const delay = this.replayEventDurationMs(currentFrame?.event);
      this.replay.timer = setTimeout(() => this.advanceProjectReplay(), delay);
    },

    buildInitialReplayProject(sourceProject) {
      const origin = sourceProject.facts.find(fact => fact.id === 'origin');
      const goal = sourceProject.facts.find(fact => fact.id === 'goal');
      return {
        project: {
          ...this.cloneData(sourceProject.project),
          status: 'active',
          reason: null,
        },
        facts: [origin, goal].filter(Boolean).map(fact => this.cloneData(fact)),
        intents: [],
        hints: [],
      };
    },

    buildReplayFrames(sourceProject, baseEvents) {
      const sourceIntents = new Map(sourceProject.intents.map(intent => [intent.id, intent]));
      const replayEvents = [];

      for (const event of baseEvents) {
        if (event.type !== 'intent_declared' || !event.intentId) {
          replayEvents.push(this.cloneData(event));
          continue;
        }

        const sourceIntent = sourceIntents.get(event.intentId);
        replayEvents.push({
          id: `reason-started-${event.intentId}`,
          type: 'reason_started',
          timestamp: sourceIntent?.created_at || event.timestamp,
          actor: sourceIntent?.creator || event.actor || 'reasoner',
          title: sourceIntent?.description || event.title,
          meta: [
            sourceIntent?.id || event.intentId,
            `基于 ${(sourceIntent?.from || event.sourceFactIds || []).join(', ')}`,
          ],
          targetType: 'reason',
          targetId: sourceIntent?.id || event.intentId,
          order: `${event.order}.reason`,
          intentId: sourceIntent?.id || event.intentId,
          producedFactId: null,
          sourceFactIds: [...(sourceIntent?.from || event.sourceFactIds || [])],
        });
        replayEvents.push(this.cloneData(event));
        if (!sourceIntent?.worker) continue;
        replayEvents.push({
          id: `intent-running-${sourceIntent.id}`,
          type: 'intent_running',
          timestamp: sourceIntent.last_heartbeat_at || sourceIntent.created_at,
          actor: sourceIntent.worker,
          title: sourceIntent.description,
          meta: [sourceIntent.id, `执行者 ${sourceIntent.worker}`],
          targetType: 'intent',
          targetId: sourceIntent.id,
          order: `${event.order}.run`,
          intentId: sourceIntent.id,
          producedFactId: null,
          sourceFactIds: [...sourceIntent.from],
        });
      }

      const replayProject = this.buildInitialReplayProject(sourceProject);
      const frames = [];
      for (const event of replayEvents) {
        this.applyReplayEvent(replayProject, sourceProject, event);
        frames.push({
          event: this.cloneData(event),
          project: this.cloneData(replayProject),
        });
      }

      if (frames.length > 0) {
        frames[frames.length - 1].project.project.status = sourceProject.project.status;
        frames[frames.length - 1].project.project.reason = this.cloneData(sourceProject.project.reason);
      }
      return frames;
    },

    applyReplayEvent(replayProject, sourceProject, event) {
      if (!replayProject || !sourceProject || !event) return;
      const sourceIntent = event.intentId
        ? sourceProject.intents.find(intent => intent.id === event.intentId) || null
        : null;

      if (event.type === 'project_created') {
        replayProject.project.title = sourceProject.project.title;
        replayProject.project.status = 'active';
        return;
      }

      if (event.type === 'hint_added') {
        const hint = sourceProject.hints.find(item => item.id === event.targetId);
        if (hint && !replayProject.hints.some(item => item.id === hint.id)) {
          replayProject.hints.push(this.cloneData(hint));
        }
        return;
      }

      if (event.type === 'reason_started') {
        replayProject.project.reason = {
          worker: event.actor || 'reasoner',
          trigger: 'new_facts',
          started_at: event.timestamp,
          last_heartbeat_at: event.timestamp,
        };
        return;
      }

      if (event.type === 'intent_declared') {
        if (!sourceIntent) return;
        replayProject.project.reason = null;
        if (!replayProject.intents.some(intent => intent.id === sourceIntent.id)) {
          replayProject.intents.push({
            id: sourceIntent.id,
            from: [...sourceIntent.from],
            to: null,
            description: sourceIntent.description,
            creator: sourceIntent.creator,
            worker: null,
            last_heartbeat_at: null,
            created_at: sourceIntent.created_at,
            concluded_at: null,
          });
        }
        return;
      }

      if (event.type === 'intent_running') {
        if (!sourceIntent) return;
        const replayIntent = replayProject.intents.find(intent => intent.id === sourceIntent.id);
        if (!replayIntent) return;
        replayIntent.worker = sourceIntent.worker || sourceIntent.creator;
        replayIntent.last_heartbeat_at = sourceIntent.last_heartbeat_at || sourceIntent.created_at;
        return;
      }

      if (event.type !== 'intent_concluded' && event.type !== 'project_completed') return;
      if (!sourceIntent) return;

      const replayIntent = replayProject.intents.find(intent => intent.id === sourceIntent.id);
      if (replayIntent) {
        replayIntent.to = sourceIntent.to;
        replayIntent.worker = sourceIntent.worker || sourceIntent.creator;
        replayIntent.last_heartbeat_at = sourceIntent.last_heartbeat_at;
        replayIntent.concluded_at = sourceIntent.concluded_at;
      }

      if (event.type === 'project_completed') {
        replayProject.project.status = 'completed';
        return;
      }

      const producedFact = sourceProject.facts.find(fact => fact.id === sourceIntent.to);
      if (producedFact && !replayProject.facts.some(fact => fact.id === producedFact.id)) {
        replayProject.facts.push(this.cloneData(producedFact));
      }
    },

    applyReplayFrame(frameIndex, options = {}) {
      if (!this.replay.active) return;
      const { reinitialize = false } = options;
      const frame = this.replay.frames[frameIndex];
      if (!frame) return;

      this.replay.frameIndex = frameIndex;
      this.project = this.cloneData(frame.project);
      this.replay.visibleEvents = this.replay.frames
        .slice(0, frameIndex + 1)
        .map(item => this.cloneData(item.event))
        .filter(entry => entry.type !== 'reason_started')
        .map((entry, index, list) => ({ ...entry, isLast: index === list.length - 1 }));
      this._timelineEventsCacheProject = null;
      this._timelineEventsCache = [];

      if (reinitialize) {
        this.teardownAutoFit();
        if (this.cy) {
          this.cy.destroy();
          this.cy = null;
        }
        this.$nextTick(() => {
          this.initGraph();
          this.followReplayTimelineTail();
        });
        return;
      }

      this.updateGraph();
      this.followReplayTimelineTail();
    },

    async startProjectReplay() {
      if (!this.project || this.replay.active || !this.selectedProjectId) return;
      try {
        const sourceProject = await this.api('GET', `/projects/${this.selectedProjectId}`);
        const baseEvents = this.timelineEvents().map(event => ({
          ...event,
          meta: [...(event.meta || [])],
          sourceFactIds: [...(event.sourceFactIds || [])],
        }));
        const frames = this.buildReplayFrames(sourceProject, baseEvents);
        if (frames.length === 0) {
          this.showToast('暂无可回放时间线', 'error');
          return;
        }

        const stepMs = String(this.replay.stepMs || '1100');
        this.stopReplayTimer();
        this.replay = {
          active: true,
          playing: true,
          stepMs,
          frameIndex: -1,
          frames,
          visibleEvents: [],
          sourceProject,
          timer: null,
        };
        this.polling = false;
        this.sideTab = 'detail';
        this.selectedNode = null;
        this.selectedFacts = [];
        this.selectedTimelineEntryId = null;
        this.applyReplayFrame(0, { reinitialize: true });
        this.scheduleReplayTick();
      } catch (e) {
        this.showToast(e.message, 'error');
      }
    },

    advanceProjectReplay() {
      if (!this.replay.active) return;
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.replay.playing = false;
        this.stopReplayTimer();
        return;
      }
      this.applyReplayFrame(this.replay.frameIndex + 1);
      this.scheduleReplayTick();
    },

    toggleProjectReplayPlayback() {
      if (!this.replay.active) return;
      if (this.replay.playing) {
        this.replay.playing = false;
        this.stopReplayTimer();
        return;
      }
      if (this.replay.frameIndex >= this.replay.frames.length - 1) {
        this.restartProjectReplay();
        return;
      }
      this.replay.playing = true;
      this.scheduleReplayTick();
    },

    restartProjectReplay() {
      if (!this.replay.active || this.replay.frames.length === 0) return;
      this.stopReplayTimer();
      this.replay.playing = true;
      this.selectedNode = null;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.applyReplayFrame(0, { reinitialize: true });
      this.scheduleReplayTick();
    },

    async exitProjectReplay() {
      if (!this.replay.active) return;
      const projectId = this.selectedProjectId;
      const stepMs = String(this.replay.stepMs || '1100');
      this.stopReplayTimer();
      this.replay = {
        active: false,
        playing: false,
        stepMs,
        frameIndex: -1,
        frames: [],
        visibleEvents: [],
        sourceProject: null,
        timer: null,
      };
      this._timelineEventsCacheProject = null;
      this._timelineEventsCache = [];
      this.polling = true;
      if (!projectId) return;
      await this.loadProject(projectId);
      this.$nextTick(() => {
        this.teardownAutoFit();
        if (this.cy) {
          this.cy.destroy();
          this.cy = null;
        }
        this.initGraph();
      });
    },
  }
};
