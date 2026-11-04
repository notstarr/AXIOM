window.AxiomModules = window.AxiomModules || {};
window.AxiomModules.lineage = {
  methods: {
    // ── 四段式解析 ────────────────────────────────────────────────
    // Reason 写的任务书有两种排版：换行分隔（"目标：…\n判据：…"）和内联
    // （"目标：… 判据：… 前置：…"）。用标记词定位切分，不依赖换行，两种
    // 都能吃；标记缺失时整段作为目标展示，不丢内容。
    parseIntentDetail(description) {
      const text = String(description == null ? '' : description).trim();
      if (!text) return { 目标: '', 判据: '', 前置: '', 边界: '' };
      const marker = /(目标|判据|前置|边界)\s*[:：]/g;
      const hits = [];
      let m;
      while ((m = marker.exec(text)) !== null) {
        hits.push({ key: m[1], start: m.index, bodyStart: m.index + m[0].length });
      }
      if (!hits.length) return { 目标: text, 判据: '', 前置: '', 边界: '' };
      const out = { 目标: '', 判据: '', 前置: '', 边界: '' };
      hits.forEach((hit, i) => {
        const end = i + 1 < hits.length ? hits[i + 1].start : text.length;
        const body = text.slice(hit.bodyStart, end).trim();
        // 同一标记重复出现时拼接，避免后一次覆盖前一次
        out[hit.key] = out[hit.key] ? `${out[hit.key]} ${body}` : body;
      });
      // 首个标记之前若有导语，归入目标
      if (hits[0].start > 0) {
        const lead = text.slice(0, hits[0].start).trim();
        if (lead) out.目标 = out.目标 ? `${lead} ${out.目标}` : lead;
      }
      return out;
    },

    // ── 血缘推导（纯函数，不碰 DOM，便于单测）────────────────────
    // 规则：
    //   1. 有分链 = 至少一条 verified 且 points>0 成果的 chain
    //   2. 每条有分链的口子 = 链内 created_at 最早的 intent
    //   3. 显示意图 = 口子 ∪ 产出 verified 成果的意图
    //   4. 链内按 created_at 排序，相邻显示意图之间跳过的数量 = gapBefore
    //   5. 红卡 = verified 且 points>0 的成果，挂到 intent_id
    //   6. 跨链边 = 真实事实血缘两端都在显示集且不同链
    deriveLineage(project) {
      const intents = (project?.intents || []).filter(item => item && item.id);
      const results = project?.red_team_results || [];
      const chains = project?.chains || [];
      const chainTitle = new Map(chains.map(item => [item.id, item.title || '']));

      const scored = results.filter(item => item && item.status === 'verified' && Number(item.points_awarded) > 0);
      if (!intents.length || !scored.length) {
        return { lanes: [], edges: [], orphanResults: scored, hasData: false, totalPoints: 0, chainCount: 0, blueCount: 0, redCount: 0 };
      }

      const byIntent = new Map();
      scored.forEach(item => {
        const key = item.intent_id || '';
        if (!byIntent.has(key)) byIntent.set(key, []);
        byIntent.get(key).push(item);
      });

      const scoredIntents = [...byIntent.keys()].filter(Boolean);
      const intentById = new Map(intents.map(item => [item.id, item]));

      // 每条有分链单独一条泳道
      const chainIds = [];
      scoredIntents.forEach(id => {
        const chainId = intentById.get(id)?.chain_id;
        if (chainId && !chainIds.includes(chainId)) chainIds.push(chainId);
      });

      // 未挂到任何已知意图的成果，单独收进 orphan，避免静默丢失
      const orphanResults = scored.filter(item => !item.intent_id || !intentById.has(item.intent_id));

      const lanes = chainIds.map(chainId => {
        const chainIntents = intents
          .filter(item => item.chain_id === chainId)
          .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')) || String(a.id).localeCompare(String(b.id)));
        const opening = chainIntents[0] || null;
        const chainScored = chainIntents.filter(item => byIntent.has(item.id));
        const keep = [];
        if (opening) keep.push(opening);
        chainScored.forEach(item => {
          if (!keep.some(prev => prev.id === item.id)) keep.push(item);
        });
        keep.sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')) || String(a.id).localeCompare(String(b.id)));

        const order = new Map(chainIntents.map((item, index) => [item.id, index]));
        const nodes = keep.map((item, index) => {
          const idx = order.get(item.id) ?? 0;
          const prev = index > 0 ? order.get(keep[index - 1].id) ?? 0 : idx;
          return {
            id: item.id,
            createdAt: item.created_at || '',
            description: item.description || '',
            detail: this.parseIntentDetail(item.description),
            isOpening: !!opening && item.id === opening.id,
            gapBefore: Math.max(0, idx - prev - 1),
            results: (byIntent.get(item.id) || []).slice().sort((a, b) => Number(b.points_awarded) - Number(a.points_awarded)),
          };
        });

        const points = nodes.reduce((sum, node) => sum + node.results.reduce((acc, item) => acc + Number(item.points_awarded || 0), 0), 0);
        return {
          chainId,
          title: chainTitle.get(chainId) || chainId,
          openingId: opening?.id || '',
          nodes,
          intentsInChain: chainIntents.length,
          points,
        };
      });

      // 跨链接力：A 产出的事实被 B 消费，且两端都在显示集、不同链
      const shown = new Map();
      lanes.forEach(lane => lane.nodes.forEach(node => shown.set(node.id, lane.chainId)));
      const edges = [];
      intents.forEach(item => {
        const targetChain = shown.get(item.id);
        if (!targetChain) return;
        (item.from || []).forEach(factId => {
          if (!factId || factId === 'f001') return;
          const producer = intents.find(prev => prev.to === factId || prev.to_fact_id === factId);
          const sourceChain = producer ? shown.get(producer.id) : null;
          if (!producer || !sourceChain || sourceChain === targetChain) return;
          if (edges.some(edge => edge.from === producer.id && edge.to === item.id)) return;
          edges.push({ from: producer.id, to: item.id, fromChain: sourceChain, toChain: targetChain });
        });
      });

      lanes.sort((a, b) => b.points - a.points || String(a.chainId).localeCompare(String(b.chainId)));

      return {
        lanes,
        edges,
        orphanResults,
        hasData: lanes.length > 0,
        totalPoints: lanes.reduce((sum, lane) => sum + lane.points, 0),
        chainCount: lanes.length,
        blueCount: lanes.reduce((sum, lane) => sum + lane.nodes.length, 0),
        redCount: scored.length,
      };
    },

    // ── 结果状态色（复用现有语义）────────────────────────────────
    lineageResultTone(status) {
      return ({
        verified: 'ok', rejected: 'err', duplicate: 'off',
        reviewing: 'warn', reported: 'info', 'needs-human': 'warn',
      })[status] || 'off';
    },

    // ── 渲染 ─────────────────────────────────────────────────────
    lineageModel() {
      if (!this._lineageCache || this._lineageCacheProject !== this.project) {
        this._lineageCache = this.deriveLineage(this.project);
        this._lineageCacheProject = this.project;
      }
      return this._lineageCache;
    },

    // 版式：左侧 i001 一框，单点扇出成曲线，接进每条链的口子卡；链内卡片
    // 横向成行、箭头推进；成果卡挂在发现者正下方，用竖直虚线相接。
    // 每条链的头部是「● 链标题 · 链号 · 分数」，对应参考图的泳道标注。
    renderLineage(pass = 1) {
      const host = document.getElementById('lineage-stage');
      if (!host) return;
      // 绑定放在数据判断之前：空状态分支会提前 return，放后面就漏绑。
      // 重复调用由 __lineagePanBound 挡住。
      this.bindLineagePan();
      const model = this.lineageModel();
      if (!model.hasData) {
        host.style.height = '';
        host.style.minWidth = '';
        host.innerHTML = `<div class="lineage-empty">
          <p>本任务尚未产出核定成果</p>
          <span>红队成果通过审核后会自动出现在这里。</span>
        </div>`;
        return;
      }

      const CARD_W = 286, GAP_X = 64, LANE_GAP = 40, RES_GAP = 16, RES_STACK = 10;
      const ROOT_X = 28, ROOT_W = 132, ROOT_H = 66;
      // 起点到泳道之间留出扇出走廊：曲线在这里拐弯，不压卡片
      const FAN_W = 176;
      const LANE_X = ROOT_X + ROOT_W + FAN_W;
      // 列数按最长的那条链算，不能写死 3 列：链里留下 4 步以上的意图时，
      // 第 4 张卡会被挤出画布右侧，横向滚动条也够不着。
      const COLUMNS = Math.max(1, ...model.lanes.map(lane => lane.nodes.length));
      const LANE_W = COLUMNS * CARD_W + (COLUMNS - 1) * GAP_X;

      // 跨链接力：在目标卡内标出来源，不画穿越整个画布的长线。
      // 长线必然横穿中间泳道的卡片，反而看不清；标签式标注既准确又不遮挡。
      const crossIn = new Map();
      model.edges.forEach(edge => {
        if (!crossIn.has(edge.to)) crossIn.set(edge.to, []);
        crossIn.get(edge.to).push(edge.from);
      });

      const parts = [];
      const anchors = {};
      let cursorY = 10;

      model.lanes.forEach(lane => {
        // 头部按标题长度预留高度，宁可多留一行也不能压到下面的卡片
        const headChars = String(lane.title || '').length + 22;
        const headLines = Math.max(1, Math.ceil(headChars / 64));
        const headH = 14 + headLines * 19;
        const rowTop = cursorY + headH;

        const cards = lane.nodes.map((node, index) => ({
          node,
          index,
          x: LANE_X + index * (CARD_W + GAP_X),
          h: this.lineageCardHeight(node, CARD_W),
        }));

        let laneBottom = rowTop;
        cards.forEach(card => {
          card.results = [];
          let offset = 0;
          card.node.results.forEach(item => {
            const h = this.lineageResultHeight(item, CARD_W);
            card.results.push({ item, h, top: rowTop + card.h + RES_GAP + offset });
            offset += h + RES_STACK;
          });
          laneBottom = Math.max(laneBottom, rowTop + card.h);
          if (card.results.length) {
            const last = card.results[card.results.length - 1];
            laneBottom = Math.max(laneBottom, last.top + last.h);
          }
        });

        parts.push(`<div class="lineage-lane-head" style="left:${LANE_X}px;top:${cursorY}px;width:${LANE_W}px;">
          <i class="lineage-dot"></i>
          <span class="lineage-lane-title">${this.escapeLineage(lane.title)}</span>
          <span class="lineage-lane-id">${lane.chainId}</span>
          <span class="lineage-lane-pts">+${lane.points} 分</span>
        </div>`);

        cards.forEach(card => {
          const node = card.node;
          const goal = node.detail['目标'] || node.description || '';
          const secs = ['判据', '前置', '边界']
            .filter(key => node.detail[key])
            .map(key => `<div class="lineage-sec"><b>${key}</b><span>${this.escapeLineage(node.detail[key])}</span></div>`)
            .join('');
          const upstream = crossIn.get(node.id) || [];
          const upstreamBadge = upstream.length
            ? `<span class="lineage-card-src" title="跨链接力：事实来自 ${upstream.join('、')}">↖ ${upstream.join(' · ')}</span>`
            : '';
          parts.push(`<div class="lineage-card" data-intent="${node.id}" style="left:${card.x}px;top:${rowTop}px;width:${CARD_W}px;">
            <div class="lineage-card-top"><span class="lineage-card-id">${node.id}</span>${upstreamBadge}</div>
            <div class="lineage-goal"><b>目标</b><span>${this.escapeLineage(goal)}</span></div>
            ${secs}
          </div>`);
          anchors[node.id] = {
            left: card.x, right: card.x + CARD_W,
            top: rowTop, height: card.h, bottom: rowTop + card.h,
            mid: rowTop + card.h / 2,
            chain: lane.chainId,
          };
        });

        // 中间被省略的意图：在两张卡之间的箭头旁标一句，避免读者以为链是断的
        cards.forEach((card, index) => {
          if (!index || !card.node.gapBefore) return;
          const prev = cards[index - 1];
          // 标签压在箭头正上方（箭头 y = min(两张卡中线)），不跟卡片抢位置
          const arrowY = rowTop + Math.min(prev.h, card.h) / 2;
          parts.push(`<div class="lineage-gap" style="left:${prev.x + CARD_W + GAP_X / 2}px;top:${arrowY - 22}px;">省略 ${card.node.gapBefore} 步</div>`);
        });

        cards.forEach(card => {
          card.results.forEach(entry => {
            const item = entry.item;
            parts.push(`<div class="lineage-result" data-result="${item.id}" style="left:${card.x}px;top:${entry.top}px;width:${CARD_W}px;" title="${this.escapeLineage(item.description || '')}">
              <div class="lineage-result-head">
                <span class="lineage-result-pts">+${item.points_awarded} 分</span>
                <span class="lineage-result-id">${item.id}</span>
              </div>
              <div class="lineage-result-title">${this.escapeLineage(item.title)}</div>
              <div class="lineage-result-desc">${this.escapeLineage(item.description || '')}</div>
            </div>`);
            anchors[`res:${item.id}`] = {
              left: card.x, right: card.x + CARD_W,
              top: entry.top, height: entry.h, bottom: entry.top + entry.h,
              mid: entry.top + entry.h / 2,
              chain: lane.chainId, intent: card.node.id,
            };
          });
        });

        cursorY = laneBottom + LANE_GAP;
      });

      const totalH = Math.max(cursorY + 10, 260);
      // i001 对齐第一条链口子卡的垂直中心：第一段扇出就是一条水平线，
      // 其余各链从同一点向下散开，和示意图里「统一入口 → 各链」同构。
      // 画布很高，起点若放在几何中心会滚出首屏，读者一进来看不到入口。
      const firstAnchor = anchors[model.lanes[0]?.openingId];
      const rootTop = firstAnchor
        ? Math.max(8, Math.round(firstAnchor.mid - ROOT_H / 2))
        : 8;
      const totalW = LANE_X + LANE_W + 40;

      host.style.height = `${totalH}px`;
      host.style.minWidth = `${totalW}px`;
      // 导出 HTML 时要用画布的自然尺寸，不能拿 offsetWidth（它会被容器撑宽）
      this._lineageSize = { w: totalW, h: totalH };
      host.innerHTML = `
        <div class="lineage-root" style="left:${ROOT_X}px;top:${rootTop}px;width:${ROOT_W}px;">
          <div class="lineage-root-title">i001</div>
          <div class="lineage-root-sub">攻击链起点 · ${model.chainCount} 条链</div>
        </div>
        ${parts.join('')}
        <svg class="lineage-edges" width="100%" height="${totalH}"></svg>`;

      // 第二遍：读回浏览器实测高度，覆盖估算值后重排一次。
      // 卡片高度取决于中英文混排的换行结果，纯估算必然有偏差，宁可量一次。
      if (pass === 1) {
        const measured = {};
        host.querySelectorAll('.lineage-result').forEach(el => {
          const id = el.getAttribute('data-result');
          if (id) measured[id] = el.offsetHeight;
        });
        host.querySelectorAll('.lineage-card').forEach(el => {
          const id = el.getAttribute('data-intent');
          if (id) measured[`intent:${id}`] = el.offsetHeight;
        });
        const keys = Object.keys(measured);
        const changed = !this._lineageRealH || keys.some(key => this._lineageRealH[key] !== measured[key]);
        if (changed) {
          this._lineageRealH = measured;
          setTimeout(() => this.renderLineage(2), 0);
          return;
        }
      }

      this.drawLineageEdges(host, model, anchors, {
        rootRight: ROOT_X + ROOT_W,
        rootMidY: rootTop + ROOT_H / 2,
        fanW: FAN_W,
        laneX: LANE_X,
        cardW: CARD_W,
        gapX: GAP_X,
      });

      this.bindLineageResize();
      // 首帧默认「适应宽度」：画布动辄几千像素高、上千像素宽，
      // 1:1 打开永远只能看到左上角一小块。之后由用户自己的选择说了算。
      // 适应类的档位每次渲染都重算，否则列数一变、窗口一改就对不上了。
      if (this.lineageFit === 'all') this.fitLineageAll();
      else if (this.lineageFit === 'width' || !this.lineageScale) this.fitLineageWidth();
      else this.applyLineageZoom();
    },

    // ── 缩放 ─────────────────────────────────────────────────────
    // 用 transform: scale 而不是 zoom：transform 不改布局尺寸，卡片实测高度
    // 仍然是自然像素，两遍测量的结果不会被缩放污染。
    // #lineage-zoom 只是一层撑尺寸的壳，撑成「画布尺寸 × 缩放」，滚动条才对。
    applyLineageZoom() {
      const wrap = document.getElementById('lineage-zoom');
      const host = document.getElementById('lineage-stage');
      if (!wrap || !host) return;
      const scale = this.lineageScale || 1;
      const w = host.offsetWidth || 0;
      const h = host.offsetHeight || 0;
      if (!w || !h) return;
      host.style.transformOrigin = '0 0';
      host.style.transform = scale === 1 ? '' : `scale(${scale})`;
      const scaledW = Math.round(w * scale);
      wrap.style.width = `${scaledW}px`;
      wrap.style.height = `${Math.round(h * scale)}px`;
      // 缩到比可视区窄时居中，否则整张图贴着左边缘，右边一大片空白
      const availW = this.lineageAvail().w;
      wrap.style.marginLeft = scaledW + 24 < availW ? `${Math.round((availW - scaledW) / 2)}px` : '0';
    },

    setLineageScale(scale, fit) {
      // 下限放到 3%：链多的任务画布能到 9000px 高，「全览」要真的全览
      this.lineageScale = Math.min(1.6, Math.max(0.03, Number(scale) || 1));
      this.lineageFit = fit || 'manual';
      this.applyLineageZoom();
    },

    lineageAvail() {
      const scroll = document.querySelector('.lineage-scroll');
      return {
        w: Math.max(200, (scroll?.clientWidth || 1200) - 24),
        h: Math.max(200, (scroll?.clientHeight || 600) - 24),
      };
    },

    fitLineageWidth() {
      const host = document.getElementById('lineage-stage');
      if (!host) return;
      const avail = this.lineageAvail();
      const w = host.offsetWidth || 0;
      if (!w) return;
      this.setLineageScale(Math.min(1, avail.w / w), 'width');
    },

    fitLineageAll() {
      const host = document.getElementById('lineage-stage');
      if (!host) return;
      const avail = this.lineageAvail();
      const w = host.offsetWidth || 0;
      const h = host.offsetHeight || 0;
      if (!w || !h) return;
      this.setLineageScale(Math.min(1, avail.w / w, avail.h / h), 'all');
    },

    zoomLineage(step) {
      const base = this.lineageScale || 1;
      this.setLineageScale(base + step, 'manual');
    },

    // 「适应宽度」要保持有效：窗口一改宽度，上次算出来的比例就错了。
    bindLineageResize() {
      if (this._lineageResizeBound) return;
      this._lineageResizeBound = true;
      this._lineageResizeHandler = () => {
        if (this.humanTab !== 'lineage') return;
        clearTimeout(this._lineageResizeTimer);
        this._lineageResizeTimer = setTimeout(() => {
          if (this.humanTab !== 'lineage') return;
          if (this.lineageFit === 'width') this.fitLineageWidth();
          else if (this.lineageFit === 'all') this.fitLineageAll();
        }, 160);
      };
      window.addEventListener('resize', this._lineageResizeHandler);
    },

    // ── 拖拽平移 ─────────────────────────────────────────────────
    // 画布动辄几千像素高，靠滚动条找某一条链很累，所以按住就能拖着走。
    // 但卡片里的判据/边界是要复制的中文长句，左键一拖就平移会让人选不住字，
    // 所以按住 Alt（或 Ctrl/⌘）时完全不接管，把拖动交还给浏览器的文本选择。
    bindLineagePan() {
      const vp = document.querySelector('.lineage-scroll');
      if (!vp || vp.__lineagePanBound) return;
      vp.__lineagePanBound = true;

      vp.addEventListener('pointerdown', ev => {
        if (ev.button !== 0 && ev.button !== 1) return;
        if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
        if (ev.target.closest('button, a, input, select, textarea, [contenteditable="true"]')) return;
        // 落点在滚动条上（offsetX/Y 超出 clientWidth/Height 就是滚动条区域）不平移，
        // 否则下面的 preventDefault 会把拖滚动条一起挡掉。
        if (ev.offsetX > vp.clientWidth || ev.offsetY > vp.clientHeight) return;
        // 选区是在 pointerdown/mousedown 那一刻起头的，等越过阈值再补 user-select:none
        // 已经来不及了——选区照样拖出来。只能在源头挡掉。
        ev.preventDefault();
        const sel = window.getSelection && window.getSelection();
        if (sel && sel.rangeCount) sel.removeAllRanges();
        vp.__lineagePanMoved = false;
        const start = { x: ev.clientX, y: ev.clientY, l: vp.scrollLeft, t: vp.scrollTop };
        let moved = false;
        try { vp.setPointerCapture(ev.pointerId); } catch (err) { /* 老浏览器没有捕获，不影响平移 */ }
        const move = e => {
          const dx = e.clientX - start.x;
          const dy = e.clientY - start.y;
          if (!moved && Math.abs(dx) + Math.abs(dy) < 4) return;
          if (!moved) { moved = true; vp.classList.add('is-panning'); }
          vp.scrollLeft = start.l - dx;
          vp.scrollTop = start.t - dy;
        };
        const end = () => {
          vp.classList.remove('is-panning');
          vp.removeEventListener('pointermove', move);
          vp.removeEventListener('pointerup', end);
          vp.removeEventListener('pointercancel', end);
          // 松手会补一个 click，吞掉它，否则拖完像误点了卡片
          if (moved) vp.__lineagePanMoved = true;
        };
        vp.addEventListener('pointermove', move);
        vp.addEventListener('pointerup', end);
        vp.addEventListener('pointercancel', end);
      });

      vp.addEventListener('click', ev => {
        if (!vp.__lineagePanMoved) return;
        vp.__lineagePanMoved = false;
        ev.preventDefault();
        ev.stopPropagation();
      }, true);
    },

    lineageScalePercent() {
      return Math.round((this.lineageScale || 1) * 100);
    },

    // ── 导出独立 HTML ────────────────────────────────────────────
    // 目的：把血缘全图从系统里拿出去（发给人、存档、离线看）。
    // 做法：不重画，直接把实时画布的 innerHTML 搬过去——卡片是绝对定位 + 内联
    // left/top，SVG 连线也在里面，天然自包含；样式从 document.styleSheets 现取，
    // 只筛 .lineage-* / .lg-* 两组规则，避免把整个面板的 CSS 塞进导出文件。
    lineageExportCss() {
      const keep = [];
      const isLineage = text => /(^|[\s,>+~(])(\.lineage[\w-]*|\.lg-[\w-]*)/.test(text);
      for (const sheet of Array.from(document.styleSheets)) {
        let rules = null;
        try { rules = sheet.cssRules; } catch (err) { continue; }  // 跨域样式表读不到，跳过
        if (!rules) continue;
        for (const rule of Array.from(rules)) {
          if (!rule.selectorText || !rule.style) continue;
          if (!isLineage(rule.selectorText)) continue;
          // 暗色规则在导出文件里不生效（导出统一用亮色），别再带出去
          if (rule.selectorText.includes('.dark')) continue;
          keep.push(rule.cssText);
        }
      }
      return keep.join('\n');
    },

    // 主题变量在导出文件里没有 :root 定义，从当前页面读实际值抄过去
    lineageExportVars() {
      const cs = getComputedStyle(document.documentElement);
      return ['--sto-panel', '--sto-inset', '--sto-line', '--sto-fg', '--sto-fg-2', '--sto-fg-3']
        .map(name => {
          const value = cs.getPropertyValue(name).trim();
          return value ? `${name}:${value};` : '';
        })
        .join('');
    },

    // previewOnly=true 时只返回 HTML 字符串不下载（自测和浏览器验证走这条）
    exportLineageHtml(previewOnly) {
      const host = document.getElementById('lineage-stage');
      if (!host || !host.innerHTML.trim()) return '';
      const model = this.lineageModel();
      const size = this._lineageSize || { w: host.offsetWidth, h: host.offsetHeight };
      const task = this.project?.project?.id || this.project?.task?.id || 'task';
      // 文件名用本地时间：toISOString 是 UTC，导出来会和界面上的时间差 8 小时
      const now = new Date();
      const pad = value => String(value).padStart(2, '0');
      const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
        + `-${pad(now.getHours())}${pad(now.getMinutes())}`;
      const title = `${task} 红队攻击血缘全图`;
      const subtitle = `${model.chainCount} 条打通链 · ${model.blueCount} 个意图 · ${model.redCount} 项成果 · ${model.totalPoints} 分`;
      const lanes = model.lanes
        .map(lane => `<li><span class="xl-id">${this.escapeLineage(lane.chainId)}</span>${this.escapeLineage(lane.title)}<span class="xl-pts">+${lane.points} 分</span></li>`)
        .join('');

      const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${this.escapeLineage(title)}</title>
<style>
:root { ${this.lineageExportVars()} }
* { box-sizing: border-box; }
body {
  margin: 0; background: #f1f5f9; color: #0f172a;
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", sans-serif;
  font-size: 13px; line-height: 1.5;
}
.xbar {
  position: sticky; top: 0; z-index: 20; display: flex; align-items: center; gap: 14px;
  flex-wrap: wrap; padding: 10px 18px; background: rgba(255,255,255,.94);
  border-bottom: 1px solid #e2e8f0; backdrop-filter: blur(8px);
}
.xbar h1 { margin: 0; font-size: 14px; font-weight: 700; white-space: nowrap; }
.xbar .xsub { font-size: 11px; color: #64748b; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.xbar .xspacer { flex: 1 1 auto; min-width: 12px; }
.xbar button {
  height: 26px; min-width: 26px; padding: 0 9px; cursor: pointer;
  border: 1px solid #cbd5e1; border-radius: 7px; background: #fff;
  color: #334155; font-size: 11.5px; font-weight: 600; font-family: inherit;
}
.xbar button:hover { border-color: #94a3b8; background: #f8fafc; }
.xpct { min-width: 48px; font-variant-numeric: tabular-nums; }
/* scrollbar-gutter: stable 让 clientWidth 恒定——否则纵向滚动条一出现，
   可用宽度就少 15px，「适应宽度」刚算好的比例立刻横向溢出。 */
.xvp { position: absolute; inset: 0; top: 0; padding: 16px; overflow: auto; scrollbar-gutter: stable; }
.xwrap { position: relative; margin: 0 auto; }
.xstage {
  position: absolute; top: 0; left: 0; transform-origin: 0 0;
  background: #fff; border-radius: 8px;
}
.xdock {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 25;
  max-height: 40vh; overflow: auto; padding: 10px 18px 14px;
  background: rgba(255,255,255,.96); border-top: 1px solid #e2e8f0;
  font-size: 11.5px; display: none;
}
.xdock.is-on { display: block; }
.xdock h2 { margin: 0 0 6px; font-size: 11px; font-weight: 700; color: #64748b; }
.xdock ol { margin: 0; padding: 0; list-style: none; }
.xdock li { display: flex; gap: 8px; align-items: baseline; padding: 3px 0; border-top: 1px dashed #eef2f7; }
.xdock li:first-child { border-top: 0; }
.xl-id { font-family: ui-monospace, Menlo, monospace; font-size: 10px; font-weight: 700; color: #94a3b8; flex: none; width: 46px; }
.xl-pts { margin-left: auto; flex: none; font-weight: 800; color: #e0453f; font-variant-numeric: tabular-nums; }
@media print {
  .xbar, .xdock { display: none !important; }
  body { background: #fff; }
  .xvp { position: static; overflow: visible; padding: 0; }
}
${this.lineageExportCss()}
</style>
</head>
<body>
<header class="xbar">
  <h1>${this.escapeLineage(title)}</h1>
  <span class="xsub">${this.escapeLineage(subtitle)}</span>
  <span class="xspacer"></span>
  <button id="xminus" title="缩小">−</button>
  <button id="xpct" class="xpct" title="恢复 100%">100%</button>
  <button id="xplus" title="放大">+</button>
  <button id="xfitw" title="按宽度铺满">适应宽度</button>
  <button id="xfitall" title="整图一屏">全览</button>
  <button id="xlanes" title="列出全部打通链">链条清单</button>
</header>
<div id="xvp" class="xvp">
  <div id="xwrap" class="xwrap">
    <div id="lineage-stage" class="xstage" style="width:${size.w}px;height:${size.h}px;">
${host.innerHTML}
    </div>
  </div>
</div>
<aside id="xdock" class="xdock">
  <h2>打通的攻击链（按核定分排序）</h2>
  <ol>${lanes}</ol>
</aside>
<script>
(function () {
  var W = ${Math.round(size.w)}, H = ${Math.round(size.h)};
  var stage = document.getElementById('lineage-stage');
  var wrap = document.getElementById('xwrap');
  var vp = document.getElementById('xvp');
  var pct = document.getElementById('xpct');
  var scale = 1;

  function apply() {
    stage.style.transform = scale === 1 ? '' : 'scale(' + scale + ')';
    wrap.style.width = Math.round(W * scale) + 'px';
    wrap.style.height = Math.round(H * scale) + 'px';
    pct.textContent = Math.round(scale * 100) + '%';
  }
  // 纵向滚动条一出现，vp.clientWidth 会少 15~17px；而第一次量的时候它还没出现。
  // 所以量完应用一次，再按新的 clientWidth 校一次，直到不再横向溢出。
  function settle(target) {
    scale = target(vp.clientWidth - 32);
    apply();
    for (var i = 0; i < 2; i++) {
      if (vp.scrollWidth <= vp.clientWidth + 1) break;
      scale = Math.max(0.03, scale * ((vp.clientWidth - 32) / (vp.scrollWidth - 32)));
      apply();
    }
  }
  function fitWidth() {
    settle(function (avail) { return Math.min(1, avail / W); });
  }
  function fitAll() {
    settle(function (avail) { return Math.min(1, avail / W, (vp.clientHeight - 32) / H); });
  }

  document.getElementById('xminus').onclick = function () { scale = Math.max(0.03, scale - 0.1); apply(); };
  document.getElementById('xplus').onclick = function () { scale = Math.min(2, scale + 0.1); apply(); };
  pct.onclick = function () { scale = 1; apply(); };
  document.getElementById('xfitw').onclick = fitWidth;
  document.getElementById('xfitall').onclick = fitAll;
  document.getElementById('xlanes').onclick = function () {
    document.getElementById('xdock').classList.toggle('is-on');
  };

  var fitMode = 'width';
  window.addEventListener('resize', function () {
    if (fitMode === 'width') fitWidth(); else if (fitMode === 'all') fitAll(); else apply();
  });
  document.getElementById('xfitw').addEventListener('click', function () { fitMode = 'width'; });
  document.getElementById('xfitall').addEventListener('click', function () { fitMode = 'all'; });

  fitWidth();
})();
</script>
</body>
</html>`;

      if (previewOnly) return html;
      const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `lineage-${task}-${stamp}.html`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      return html;
    },

    // 文字折行行数：中文按 1 个字宽，英文/数字按 0.55 字宽，再折成行数。
    // 只用于首帧估算，第二帧一律用浏览器实测高度。
    lineageTextLines(text, fontSize, innerWidth) {
      const value = String(text || '');
      if (!value) return 0;
      let px = 0;
      for (const ch of value) {
        px += /[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]/.test(ch) ? fontSize : fontSize * 0.55;
      }
      return Math.max(1, Math.ceil(px / innerWidth));
    },

    lineageCardHeight(node, cardW) {
      const measured = this._lineageRealH?.[`intent:${node.id}`];
      if (measured) return measured;
      const inner = cardW - 30;
      const goal = String(node.detail['目标'] || '');
      const goalLines = this.lineageTextLines(`目标${goal}`, 12, inner);
      let secLines = 0;
      let secCount = 0;
      ['判据', '前置', '边界'].forEach(key => {
        if (!node.detail[key]) return;
        secCount += 1;
        secLines += this.lineageTextLines(`${key}${node.detail[key]}`, 10.5, inner);
      });
      return 22 + goalLines * 19 + secLines * 17 + secCount * 6 + 12;
    },

    lineageResultHeight(item, cardW) {
      const measured = this._lineageRealH?.[item.id];
      if (measured) return measured;
      const inner = cardW - 30;
      const titleLines = this.lineageTextLines(item.title, 12.5, inner);
      const descLines = this.lineageTextLines(item.description, 10.5, inner);
      return 24 + titleLines * 19 + descLines * 17 + 18;
    },

    drawLineageEdges(host, model, anchors, geo) {
      const svg = host.querySelector('.lineage-edges');
      if (!svg) return;
      const NS = 'http://www.w3.org/2000/svg';
      const add = (d, color, width, dash) => {
        const path = document.createElementNS(NS, 'path');
        path.setAttribute('d', d);
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', color);
        path.setAttribute('stroke-width', width);
        if (dash) path.setAttribute('stroke-dasharray', dash);
        svg.appendChild(path);
      };
      // 实心三角箭头，和参考图一致
      const head = (x, y, color) => {
        const tri = document.createElementNS(NS, 'path');
        tri.setAttribute('d', `M ${x - 8} ${y - 4.5} L ${x} ${y} L ${x - 8} ${y + 4.5} Z`);
        tri.setAttribute('fill', color);
        svg.appendChild(tri);
      };

      // 1) i001 → 每条链的口子。
      //    统一从起点右侧出发，进入扇出走廊后各自竖直下落，最后用圆角拐进卡片。
      //    曲线数等于链数时，如果都用同一条贝塞尔，纵向跨几千像素会挤成一束；
      //    改成「每根电缆占走廊里一个 x 位」，看上去就是一束分好的线。
      const LANE_R = 9;
      const cableX = (index, total) => {
        const room = geo.fanW - 30;           // 走廊内可用宽度
        const step = total > 1 ? Math.min(22, room / (total - 1)) : 0;
        return geo.laneX - 24 - index * step; // 从近端向起点方向依次排列
      };
      model.lanes.forEach((lane, index) => {
        const anchor = anchors[lane.openingId];
        if (!anchor) return;
        const x1 = geo.rootRight, y1 = geo.rootMidY;
        const x2 = anchor.left - 4, y2 = anchor.mid;
        const cx = cableX(index, model.lanes.length);
        // 起点与目标几乎同一水平线：直接拉一条横线，不要多余的拐弯
        if (Math.abs(y2 - y1) < LANE_R * 2 + 6) {
          add(`M ${x1} ${y1} H ${x2 - 8}`, '#94a3b8', 1.5, null);
          head(x2 - 8, y2, '#94a3b8');
          return;
        }
        const down = y2 > y1;
        const r = LANE_R;
        const d = [
          `M ${x1} ${y1}`,
          `H ${cx - r}`,
          `Q ${cx} ${y1} ${cx} ${y1 + (down ? r : -r)}`,
          `V ${y2 - (down ? r : -r)}`,
          `Q ${cx} ${y2} ${cx + r} ${y2}`,
          `H ${x2 - 8}`,
        ].join(' ');
        add(d, '#94a3b8', 1.5, null);
        head(x2 - 8, y2, '#94a3b8');
      });

      // 2) 链内推进：卡片右缘 → 下一张卡左缘，省略过意图的一段用虚线
      model.lanes.forEach(lane => {
        lane.nodes.forEach((node, index) => {
          if (!index) return;
          const prev = anchors[lane.nodes[index - 1].id];
          const cur = anchors[node.id];
          if (!prev || !cur) return;
          const y = Math.min(prev.mid, cur.mid);
          const x1 = prev.right + 2, x2 = cur.left - 3;
          if (x2 <= x1) return;
          const dashed = node.gapBefore > 0;
          const color = dashed ? '#cbd5e1' : '#2563eb';
          add(`M ${x1} ${y} H ${x2}`, color, 1.8, dashed ? '5 5' : null);
          head(x2 + 1, y, color);
        });
      });

      // 3) 意图 → 成果：从卡片底部中心垂直虚线下挂，一张接一张
      const byIntent = new Map();
      Object.entries(anchors).forEach(([key, anchor]) => {
        if (!key.startsWith('res:')) return;
        if (!anchors[anchor.intent]) return;
        if (!byIntent.has(anchor.intent)) byIntent.set(anchor.intent, []);
        byIntent.get(anchor.intent).push(anchor);
      });
      byIntent.forEach((list, intentId) => {
        const parent = anchors[intentId];
        const cx = parent.left + geo.cardW / 2;
        const sorted = list.slice().sort((a, b) => a.top - b.top);
        let from = parent.bottom;
        sorted.forEach(item => {
          add(`M ${cx} ${from + 2} V ${item.top - 3}`, '#fca5a5', 1.4, '3 4');
          from = item.bottom;
        });
      });
    },

    escapeLineage(value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    },

    lineageSummaryLabel() {
      const model = this.lineageModel();
      if (!model.hasData) return '暂无成果';
      return `${model.chainCount} 条打通 · ${model.redCount} 项成果 · ${model.totalPoints} 分`;
    },
  },
};
