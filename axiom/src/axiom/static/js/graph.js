window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.graph = {
  methods: {
    buildElements() {
      const nodes = [];
      const edges = [];
      for (const f of this.project.facts) {
        if (this.project.project.depth_mode && f.id === 'goal') continue;
        const nodeType = this.project.project.depth_mode
          ? (f.id === 'origin' ? 'depth_origin' : f.id === 'goal' ? 'goal' : f.id === 'f001' ? 'depth_plan' : 'depth_fact')
          : (f.id === 'origin' ? 'origin' : f.id === 'goal' ? 'goal' : 'fact');
        const label = this.summarizeFactLabel(f);
        const size = this.factNodeSize(label, nodeType);
        nodes.push({ data: {
          id: f.id,
          label,
          description: f.description,
          nodeType,
          title: this.nodeTitle(nodeType),
          iconType: this.nodeIconType(nodeType),
          width: size.width,
          height: size.height,
          archived: f.archived || false,
        }});
      }
      for (const intent of this.project.intents) {
        const lbl = intent.description;
        if (intent.to) {
          if (this.project.project.depth_mode && (intent.to === 'goal' || intent.from.includes('goal'))) continue;
          for (const src of intent.from) {
            if (this.project.project.depth_mode && src === 'goal') continue;
            edges.push({ data: { id: `${intent.id}_${src}`, source: src, target: intent.to, intentId: intent.id, label: lbl, status: 'concluded', archived: intent.archived || false }});
          }
        } else {
          if (this.project.project.depth_mode && intent.from.includes('goal')) continue;
          const phId = `_ph_${intent.id}`;
          const nodeSize = this.openIntentNodeSize(intent);
          const nodeType = this.openIntentNodeType(intent);
          nodes.push({ data: {
            id: phId,
            label: this.openIntentNodeLabel(intent),
            description: intent.description,
            nodeType,
            title: this.nodeTitle(nodeType),
            iconType: this.nodeIconType(nodeType),
            intentId: intent.id,
            width: nodeSize.width,
            height: nodeSize.height,
            archived: intent.archived || false,
          }});
          for (const src of intent.from) {
            if (this.project.project.depth_mode && src === 'goal') continue;
            edges.push({ data: { id: `${intent.id}_${src}`, source: src, target: phId, intentId: intent.id, label: lbl, status: nodeType, archived: intent.archived || false }});
          }
          if (this.isBootstrapIntent(intent) && !this.project.project.depth_mode) {
            edges.push({ data: { id: `${intent.id}_goal`, source: phId, target: 'goal', intentId: intent.id, label: '', status: nodeType, edgeType: 'bootstrap_scope' }});
          }
        }
      }
      if (!this.project.project.depth_mode) {
        const existingGoalSources = new Set(
          edges
            .filter(edge => edge.data.target === 'goal')
            .map(edge => edge.data.source),
        );
        for (const factId of this.project.goal_from_fact_ids || []) {
          if (!factId || factId === 'goal' || existingGoalSources.has(factId)) continue;
          if (!nodes.some(node => node.data.id === factId)) continue;
          edges.push({ data: {
            id: `_goal_evidence_${factId}`,
            source: factId,
            target: 'goal',
            label: 'Goal 达成证据',
            status: 'goal_evidence',
            edgeType: 'goal_evidence',
          }});
        }
        if (this.showGoalHistory) {
          for (const history of this.project.goal_history || []) {
            const historyNodeId = `_goal_history_${history.goal_cycle_id}`;
            const label = history.goal || `第 ${history.sequence} 轮 Goal`;
            const size = this.factNodeSize(label, 'goal');
            nodes.push({ data: {
              id: historyNodeId,
              label,
              description: label,
              nodeType: 'goal_history',
              title: `历史目标 · 第 ${history.sequence} 轮`,
              iconType: 'target',
              width: size.width,
              height: size.height,
              completedAt: history.completed_at || '',
            }});
            for (const factId of history.evidence_fact_ids || []) {
              if (!nodes.some(node => node.data.id === factId)) continue;
              edges.push({ data: {
                id: `_history_goal_evidence_${history.goal_cycle_id}_${factId}`,
                source: factId,
                target: historyNodeId,
                label: `第 ${history.sequence} 轮达成证据`,
                status: 'goal_evidence_history',
                edgeType: 'goal_evidence_history',
              }});
            }
          }
        }
      }
      return { nodes, edges };
    },

    nodeIconSvg(iconType) {
      const paths = {
        flag: '<path d="M4 22V4.5C4 4.2 4.2 4 4.5 4L14 3c4-0.5 6 1.5 6 4s-2 4.5-6 4L8 11.5V22" stroke-linejoin="round"/>',
        target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/>',
        doc: '<path d="M7 3h7l5 5v13a1 1 0 01-1 1H7a1 1 0 01-1-1V4a1 1 0 011-1z"/><path d="M14 3v5h5"/><path d="M9 13h7M9 17h5"/>',
        route: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.5 6H15a3 3 0 013 3v6.5"/>',
        compass: '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z" fill="currentColor" stroke="none"/>',
        bolt: '<path d="M13 2L4 14h6l-1 8 9-12h-6z" stroke-linejoin="round"/>',
        spinner: '<path d="M12 3a9 9 0 109 9" stroke-linecap="round"/><circle cx="12" cy="12" r="2" fill="currentColor" stroke="none"/>',
        question: '<path d="M9.5 9a2.5 2.5 0 015 0c0 1.5-2.5 2-2.5 3.5"/><circle cx="12" cy="17" r="1" fill="currentColor" stroke="none"/>',
      };
      return `<svg class="node-card__icon" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">${paths[iconType] || paths.doc}</svg>`;
    },

    nodeCardTpl(data) {
      const cardTypes = ['origin','depth_origin','goal','goal_history','fact','depth_plan','depth_fact','bootstrap_pending','bootstrap_running'];
      if (!cardTypes.includes(data.nodeType)) return '';
      const title = data.title || '';
      const icon = this.nodeIconSvg(data.iconType);
      const body = (data.label || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const w = data.width || 60;
      const h = data.height || 40;
      const archivedClass = data.archived ? ' node-card--archived' : '';
      return `<div class="node-card node-card--${data.nodeType}${archivedClass}" style="width:${w}px;height:${h}px">`
        + `<div class="node-card__header">${icon}<span class="node-card__title">${title}</span></div>`
        + `<div class="node-card__body">${body}</div>`
        + `</div>`;
    },

    setupHtmlLabels() {
      if (!this.cy || !this.cy.htmlLabel) return;
      const self = this;
      this.cy.htmlLabel([{
        query: 'node',
        tpl: (d) => self.nodeCardTpl(d),
        halign: 'center',
        valign: 'center',
        halignBox: 'center',
        valignBox: 'center',
      }]);
    },

    initGraph() {
      const container = document.getElementById('cy');
      if (!container) return;
      const { nodes, edges } = this.buildElements();
      const rawCy = cytoscape({
        container,
        elements: [...nodes, ...edges],
        style: this.graphStyles(),
        layout: this.layoutOpts(false),
        minZoom: 0.35, maxZoom: 3.5,
        pixelRatio: 1,
      });
      this.cy = rawCy;
      this.setupHtmlLabels();
      const self = this;
      rawCy.on('tap', 'node', e => self.onNodeTap(e));
      rawCy.on('dbltap', 'node', e => self.onNodeDblTap(e));
      rawCy.on('tap', 'edge', e => self.onEdgeTap(e));
      rawCy.on('tap', e => { if (e.target === rawCy) self.clearSelection(); });
      this.pulseActive();
      this.setupAutoFit();
      this.setupMinimapListeners();
      // Ensure readable zoom after initial fit
      setTimeout(() => {
        if (this.cy && this.cy.zoom() < 0.5) this.cy.zoom(0.5);
        this.drawMinimap();
      }, 100);
    },

    graphStyles() {
      const font = '-apple-system,BlinkMacSystemFont,Inter,sans-serif';
      const dark = document.documentElement.classList.contains('dark');
      const theme = dark ? {
        edgeLabelBackground: '#252a31', edgeLabelText: '#aeb5bf',
        mutedBackground: '#30353d', mutedText: '#aeb5bf', mutedBorder: '#657080',
        cancelledBackground: '#30353d', cancelledText: '#aeb5bf', scopeEdge: '#657080',
        goalGold: '#f6c85f', goalGoldText: '#ffe09a', historyGold: '#b9913f',
      } : {
        edgeLabelBackground: '#ffffff', edgeLabelText: '#64748b',
        mutedBackground: '#f1f5f9', mutedText: '#94a3b8', mutedBorder: '#cbd5e1',
        cancelledBackground: '#e2e8f0', cancelledText: '#64748b', scopeEdge: '#cbd5e1',
        goalGold: '#c58b00', goalGoldText: '#8a6100', historyGold: '#d5b56a',
      };
      return [
        // ── Card nodes: transparent (visuals via html-label overlay) ──
        { selector: 'node[nodeType="origin"]', style: {
          shape:'round-rectangle', 'background-color':'transparent', 'background-opacity':0,
          label:'', width:'data(width)', height:'data(height)',
          'border-width':0, padding:'0',
        }},
        { selector: 'node[nodeType="goal"]', style: {
          shape:'round-rectangle', 'background-color':'transparent', 'background-opacity':0,
          label:'', width:'data(width)', height:'data(height)',
          'border-width':0, padding:'0',
        }},
        { selector: 'node[nodeType="goal_history"]', style: {
          shape:'round-rectangle', 'background-color':'transparent', 'background-opacity':0,
          label:'', width:'data(width)', height:'data(height)',
          'border-width':0, padding:'0', opacity:0.82,
        }},
        { selector: 'node[nodeType="fact"]', style: {
          shape:'round-rectangle', 'background-color':'transparent', 'background-opacity':0,
          label:'', width:'data(width)', height:'data(height)',
          'border-width':0, padding:'0',
        }},
        { selector: 'node[nodeType="depth_origin"]', style: {
          shape:'round-rectangle', 'background-color':'transparent', 'background-opacity':0,
          label:'', width:'data(width)', height:'data(height)',
          'border-width':0, padding:'0',
        }},
        { selector: 'node[nodeType="depth_plan"]', style: {
          shape:'round-rectangle', 'background-color':'transparent', 'background-opacity':0,
          label:'', width:'data(width)', height:'data(height)',
          'border-width':0, padding:'0',
        }},
        { selector: 'node[nodeType="depth_fact"]', style: {
          shape:'round-rectangle', 'background-color':'transparent', 'background-opacity':0,
          label:'', width:'data(width)', height:'data(height)',
          'border-width':0, padding:'0',
        }},

        // ── Intent nodes (small circles, keep native label) ──
        { selector: 'node[nodeType="in_progress"]', style: {
          shape:'ellipse', 'background-color':'#f59e0b', 'background-opacity':0.85,
          label:'data(label)', color:'#fff', 'font-size':'11px', 'font-weight':'bold',
          width:26, height:26, 'border-width':2.5, 'border-color':'#fbbf24',
          'text-valign':'center', 'text-halign':'center', 'font-family':font,
        }},
        { selector: 'node[nodeType="unclaimed"]', style: {
          shape:'ellipse', 'background-color':theme.mutedBackground, 'background-opacity':0.8,
          label:'data(label)', color:theme.mutedText, 'font-size':'11px', 'font-weight':'bold',
          width:22, height:22, 'border-width':1.5, 'border-color':theme.mutedBorder, 'border-style':'dashed',
          'text-valign':'center', 'text-halign':'center', 'font-family':font,
        }},
        { selector: 'node[nodeType="intent_completed"]', style: {
          shape:'ellipse', 'background-color':'#10b981', 'background-opacity':0.92,
          label:'data(label)', color:'#fff', 'font-size':'12px', 'font-weight':'bold',
          width:24, height:24, 'border-width':2, 'border-color':'#6ee7b7',
          'text-valign':'center', 'text-halign':'center', 'font-family':font,
        }},
        { selector: 'node[nodeType="intent_cancelled"]', style: {
          shape:'ellipse', 'background-color':theme.cancelledBackground, 'background-opacity':0.95,
          label:'data(label)', color:theme.cancelledText, 'font-size':'12px', 'font-weight':'bold',
          width:24, height:24, 'border-width':1.5, 'border-color':theme.mutedBorder,
          'text-valign':'center', 'text-halign':'center', 'font-family':font,
        }},
        { selector: 'node[nodeType="intent_failed"]', style: {
          shape:'ellipse', 'background-color':'#ef4444', 'background-opacity':0.9,
          label:'data(label)', color:'#fff', 'font-size':'12px', 'font-weight':'bold',
          width:24, height:24, 'border-width':2, 'border-color':'#fca5a5',
          'text-valign':'center', 'text-halign':'center', 'font-family':font,
        }},
        { selector: 'node[nodeType="bootstrap_pending"]', style: {
          shape:'round-rectangle', 'background-color':'transparent', 'background-opacity':0,
          label:'', width:'data(width)', height:'data(height)',
          'border-width':0, padding:'0',
        }},
        { selector: 'node[nodeType="bootstrap_running"]', style: {
          shape:'round-rectangle', 'background-color':'transparent', 'background-opacity':0,
          label:'', width:'data(width)', height:'data(height)',
          'border-width':0, padding:'0',
        }},

        // ── Edges: curved bezier with arrows ──
        { selector: 'edge[status="concluded"]', style: {
          width:2, 'line-color':'#22c55e', 'target-arrow-color':'#22c55e',
          'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'7px', color:theme.edgeLabelText,
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-max-width':'80px', 'text-wrap':'ellipsis',
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.9,
          'text-background-padding':'2px', 'text-events':'yes', 'arrow-scale':0.9,
        }},
        { selector: 'edge[status="goal_evidence"]', style: {
          width:3, 'line-color':theme.goalGold, 'target-arrow-color':theme.goalGold,
          'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'8px', color:theme.goalGoldText,
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.92,
          'text-background-padding':'2px', 'arrow-scale':1,
        }},
        { selector: 'edge[status="goal_evidence_history"]', style: {
          width:2, 'line-color':theme.historyGold, 'target-arrow-color':theme.historyGold,
          'line-style':'dashed', 'line-dash-pattern':[7,5], opacity:0.7,
          'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'7px', color:theme.historyGold,
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.9,
          'text-background-padding':'2px', 'arrow-scale':0.85,
        }},
        { selector: 'edge[status="in_progress"]', style: {
          width:2, 'line-color':'#f59e0b', 'line-style':'dashed', 'line-dash-pattern':[8,4],
          'target-arrow-color':'#f59e0b', 'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'7px', color:'#b45309',
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-max-width':'80px', 'text-wrap':'ellipsis',
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.9,
          'text-background-padding':'2px', 'text-events':'yes', 'arrow-scale':0.9,
        }},
        { selector: 'edge[status="unclaimed"]', style: {
          width:1.5, 'line-color':theme.mutedBorder, 'line-style':'dashed', 'line-dash-pattern':[5,5],
          'target-arrow-color':theme.mutedBorder, 'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'7px', color:theme.mutedText,
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-max-width':'80px', 'text-wrap':'ellipsis',
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.9,
          'text-background-padding':'2px', 'text-events':'yes', 'arrow-scale':0.7,
        }},
        { selector: 'edge[status="intent_completed"]', style: {
          width:2, 'line-color':'#34d399', 'target-arrow-color':'#34d399',
          'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'7px', color:'#047857',
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-max-width':'80px', 'text-wrap':'ellipsis',
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.9,
          'text-background-padding':'2px', 'text-events':'yes', 'arrow-scale':0.8,
        }},
        { selector: 'edge[status="intent_cancelled"]', style: {
          width:1.5, 'line-color':'#94a3b8', 'line-style':'dashed', 'line-dash-pattern':[4,5],
          'target-arrow-color':theme.mutedBorder, 'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'7px', color:theme.edgeLabelText,
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-max-width':'80px', 'text-wrap':'ellipsis',
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.9,
          'text-background-padding':'2px', 'text-events':'yes', 'arrow-scale':0.7,
        }},
        { selector: 'edge[status="intent_failed"]', style: {
          width:1.8, 'line-color':'#f87171', 'line-style':'dashed', 'line-dash-pattern':[6,4],
          'target-arrow-color':'#f87171', 'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'7px', color:'#b91c1c',
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-max-width':'80px', 'text-wrap':'ellipsis',
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.9,
          'text-background-padding':'2px', 'text-events':'yes', 'arrow-scale':0.75,
        }},
        { selector: 'edge[status="bootstrap_pending"]', style: {
          width:2, 'line-color':'#fdba74', 'line-style':'dashed', 'line-dash-pattern':[8,4],
          'target-arrow-color':'#fdba74', 'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'7px', color:'#c2410c',
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-max-width':'88px', 'text-wrap':'ellipsis',
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.9,
          'text-background-padding':'2px', 'text-events':'yes', 'arrow-scale':0.85,
        }},
        { selector: 'edge[status="bootstrap_running"]', style: {
          width:2.5, 'line-color':'#fb923c', 'line-style':'dashed', 'line-dash-pattern':[10,4],
          'target-arrow-color':'#fb923c', 'target-arrow-shape':'triangle', 'curve-style':'bezier',
          label:'data(label)', 'font-size':'7px', color:'#c2410c',
          'text-rotation':'autorotate', 'text-margin-y':-9,
          'text-max-width':'88px', 'text-wrap':'ellipsis',
          'text-background-color':theme.edgeLabelBackground, 'text-background-opacity':0.9,
          'text-background-padding':'2px', 'text-events':'yes', 'arrow-scale':0.95,
        }},
        { selector: 'edge[edgeType="bootstrap_scope"]', style: {
          label:'', width:1.8, 'curve-style':'bezier',
          'line-style':'dotted', 'line-dash-pattern':[2,5],
          'target-arrow-shape':'triangle-backcurve', 'arrow-scale':0.75,
          'target-distance-from-node':2, 'line-color':theme.scopeEdge,
        }},

        // ── Interaction states (overlay glow, not border — card owns border) ──
        { selector: '.highlight', style: { 'z-index':999 }},
        { selector: 'edge.highlight', style: { 'z-index':999, width:3 }},
        { selector: 'node.focus', style: {
          'overlay-color':'#3b82f6', 'overlay-opacity':0.18, 'overlay-padding':4, 'z-index':1000,
        }},
        { selector: 'edge.focus', style: {
          'z-index':1000, 'overlay-color':'#3b82f6', 'overlay-opacity':0.22, 'overlay-padding':5,
        }},
        { selector: 'node.selected-fact', style: {
          'overlay-color':'#3b82f6', 'overlay-opacity':0.25, 'overlay-padding':6, 'z-index':1001,
        }},
        { selector: '.faded', style: { opacity:0.25 }},
        { selector: '.fresh', style: { opacity:0 }},
        // ── Pulse animation via CSS class (overlay glow, not border) ──
        { selector: 'node.node-pulse', style: {
          'overlay-color':'#f59e0b', 'overlay-opacity':0.3, 'overlay-padding':4,
        }},
      ];
    },

    applyGraphTheme() {
      if (!this.cy) return;
      this.cy.style().fromJson(this.graphStyles()).update();
      this.refreshGraphDecorations();
      this.drawMinimap();
    },

    layoutOpts(animate = true) {
      const direction = this.layoutDirection();
      if (this.layoutEngine() === 'elk') {
        const elkDirection = direction === 'TB' ? 'DOWN' : 'RIGHT';
        return {
          name: 'elk',
          fit: true,
          padding: 50,
          animate,
          animationDuration: 350,
          animationEasing: 'ease-in-out-cubic',
          elk: {
            algorithm: 'layered',
            'elk.direction': elkDirection,
            'elk.aspectRatio': '1.5',
            'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
            'elk.spacing.nodeNode': '50',
            'elk.layered.spacing.nodeNodeBetweenLayers': '80',
            'elk.spacing.edgeNode': '25',
            'elk.layered.crossingMinimization.strategy': 'LAYER_SWEEP',
            'elk.layered.nodePlacement.bk.fixedAlignment': 'BALANCED',
          },
        };
      }
      return {
        name: 'dagre',
        rankDir: direction,
        nodeSep: 60,
        rankSep: 80,
        padding: 50,
        fit: true,
        animate,
        animationDuration: 400,
        animationEasing: 'ease-in-out-cubic',
      };
    },

    snapshotNodePositions() {
      const positions = new Map();
      if (!this.cy) return positions;
      this.cy.nodes().forEach(node => {
        positions.set(node.id(), { x: node.position('x'), y: node.position('y') });
      });
      return positions;
    },

    anchorPositionFromIds(nodeIds, previousPositions, offset = 36) {
      const anchors = [];
      for (const nodeId of nodeIds) {
        const existing = this.cy?.getElementById(nodeId);
        if (existing?.length) {
          anchors.push({ x: existing.position('x'), y: existing.position('y') });
          continue;
        }
        const previous = previousPositions.get(nodeId);
        if (previous) anchors.push(previous);
      }
      if (anchors.length === 0) return null;
      const center = anchors.reduce((acc, pos) => ({ x: acc.x + pos.x, y: acc.y + pos.y }), { x: 0, y: 0 });
      const avg = { x: center.x / anchors.length, y: center.y / anchors.length };
      return this.layoutDirection() === 'TB'
        ? { x: avg.x, y: avg.y + offset }
        : { x: avg.x + offset, y: avg.y };
    },

    initialPositionForNode(nodeData, previousPositions) {
      if (!this.project) return null;
      if (nodeData.intentId) {
        const previousPlaceholder = previousPositions.get(nodeData.id);
        if (previousPlaceholder) return previousPlaceholder;
        const intent = this.project.intents.find(item => item.id === nodeData.intentId);
        return intent ? this.anchorPositionFromIds(intent.from, previousPositions, 32) : null;
      }

      const producingIntent = this.project.intents.find(item => item.to === nodeData.id);
      if (!producingIntent) return null;
      const placeholderPosition = previousPositions.get(`_ph_${producingIntent.id}`);
      if (placeholderPosition) return placeholderPosition;
      return this.anchorPositionFromIds(producingIntent.from, previousPositions, 44);
    },

    fadeInFreshElement(element) {
      if (!element || element.length === 0) return;
      element.addClass('fresh');
      setTimeout(() => {
        if (!element.inside()) return;
        element.animate(
          { style: { opacity: 1 } },
          {
            duration: 500,
            complete: () => {
              if (!element.inside()) return;
              element.removeStyle('opacity');
            },
          },
        );
        element.removeClass('fresh');
      }, 30);
    },

    updateGraph() {
      if (!this.cy || !this.project) return;
      const { nodes, edges } = this.buildElements();
      const wantNodes = new Set(nodes.map(n => n.data.id));
      const wantEdges = new Set(edges.map(e => e.data.id));
      const previousPositions = this.snapshotNodePositions();
      let topologyChanged = false;
      let geometryChanged = false;
      let visualChanged = false;

      this.cy.nodes().forEach(n => { if (!wantNodes.has(n.id())) { n.remove(); topologyChanged = true; } });
      this.cy.edges().forEach(e => { if (!wantEdges.has(e.id())) { e.remove(); topologyChanged = true; } });

      for (const n of nodes) {
        const ex = this.cy.getElementById(n.data.id);
        if (ex.length === 0) {
          const initialPosition = this.initialPositionForNode(n.data, previousPositions);
          const a = this.cy.add(initialPosition ? { ...n, position: initialPosition } : n);
          this.fadeInFreshElement(a);
          topologyChanged = true;
        } else {
          const nextData = n.data;
          const sizeChanged = ex.data('width') !== nextData.width || ex.data('height') !== nextData.height;
          const dataChanged = Object.entries(nextData).some(([key, value]) => ex.data(key) !== value);
          if (dataChanged) {
            ex.data(nextData);
            visualChanged = true;
            geometryChanged ||= sizeChanged;
          }
        }
      }
      for (const e of edges) {
        const ex = this.cy.getElementById(e.data.id);
        if (ex.length === 0) {
          const a = this.cy.add(e);
          this.fadeInFreshElement(a);
          topologyChanged = true;
        } else if (ex.source().id() !== e.data.source || ex.target().id() !== e.data.target) {
          ex.remove();
          this.cy.add(e);
          topologyChanged = true;
        } else if (Object.entries(e.data).some(([key, value]) => ex.data(key) !== value)) {
          ex.data(e.data);
          visualChanged = true;
        }
      }
      if (topologyChanged || geometryChanged) {
        this.cy.layout(this.layoutOpts()).run();
        setTimeout(() => {
          if (this.cy && this.cy.zoom() < 0.5) this.cy.zoom(0.5);
          this.drawMinimap();
        }, 450);
      }
      this.pulseActive();
      if (topologyChanged || geometryChanged || visualChanged) this.refreshGraphDecorations();
    },

    pulseActive() {
      if (!this.cy) return;
      this.cy.nodes('[nodeType="in_progress"], [nodeType="bootstrap_running"]').forEach(node => {
        if (!node.hasClass('node-pulse')) node.addClass('node-pulse');
      });
      this.cy.nodes('.node-pulse').forEach(node => {
        const nt = node.data('nodeType');
        if (nt !== 'in_progress' && nt !== 'bootstrap_running') {
          node.removeClass('node-pulse');
        }
      });
      this.startGraphActivityAnimation();
    },

    startGraphActivityAnimation() {
      if (this._graphActivityRaf || !this.cy || document.hidden) return;
      const tick = timestamp => {
        if (!this.cy || this.activePage !== 'task-detail' || this.view !== 'graph' || document.hidden) {
          this.stopGraphActivityAnimation();
          return;
        }
        const activeEdges = this.cy.edges('[status="in_progress"], [status="bootstrap_running"]');
        if (!activeEdges.length) {
          this.stopGraphActivityAnimation();
          return;
        }
        const offset = -((timestamp / 45) % 16);
        activeEdges.style('line-dash-offset', offset);
        this._graphActivityRaf = requestAnimationFrame(tick);
      };
      this._graphActivityRaf = requestAnimationFrame(tick);
    },

    stopGraphActivityAnimation() {
      if (this._graphActivityRaf) cancelAnimationFrame(this._graphActivityRaf);
      this._graphActivityRaf = null;
      if (this.cy) this.cy.edges('[status="in_progress"], [status="bootstrap_running"]').removeStyle('line-dash-offset');
    },

    fitGraph() {
      if (this.cy) {
        this.cy.elements().removeClass('highlight faded');
        this.cy.fit(undefined, 50);
        if (this.cy.zoom() < 0.5) this.cy.zoom(0.5);
      }
    },

    minimapNodeColor(nodeType) {
      const colors = {
        origin: '#14b8a6', depth_origin: '#14b8a6',
        goal: '#f43f5e', fact: '#94a3b8',
        depth_plan: '#6366f1', depth_fact: '#f59e0b',
        bootstrap_pending: '#fb923c', bootstrap_running: '#ea580c',
        in_progress: '#f59e0b', unclaimed: '#cbd5e1',
        intent_completed: '#10b981', intent_cancelled: '#94a3b8', intent_failed: '#ef4444',
      };
      return colors[nodeType] || '#94a3b8';
    },

    setupMinimapListeners() {
      if (!this.cy) return;
      const self = this;
      const schedule = () => {
        if (self._minimapRaf) return;
        self._minimapRaf = requestAnimationFrame(() => {
          self._minimapRaf = null;
          self.drawMinimap();
        });
      };
      this.cy.on('pan zoom layoutstop add remove', schedule);
      this._minimapSchedule = schedule;
    },

    drawMinimap() {
      const canvas = document.getElementById('minimap');
      if (!canvas || !this.cy || !this.cy.nodes().length) return;
      const ctx = canvas.getContext('2d');
      const W = canvas.width, H = canvas.height;
      ctx.clearRect(0, 0, W, H);

      const nodes = this.cy.nodes();
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      nodes.forEach(n => {
        const p = n.position();
        minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
        maxX = Math.max(maxX, p.x); maxY = Math.max(maxY, p.y);
      });
      const graphW = Math.max(1, maxX - minX);
      const graphH = Math.max(1, maxY - minY);
      const pad = 6;
      const scale = Math.min((W - pad * 2) / graphW, (H - pad * 2) / graphH);
      const offX = (W - graphW * scale) / 2 - minX * scale;
      const offY = (H - graphH * scale) / 2 - minY * scale;

      nodes.forEach(n => {
        const p = n.position();
        const x = p.x * scale + offX;
        const y = p.y * scale + offY;
        ctx.fillStyle = this.minimapNodeColor(n.data('nodeType'));
        ctx.beginPath();
        ctx.arc(x, y, 2, 0, Math.PI * 2);
        ctx.fill();
      });

      const extent = this.cy.extent();
      const vx1 = extent.x1 * scale + offX;
      const vy1 = extent.y1 * scale + offY;
      const vw = (extent.x2 - extent.x1) * scale;
      const vh = (extent.y2 - extent.y1) * scale;
      ctx.strokeStyle = '#3b82f6';
      ctx.lineWidth = 1.2;
      ctx.setLineDash([3, 2]);
      ctx.strokeRect(vx1, vy1, vw, vh);
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(59,130,246,0.08)';
      ctx.fillRect(vx1, vy1, vw, vh);
    },

    onMinimapPointerDown(e) {
      this._minimapDragging = true;
      this.centerOnMinimapPoint(e);
    },

    onMinimapPointerMove(e) {
      if (!this._minimapDragging) return;
      this.centerOnMinimapPoint(e);
    },

    onMinimapPointerUp() {
      this._minimapDragging = false;
    },

    centerOnMinimapPoint(e) {
      if (!this.cy) return;
      const canvas = e.currentTarget;
      const rect = canvas.getBoundingClientRect();
      const cx = (e.clientX - rect.left) / rect.width * canvas.width;
      const cy = (e.clientY - rect.top) / rect.height * canvas.height;

      const nodes = this.cy.nodes();
      if (nodes.length === 0) return;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      nodes.forEach(n => {
        const p = n.position();
        minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
        maxX = Math.max(maxX, p.x); maxY = Math.max(maxY, p.y);
      });
      const graphW = Math.max(1, maxX - minX);
      const graphH = Math.max(1, maxY - minY);
      const pad = 6;
      const scale = Math.min((canvas.width - pad * 2) / graphW, (canvas.height - pad * 2) / graphH);
      const offX = (canvas.width - graphW * scale) / 2 - minX * scale;
      const offY = (canvas.height - graphH * scale) / 2 - minY * scale;

      const targetX = (cx - offX) / scale;
      const targetY = (cy - offY) / scale;
      this.cy.panBy({ x: this.cy.width() / 2 - this.cy.pan().x - targetX * this.cy.zoom(), y: this.cy.height() / 2 - this.cy.pan().y - targetY * this.cy.zoom() });
      this.drawMinimap();
    },

    searchGraphNodes() {
      if (!this.cy) return;
      const q = this.graphSearch.trim().toLowerCase();
      if (!q) {
        this.cy.nodes().removeClass('faded');
        return;
      }
      this.cy.nodes().forEach(node => {
        const label = (node.data('label') || '').toLowerCase();
        const desc = (node.data('description') || '').toLowerCase();
        const id = (node.id() || '').toLowerCase();
        if (label.includes(q) || desc.includes(q) || id.includes(q)) {
          node.removeClass('faded');
          node.animate({ style: { 'overlay-color':'#3b82f6', 'overlay-opacity':0.25, 'overlay-padding':5 } }, { duration: 200 });
          setTimeout(() => {
            if (node.inside()) node.removeStyle('overlay-color');
            if (node.inside()) node.removeStyle('overlay-opacity');
            if (node.inside()) node.removeStyle('overlay-padding');
          }, 2000);
        } else {
          node.addClass('faded');
        }
      });
      const matched = this.cy.nodes().filter(n => !n.hasClass('faded'));
      if (matched.length > 0 && matched.length < this.cy.nodes().length) {
        this.cy.animate({ fit: { eles: matched, padding: 80 } }, { duration: 300 });
      }
    },

    centerGraphOnElements(eles) {
      if (!this.cy || !eles || eles.length === 0) return;
      if (this._centerAnimation) this._centerAnimation.stop();
      this._centerAnimation = this.cy.animation({ center: { eles }, duration: 220, easing: 'ease-in-out-cubic' });
      this._centerAnimation.play();
    },

    centerGraphOnFact(factId) {
      if (!this.cy || !factId) return;
      const node = this.cy.getElementById(factId);
      if (node.length > 0) this.centerGraphOnElements(node);
    },

    centerGraphOnIntent(intentId) {
      if (!this.cy || !intentId) return;
      const edges = this.cy.edges(`[intentId="${intentId}"]`);
      if (edges.length === 0) return;
      this.centerGraphOnElements(edges.add(edges.sources()).add(edges.targets()));
    },

    summarizeFactLabel(fact) {
      if (fact.id === 'origin') {
        const d = (fact.description || '').replace(/\s+/g, ' ').trim();
        return d || '起点';
      }
      if (fact.id === 'goal') {
        const d = (fact.description || '').replace(/\s+/g, ' ').trim();
        return d || '目标';
      }
      const normalized = fact.description.replace(/\s+/g, ' ').trim();
      const chars = Array.from(normalized);
      if (chars.length <= 24) return normalized || fact.id;
      return `${chars.slice(0, 24).join('')}…`;
    },

    nodeTitle(nodeType) {
      switch (nodeType) {
        case 'origin':
        case 'depth_origin': return '起点';
        case 'goal': return '目标';
        case 'fact': return '事实';
        case 'depth_plan': return '计划';
        case 'depth_fact': return '探索';
        case 'bootstrap_pending': return 'Boot·待';
        case 'bootstrap_running': return 'Boot·运行';
        case 'in_progress': return '进行中';
        case 'unclaimed': return '待领取';
        case 'intent_completed': return '已完成';
        case 'intent_cancelled': return '已取消';
        case 'intent_failed': return '执行失败';
        default: return '';
      }
    },

    nodeIconType(nodeType) {
      switch (nodeType) {
        case 'origin':
        case 'depth_origin': return 'flag';
        case 'goal': return 'target';
        case 'fact': return 'doc';
        case 'depth_plan': return 'route';
        case 'depth_fact': return 'compass';
        case 'bootstrap_pending':
        case 'bootstrap_running': return 'bolt';
        case 'in_progress': return 'spinner';
        case 'unclaimed': return 'question';
        case 'intent_completed': return 'doc';
        case 'intent_cancelled': return 'doc';
        case 'intent_failed': return 'doc';
        default: return 'doc';
      }
    },

    factNodeSize(label, nodeType) {
      const headerHeight = 17;
      const preset = (nodeType === 'fact' || nodeType === 'depth_fact' || nodeType === 'depth_origin')
        ? { fontSize: 10, maxTextWidth: 116, minWidth: 52, minHeight: 48, paddingX: 10, paddingY: 8 }
        : { fontSize: 11, maxTextWidth: 92, minWidth: 58, minHeight: 52, paddingX: 10, paddingY: 8 };

      const measured = this.measureWrappedText(label, preset.maxTextWidth, preset.fontSize);
      return {
        width: Math.max(preset.minWidth, Math.ceil(measured.width + preset.paddingX * 2)),
        height: Math.max(preset.minHeight, Math.ceil(measured.height + preset.paddingY * 2 + headerHeight)),
      };
    },

    measureWrappedText(text, maxWidth, fontSize) {
      const content = (text || '').trim() || ' ';
      const lines = [];
      let currentWidth = 0;
      let currentChars = 0;
      let maxLineWidth = 0;

      const pushLine = () => {
        if (currentChars === 0 && lines.length > 0) lines.push(0);
        else if (currentChars > 0) lines.push(currentWidth);
        maxLineWidth = Math.max(maxLineWidth, currentWidth);
        currentWidth = 0;
        currentChars = 0;
      };

      for (const char of Array.from(content)) {
        if (char === '\n') {
          pushLine();
          continue;
        }
        const charWidth = this.estimateLabelCharWidth(char, fontSize);
        if (currentChars > 0 && currentWidth + charWidth > maxWidth) pushLine();
        currentWidth += charWidth;
        currentChars += 1;
      }

      pushLine();

      const lineCount = Math.max(1, lines.length);
      const lineHeight = fontSize * 1.35;
      return {
        width: Math.min(maxWidth, Math.max(fontSize * 1.6, maxLineWidth)),
        height: lineCount * lineHeight,
      };
    },

    estimateLabelCharWidth(char, fontSize) {
      if (/\s/.test(char)) return fontSize * 0.35;
      if (/[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE10-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/.test(char)) {
        return fontSize * 1.0;
      }
      return fontSize * 0.58;
    },

    setupAutoFit() {
      this.teardownAutoFit();
      const container = document.getElementById('cy');
      if (!container || !this.cy) return;
      let fitTimer = null;
      this._resizeObserver = new ResizeObserver(() => {
        clearTimeout(fitTimer);
        fitTimer = setTimeout(() => {
          if (this.cy) {
            const clampedWidth = this.clampPanelWidth(this.sidePanelWidth);
            if (clampedWidth !== this.sidePanelWidth) {
              this.sidePanelWidth = clampedWidth;
              this.saveSidePanelWidth();
            }
            this.cy.resize();
            this.cy.fit(undefined, 50);
            if (this.cy.zoom() < 0.5) this.cy.zoom(0.5);
            this.drawMinimap();
          }
        }, 500);
      });
      this._resizeObserver.observe(container);
    },

    teardownAutoFit() {
      if (this._resizeObserver) {
        this._resizeObserver.disconnect();
        this._resizeObserver = null;
      }
    },
    applySelectedLayout() {
      this.layoutMode = this.isValidLayoutMode(this.layoutMode) ? this.layoutMode : 'elk_tb';
      this.localPrefs.layout_mode = this.layoutMode;
      this.saveLocalPrefs();
      if (this.cy) this.cy.layout(this.layoutOpts()).run();
    },

    clampPanelWidth(width) {
      const containerWidth = document.getElementById('graphLayout')?.getBoundingClientRect().width || window.innerWidth;
      const min = 260;
      const max = Math.max(min, containerWidth - 260);
      return Math.min(max, Math.max(min, width));
    },

    startPanelResize(e) {
      e.currentTarget?.setPointerCapture?.(e.pointerId);
      this.isResizingPanel = true;
      this.onPanelResize(e);
    },

    onPanelResize(e) {
      if (!this.isResizingPanel) return;
      const rect = document.getElementById('graphLayout')?.getBoundingClientRect();
      if (!rect) return;
      this.sidePanelWidth = this.clampPanelWidth(rect.right - e.clientX);
    },

    stopPanelResize() {
      if (!this.isResizingPanel) return;
      this.isResizingPanel = false;
      this.saveSidePanelWidth();
    },

    clearGraphSelection(preserveTimeline = false) {
      this.selectedNode = null;
      this.selectedFacts = [];
      if (!preserveTimeline) this.selectedTimelineEntryId = null;
      if (this.cy) this.cy.elements().removeClass('highlight focus faded selected-fact');
    },

    clearSelection() {
      this.clearGraphSelection(false);
    },

    toggleFactSelection(fid) {
      this.selectedTimelineEntryId = null;
      const idx = this.selectedFacts.indexOf(fid);
      if (idx >= 0) {
        this.selectedFacts.splice(idx, 1);
        if (this.selectedFacts.length === 0) {
          this.clearSelection();
          return;
        }
        if (this.selectedNode?.type === 'fact' && this.selectedNode.id === fid) {
          this.selectedNode = { type:'fact', id: this.selectedFacts[this.selectedFacts.length - 1] };
        }
      } else {
        this.selectedFacts.push(fid);
        this.selectedNode = { type:'fact', id: fid };
      }
      this.refreshGraphDecorations();
    },

    removeFactSelection(fid) {
      this.selectedTimelineEntryId = null;
      const idx = this.selectedFacts.indexOf(fid);
      if (idx < 0) return;
      this.selectedFacts.splice(idx, 1);
      if (this.selectedFacts.length === 0) {
        this.clearSelection();
        return;
      }
      if (this.selectedNode?.type === 'fact' && this.selectedNode.id === fid) {
        this.selectedNode = { type:'fact', id: this.selectedFacts[this.selectedFacts.length - 1] };
      }
      this.refreshGraphDecorations();
    },

    onNodeTap(e) {
      const node = e.target;
      const id = node.data('id');
      const nt = node.data('nodeType');
      const keepLogFocus = this.sideTab === 'log';

      if (['in_progress', 'unclaimed', 'bootstrap_pending', 'bootstrap_running', 'intent_completed', 'intent_cancelled', 'intent_failed'].includes(nt)) {
        this.selectIntent(node.data('intentId'));
        if (keepLogFocus) this.scrollTimelineToSelection();
        else this.sideTab = 'detail';
        return;
      }
      if (e.originalEvent.shiftKey) {
        this.toggleFactSelection(id);
        if (keepLogFocus) this.scrollTimelineToSelection();
        else this.sideTab = 'detail';
        return;
      } else {
        this.selectedFacts = [id];
        this.selectFact(id);
      }
      if (keepLogFocus) this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
    },

    onNodeDblTap(e) {
      if (!this.cy) return;
      const node = e.target;
      const sub = node.predecessors().add(node.successors()).add(node);
      this.cy.elements().removeClass('highlight focus faded');
      sub.addClass('highlight');
      this.cy.elements().not(sub).addClass('faded');
      this.cy.fit(sub, 60);
      if (this.cy.zoom() < 0.5) this.cy.zoom(0.5);
    },

    onEdgeTap(e) {
      const intentId = e.target.data('intentId');
      if (intentId) this.selectIntent(intentId);
      else if (['goal_evidence', 'goal_evidence_history'].includes(e.target.data('edgeType'))) this.selectFact(e.target.data('source'));
      if (this.sideTab === 'log') this.scrollTimelineToSelection();
      else this.sideTab = 'detail';
    },

    selectIntent(intent) {
      const intentId = typeof intent === 'string' ? intent : intent?.id;
      if (!intentId) return;
      this.selectedFacts = [];
      this.selectedTimelineEntryId = null;
      this.selectedNode = { type:'intent', id: intentId };
      this.applyLineageHighlightForIntent(intentId);
    },

    selectFact(fact) {
      const factId = typeof fact === 'string' ? fact : fact?.id;
      if (!factId) return;
      if (!this.selectedFacts.includes(factId)) this.selectedFacts = [factId];
      this.selectedTimelineEntryId = null;
      this.selectedNode = { type:'fact', id: factId };
      if (this.selectedFacts.length > 1) {
        this.applyMultiFactSelectionHighlight();
        return;
      }
      this.applyLineageHighlightForFact(factId);
    },

    refreshGraphDecorations() {
      if (!this.selectedNode) {
        this.syncFactSelections();
        return;
      }
      if (this.selectedNode.type === 'intent') this.applyLineageHighlightForIntent(this.selectedNode.id);
      if (this.selectedNode.type === 'fact') {
        if (this.selectedFacts.length > 1 && this.selectedFacts.includes(this.selectedNode.id)) {
          this.applyMultiFactSelectionHighlight();
          return;
        }
        this.applyLineageHighlightForFact(this.selectedNode.id);
      }
    },

    syncFactSelections() {
      if (!this.cy) return;
      this.cy.nodes().removeClass('selected-fact');
      for (const fid of this.selectedFacts) {
        const node = this.cy.getElementById(fid);
        if (node.length > 0) node.addClass('selected-fact');
      }
    },

    collectFactLineage(fid) {
      const upstreamFacts = new Set();
      const upstreamIntents = new Set();

      const walkFactUpstream = (factId) => {
        if (upstreamFacts.has(factId) || !this.project) return;
        upstreamFacts.add(factId);
        for (const intent of this.project.intents) {
          if (intent.to === factId) walkIntentUpstream(intent.id);
        }
      };

      const walkIntentUpstream = (iid) => {
        if (upstreamIntents.has(iid) || !this.project) return;
        upstreamIntents.add(iid);
        const intent = this.project.intents.find(i => i.id === iid);
        if (!intent) return;
        for (const sourceId of intent.from) walkFactUpstream(sourceId);
      };

      walkFactUpstream(fid);
      return { upstreamFacts, upstreamIntents };
    },

    collectIntentElements(intent, nodeIds, edgeIds) {
      if (intent.to) nodeIds.add(intent.to);
      else nodeIds.add(`_ph_${intent.id}`);
      for (const sourceId of intent.from) {
        nodeIds.add(sourceId);
        edgeIds.add(`${intent.id}_${sourceId}`);
      }
      if (this.isBootstrapIntent(intent)) {
        nodeIds.add('goal');
        edgeIds.add(`${intent.id}_goal`);
      }
    },

    applyLineageHighlightForIntent(intentId) {
      if (!this.cy || !this.project) return;
      const intent = this.project.intents.find(i => i.id === intentId);
      if (!intent) return;
      this.cy.elements().removeClass('highlight focus faded');

      const nodeIds = new Set();
      const edgeIds = new Set();
      this.collectIntentElements(intent, nodeIds, edgeIds);
      const highlightNodes = this.cy.nodes().filter(n => nodeIds.has(n.id()));
      const focusEdges = this.cy.edges().filter(e => edgeIds.has(e.id()));

      highlightNodes.addClass('highlight');
      focusEdges.addClass('focus');

      const visible = highlightNodes.add(focusEdges);
      this.cy.elements().not(visible).addClass('faded');
      this.syncFactSelections();
    },

    applyLineageHighlightForFact(factId) {
      if (!this.cy || !this.project) return;
      const { upstreamFacts, upstreamIntents } = this.collectFactLineage(factId);
      const nodeIds = new Set(upstreamFacts);
      const edgeIds = new Set();

      for (const iid of upstreamIntents) {
        const intent = this.project.intents.find(i => i.id === iid);
        if (intent) this.collectIntentElements(intent, nodeIds, edgeIds);
      }

      this.cy.elements().removeClass('highlight focus faded');
      const highlightNodes = this.cy.nodes().filter(n => nodeIds.has(n.id()));
      const highlightEdges = this.cy.edges().filter(e => edgeIds.has(e.id()));
      const focusNode = this.cy.getElementById(factId);

      highlightNodes.addClass('highlight');
      highlightEdges.addClass('highlight');
      focusNode.addClass('focus');

      const visible = highlightNodes.add(highlightEdges).add(focusNode);
      this.cy.elements().not(visible).addClass('faded');
      this.syncFactSelections();
    },

    applyMultiFactSelectionHighlight() {
      if (!this.cy) return;
      this.cy.elements().removeClass('highlight focus faded selected-fact');
      this.syncFactSelections();
    },

    getProducingIntent(fid) {
      return this.project ? this.project.intents.find(i => i.to === fid) || null : null;
    },

    selectedFactId() {
      return this.selectedNode?.type === 'fact' ? this.selectedNode.id : null;
    },

    selectedFactRecord() {
      const factId = this.selectedFactId();
      if (!this.project || !factId) return null;
      return this.project.facts.find(f => f.id === factId) || null;
    },

    selectedFactProducingIntent() {
      const factId = this.selectedFactId();
      return factId ? this.getProducingIntent(factId) : null;
    },

    selectedFactRecords() {
      if (!this.project || this.selectedFacts.length === 0) return [];
      const factsById = new Map(this.project.facts.map(f => [f.id, f]));
      return this.selectedFacts.map(fid => factsById.get(fid)).filter(Boolean);
    },

    selectedIntentId() {
      return this.selectedNode?.type === 'intent' ? this.selectedNode.id : null;
    },

    selectedIntentRecord() {
      const intentId = this.selectedIntentId();
      if (!this.project || !intentId) return null;
      return this.project.intents.find(i => i.id === intentId) || null;
    },

    intentActivityVisible(intent) {
      return !!intent && (
        ['claimed', 'running', 'concluding'].includes(intent.status)
        || !!intent.activity_summary
      );
    },

    intentActivityTitle(intent) {
      return ['claimed', 'running', 'concluding'].includes(intent?.status) ? '实时活动' : '最后活动';
    },

    intentActivityStateLabel(intent) {
      const labels = {
        starting: '准备中',
        tool_running: '工具执行中',
        tool_result: '分析结果',
        tool_waiting: '等待工具',
        analyzing: '分析结果',
        organizing: '整理产物',
        soft_reminder: '整理产物',
        concluding: '收尾中',
        submitting: '提交结果',
        waiting_retry: '等待重试',
        stopped: '已停止',
        failed: '未完成',
        finished: '已结束',
        cancelled: '已取消',
      };
      return labels[intent?.activity_kind] || (['claimed', 'running', 'concluding'].includes(intent?.status) ? '运行中' : '已结束');
    },

    intentActivityAge(intent) {
      const value = intent?.activity_updated_at;
      if (!value) return '尚未上报';
      const elapsed = Math.max(0, Date.now() - Date.parse(value));
      if (!Number.isFinite(elapsed)) return this.formatTime(value);
      const seconds = Math.floor(elapsed / 1000);
      if (seconds < 5) return '刚刚';
      if (seconds < 60) return `${seconds} 秒前`;
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return `${minutes} 分钟前`;
      return this.formatTime(value);
    },

    intentRunDuration(intent) {
      const started = intent?.activity_started_at;
      if (!started) return '—';
      const end = intent?.concluded_at || new Date().toISOString();
      const elapsed = Math.max(0, Date.parse(end) - Date.parse(started));
      return Number.isFinite(elapsed) ? this.formatDurationMs(elapsed) : '—';
    },

    reasonActivityStateLabel(reason) {
      const labels = {
        pending: '等待领取',
        claimed: '已领取',
        running: '运行中',
        concluding: '收口中',
        starting: '准备中',
        reading_context: '读取上下文',
        analyzing: '分析规划中',
        updating_todos: '更新 Todo',
        planning_intents: '规划 Intent',
        submitting: '提交结果',
        tool_running: '工具执行中',
        tool_result: '分析结果',
        failed: '未完成',
        finished: '已完成',
      };
      return labels[reason?.activity_kind] || labels[reason?.status] || '运行中';
    },

    reasonActivityAge(reason) {
      const value = reason?.activity_updated_at || reason?.last_heartbeat_at;
      if (!value) return '尚未上报';
      const elapsed = Math.max(0, Date.now() - Date.parse(value));
      if (!Number.isFinite(elapsed)) return this.formatTime(value);
      const seconds = Math.floor(elapsed / 1000);
      if (seconds < 5) return '刚刚';
      if (seconds < 60) return `${seconds} 秒前`;
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return `${minutes} 分钟前`;
      return this.formatTime(value);
    },

    reasonRunDuration(reason) {
      const started = reason?.activity_started_at || reason?.created_at || reason?.last_heartbeat_at;
      if (!started) return '—';
      const elapsed = Math.max(0, Date.now() - Date.parse(started));
      return Number.isFinite(elapsed) ? this.formatDurationMs(elapsed) : '—';
    },

    selectedOpenIntentRecord() {
      const intent = this.selectedIntentRecord();
      if (!intent || !this.projectIsActive()) return null;
      if (!['pending', 'running'].includes(this.intentLifecycleState(intent))) return null;
      return intent;
    },

    selectedActionableOpenIntentRecord() {
      const intent = this.selectedOpenIntentRecord();
      if (!intent) return null;
      const actor = this.actorName();
      return !intent.worker || intent.worker === actor ? intent : null;
    },

    selectedReleasableOpenIntentRecord() {
      const intent = this.selectedOpenIntentRecord();
      if (!intent?.worker) return null;
      return intent.worker === this.actorName() ? intent : null;
    },

    selectedIntentPrimaryActionLabel() {
      const intent = this.selectedOpenIntentRecord();
      if (!intent) return '认领';
      if (!intent.worker) return '认领';
      return intent.worker === this.actorName() ? '心跳' : '已被认领';
    },

    intentRetryCount(intentOrId) {
      if (!intentOrId) return 0;
      const intentId = typeof intentOrId === 'string' ? intentOrId : intentOrId.id;
      if (!intentId) return 0;
      const events = Array.isArray(this.seeEvents)
        ? this.seeEvents
        : (this.projectEventsCache?.[this.selectedProjectId] || []);
      const retryOutcomes = new Set([
        'failed',
        'timed_out',
        'parse_failure',
        'hard_timeout',
        'hard_timeout_execute',
        'hard_timeout_conclude',
        'hard_timeout_both',
      ]);
      return events.filter((event) => {
        if (!event || event.phase !== 'task_outcome') return false;
        const payload = event.payload || {};
        if (payload.intent_id !== intentId) return false;
        return retryOutcomes.has(String(payload.outcome || '').toLowerCase());
      }).length;
    },

    intentExpectedCompletionAt(intent) {
      if (!intent) return null;
      if (intent.concluded_at) return intent.concluded_at;
      const createdAtMs = Date.parse(intent.created_at || '');
      if (Number.isNaN(createdAtMs)) return null;

      const retries = this.intentRetryCount(intent);
      const executeBase = Number(intent?.timeouts?.execute_seconds);
      const concludeBase = Number(intent?.timeouts?.conclude_seconds);

      let executeSeconds = Number.isFinite(executeBase) && executeBase > 0
        ? executeBase
        : Math.min(
          Number(this.dispatchSettings?.explore_execute_max_seconds ?? 3600),
          Math.max(Number(this.dispatchSettings?.explore_execute_min_seconds ?? 60), 300),
        );
      let concludeSeconds = Number.isFinite(concludeBase) && concludeBase > 0
        ? concludeBase
        : Math.min(
          Number(this.dispatchSettings?.explore_conclude_max_seconds ?? 600),
          Math.max(Number(this.dispatchSettings?.explore_conclude_min_seconds ?? 180), 180),
        );

      const isBootstrap = intent.creator === 'dispatcher.bootstrap' || intent.description === 'bootstrap';
      if (isBootstrap) {
        executeSeconds = Number(this.dispatchSettings?.bootstrap_execute_seconds ?? 300);
        concludeSeconds = Number(this.dispatchSettings?.bootstrap_conclude_seconds ?? 120);
      }

      // Align with explore retry backoff policy.
      if (!isBootstrap) {
        const executeMultiplier = Math.min(2 ** retries, 4);
        const concludeMultiplier = Math.min(2 ** retries, 3);
        executeSeconds *= executeMultiplier;
        concludeSeconds *= concludeMultiplier;
      }

      const expectedMs = createdAtMs + (executeSeconds + concludeSeconds) * 1000;
      return new Date(expectedMs).toISOString();
    },

    intentExpectedCompletionLabel(intent) {
      const eta = this.intentExpectedCompletionAt(intent);
      if (!eta) return '—';
      if (intent?.concluded_at) return `已完成（${this.formatDate(eta)}）`;
      return this.formatDate(eta);
    },

    getFactRecord(fid) {
      return this.project ? this.project.facts.find(f => f.id === fid) || null : null;
    },
  }
};
