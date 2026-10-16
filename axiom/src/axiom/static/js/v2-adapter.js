/* Backend V2 adapter for the established AXIOM frontend.
 *
 * The UI deliberately keeps the original templates, layout and interaction
 * model.  This file is the only compatibility layer: it translates the old
 * frontend vocabulary (project group / project) to Backend V2's explicit
 * Project / Task resources.
 */
(function () {
  const M = window.AxiomModules;
  if (!M) return;

  const apiRoot = '/api/v1';
  const knowledgeCounts = new Map();
  const piThinkingLevels = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
  const runtimeConfigKeys = [
    'runner_interval_seconds',
    'global_max_active_runs',
    'global_max_active_tasks',
    'per_task_max_active_runs',
    'per_task_explore_concurrency',
    'per_task_review_concurrency',
    'heartbeat_interval_seconds',
    'lease_timeout_seconds',
    'execution_retry_limit',
    'infrastructure_retry_limit',
    'retry_base_seconds',
    'retry_max_seconds',
    'worker_failure_cooldown_seconds',
    'bootstrap_execute_seconds',
    'bootstrap_conclude_seconds',
    'reason_execute_seconds',
    'reason_conclude_seconds',
    'plan_execute_seconds',
    'plan_conclude_seconds',
    'review_execute_seconds',
    'review_conclude_seconds',
    'reason_batch_size',
    'reason_low_watermark',
    'reason_max_wait_seconds',
    'reason_min_interval_seconds',
    'reason_queue_limit',
    'plan_queue_limit',
    'plan_max_consecutive_runs',
    'sandbox_backend',
    'sandbox_required',
    'sandbox_local_fallback',
    'sandbox_template',
    'sandbox_runtime_revision',
    'sandbox_healthcheck_timeout',
    'browser_gateway_enabled',
    'browser_gateway_bind',
    'browser_gateway_base_url',
    'browser_file_bridge_limit',
    'explore_execute_min_seconds',
    'explore_execute_max_seconds',
    'explore_conclude_min_seconds',
    'explore_conclude_max_seconds',
    'explore_execute_default_seconds',
    'explore_conclude_default_seconds',
  ];
  const splitLines = value => String(value || '').split(/[\n,]+/).map(item => item.trim()).filter(Boolean);
  const taskStatus = status => ({ running: 'active', stopped: 'stopped', completed: 'completed', failed: 'failed', draft: 'stopped' }[status] || status);
  const v2Status = status => ({ active: 'running', stopped: 'stopped' }[status] || status);
  const originText = task => {
    const origin = task?.origin || {};
    if (task?.legacy_readonly && origin.host === 'legacy.invalid') return 'legacy origin unavailable';
    if (origin.original_input) return origin.original_input;
    const host = String(origin.host || '');
    const wrapped = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
    return `${origin.scheme ? `${origin.scheme}://` : ''}${wrapped}${origin.port ? `:${origin.port}` : ''}`;
  };

  function onUnauthorized() {
    // 凭据过期/失效：清除本地 token 并通知应用切回登录态
    try { localStorage.removeItem('axiom_token'); } catch (_) {}
    try { window.dispatchEvent(new CustomEvent('axiom:unauthorized')); } catch (_) {}
  }

  async function request(method, path, body, options = {}) {
    const headers = { ...(options.headers || {}) };
    const init = { method, headers, signal: options.signal };
    if (body instanceof FormData) {
      init.body = body;
    } else if (body !== undefined && body !== null) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const response = await fetch(`${apiRoot}${path}`, init);
    if (response.status === 204) return null;
    const text = await response.text();
    let data = text;
    try { data = text ? JSON.parse(text) : null; } catch (_) {}
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('/auth/')) onUnauthorized();
      const detail = data && typeof data === 'object'
        ? (data.detail || data.error?.message || data.message)
        : text;
      const message = Array.isArray(detail)
        ? detail.map(item => item.msg || JSON.stringify(item)).join('; ')
        : (detail || `HTTP ${response.status}`);
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function requestPage(path, options = {}) {
    const response = await fetch(`${apiRoot}${path}`, { signal: options.signal });
    const text = await response.text();
    let items = [];
    try { items = text ? JSON.parse(text) : []; } catch (_) {}
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('/auth/')) onUnauthorized();
      throw new Error(items?.detail || text || `HTTP ${response.status}`);
    }
    return {
      items: Array.isArray(items) ? items : [],
      total: Number(response.headers.get('X-Total-Count') || items.length || 0),
    };
  }

  function reasonActivitySummary(activityKind, status, summary = '') {
    if (summary) return summary;
    return {
      pending: '等待可用的 Reason Worker 领取',
      claimed: '正在启动 Reason 环境',
      preparing_context: '正在准备 Reason Context 与能力快照',
      analyzing: '正在分析任务上下文并规划下一批 Intent',
    }[activityKind || status] || '正在分析任务上下文并规划下一批 Intent';
  }

  function planActivitySummary(activityKind, status, summary = '') {
    if (summary) return summary;
    return {
      pending: '等待可用的 Plan Worker 领取',
      claimed: '正在启动 Plan 环境',
      preparing_context: '正在准备 Plan 全景 Context 与能力快照',
      analyzing: '正在复核全量历史图谱与攻击面覆盖',
      concluding: '正在整理 Plan Workspace 并提交全景决策',
    }[activityKind || status] || '正在进行全景复盘';
  }

  function mapTask(task) {
    // A legacy Plan was persisted as reason_runs(trigger="plan") and older
    // APIs exposed that row through both active_plan_run and the compatibility
    // reason_worker fields.  Once the task is in the Plan phase, the Plan card
    // is the sole planning activity shown to users; never let that legacy
    // projection resurrect a duplicate Reason card.  A real ReasonRun must
    // always carry a non-Plan trigger.
    const rawActiveReasonRun = task.active_reason_run || null;
    const activeReasonRun = rawActiveReasonRun && rawActiveReasonRun.trigger !== 'plan'
      ? rawActiveReasonRun
      : null;
    const activePlanRun = task.active_plan_run || null;
    const allQueuedPlans = (task.queued_plan_runs || []).map(run => ({
      id: run.id,
      status: run.status,
      worker: run.worker || '排队中',
      trigger: run.trigger || 'reconcile',
      trigger_payload: run.trigger_payload || {},
      settings_version: run.settings_version,
      execute_timeout_seconds: run.execute_timeout_seconds,
      conclude_timeout_seconds: run.conclude_timeout_seconds,
      created_at: run.created_at,
    }));
    const planPhase = String(task.phase || '').toLowerCase() === 'plan';
    const planContextPresent = Boolean(activePlanRun || allQueuedPlans.length || planPhase);
    const legacyReason = !activeReasonRun && !planContextPresent && task.reason_worker ? {
      id: '',
      status: 'running',
      worker: task.reason_worker,
      trigger: 'legacy',
      last_heartbeat_at: task.reason_heartbeat_at,
      activity_kind: task.reason_activity_kind || 'analyzing',
      activity_summary: reasonActivitySummary(
        task.reason_activity_kind,
        'running',
        task.reason_activity_summary,
      ),
      activity_tool: task.reason_activity_tool || '',
      activity_started_at: task.reason_activity_started_at || task.reason_heartbeat_at,
      activity_updated_at: task.reason_activity_updated_at || task.reason_heartbeat_at,
      created_at: task.reason_activity_started_at || task.reason_heartbeat_at,
    } : null;
    const allQueuedReasons = (task.queued_reason_runs || []).map(qr => ({
      id: qr.id,
      status: qr.status,
      worker: qr.worker || '排队中',
      trigger: qr.trigger || 'scheduler',
      trigger_payload: qr.trigger_payload || {},
      created_at: qr.created_at,
    }));
    const isQueuedReasonPrimary = !activeReasonRun && !legacyReason && allQueuedReasons.length > 0;
    const queuedReasonDisplay = isQueuedReasonPrimary ? {
      id: allQueuedReasons[0].id,
      status: 'pending',
      worker: '排队中',
      trigger: allQueuedReasons[0].trigger,
      trigger_payload: allQueuedReasons[0].trigger_payload,
      activity_kind: 'pending',
      activity_summary: '等待前序 Reason 完成后按 FIFO 领取',
      activity_tool: '',
      created_at: allQueuedReasons[0].created_at,
    } : null;
    const isQueuedPlanPrimary = !activePlanRun && allQueuedPlans.length > 0;
    const queuedPlanDisplay = isQueuedPlanPrimary ? {
      ...allQueuedPlans[0],
      status: 'pending',
      worker: '等待 Plan Worker',
      activity_kind: 'pending',
      activity_summary: '等待可用的 Plan Worker 按 FIFO 领取',
      activity_tool: '',
    } : null;
    // 当主卡片展示的是排队中的第一项时，下方的叠放排队列表应当从第 2 项开始展示，避免重影自等
    const queuedReasons = isQueuedReasonPrimary ? allQueuedReasons.slice(1) : allQueuedReasons;
    const queuedPlans = isQueuedPlanPrimary ? allQueuedPlans.slice(1) : allQueuedPlans;
    return {
      id: task.id,
      parent_project_id: task.project_id,
      title: task.title,
      origin: originText(task),
      goal: task.goal,
      engagement_mode: task.engagement_mode || 'vulnerability',
      red_team_objective: task.red_team_objective || null,
      current_goal_cycle_id: task.current_goal_cycle_id || '',
      status: taskStatus(task.status),
      phase: task.phase,
      bootstrap_enabled: true,
      bootstrap_status: task.phase === 'bootstrap' ? 'running' : (task.status === 'completed' ? 'completed' : ''),
      guard_enabled: !!task.guard_enabled,
      worker_pool: task.worker_pool || [],
      task_scope: task.scope || { enabled: false, targets: [], exclude: [], notes: '' },
      legacy_readonly: !!task.legacy_readonly,
      archived: !!task.archived,
      // ReasonRun is an execution activity, not an Intent/graph node.  Keep a
      // narrow legacy fallback for historical payloads only.
      reason: activeReasonRun ? {
        id: activeReasonRun.id,
        status: activeReasonRun.status,
        worker: activeReasonRun.worker || '等待 Reason Worker',
        trigger: activeReasonRun.trigger || 'scheduler',
        trigger_payload: activeReasonRun.trigger_payload || {},
        lease_id: activeReasonRun.lease_id || '',
        settings_version: activeReasonRun.settings_version,
        execute_timeout_seconds: activeReasonRun.execute_timeout_seconds,
        conclude_timeout_seconds: activeReasonRun.conclude_timeout_seconds,
        last_heartbeat_at: activeReasonRun.last_heartbeat_at,
        activity_kind: activeReasonRun.activity_kind || activeReasonRun.status || 'pending',
        activity_summary: reasonActivitySummary(
          activeReasonRun.activity_kind,
          activeReasonRun.status,
          activeReasonRun.activity_summary,
        ),
        activity_tool: activeReasonRun.activity_tool || '',
        activity_started_at: activeReasonRun.activity_started_at || activeReasonRun.created_at,
        activity_updated_at: activeReasonRun.activity_updated_at || activeReasonRun.last_heartbeat_at || activeReasonRun.created_at,
        created_at: activeReasonRun.created_at,
        is_plan: false,
      } : legacyReason || queuedReasonDisplay,
      plan: activePlanRun ? {
        id: activePlanRun.id,
        status: activePlanRun.status,
        worker: activePlanRun.worker || '等待 Plan Worker',
        trigger: activePlanRun.trigger || 'reconcile',
        trigger_payload: activePlanRun.trigger_payload || {},
        lease_id: activePlanRun.lease_id || '',
        settings_version: activePlanRun.settings_version,
        execute_timeout_seconds: activePlanRun.execute_timeout_seconds,
        conclude_timeout_seconds: activePlanRun.conclude_timeout_seconds,
        last_heartbeat_at: activePlanRun.last_heartbeat_at,
        activity_kind: activePlanRun.activity_kind || activePlanRun.status || 'pending',
        activity_summary: planActivitySummary(
          activePlanRun.activity_kind,
          activePlanRun.status,
          activePlanRun.activity_summary,
        ),
        activity_tool: activePlanRun.activity_tool || '',
        activity_started_at: activePlanRun.activity_started_at || activePlanRun.created_at,
        activity_updated_at: activePlanRun.activity_updated_at || activePlanRun.last_heartbeat_at || activePlanRun.created_at,
        created_at: activePlanRun.created_at,
      } : (queuedPlanDisplay || (task.phase === 'plan' ? {
        id: '',
        status: 'pending',
        worker: '等待 Plan Worker',
        trigger: 'reconcile',
        trigger_payload: {},
        lease_id: '',
        settings_version: 0,
        execute_timeout_seconds: 0,
        conclude_timeout_seconds: 0,
        last_heartbeat_at: null,
        activity_kind: 'pending',
        activity_summary: 'Plan 队列等待可用后端',
        activity_tool: '',
        created_at: task.updated_at || task.created_at,
      } : null)),
      active_plan_run: activePlanRun,
      plan_run_count: Number(task.plan_run_count || 0),
      last_plan_decision: task.last_plan_decision || null,
      queued_reasons: queuedReasons,
      queued_plans: queuedPlans,
      // The first pending item may be promoted into the primary Plan/Reason
      // card when there is no active run. Preserve the true pending totals so
      // badges and status copy do not under-count the queue.
      queued_reason_count: allQueuedReasons.length,
      queued_plan_count: allQueuedPlans.length,
      created_at: task.created_at,
      updated_at: task.updated_at,
      completed_at: task.completed_at,
      run_duration_seconds: task.run_duration_seconds ?? null,
      run_started_at: task.run_started_at || null,
      run_deadline_at: task.run_deadline_at || null,
      last_pause_reason: task.last_pause_reason || null,
      fact_count: task.fact_count || 0,
      intent_count: task.intent_count || 0,
      working_intent_count: task.working_intent_count || 0,
      hint_count: task.hint_count || 0,
      vulnerability_count: task.vulnerability_count || 0,
      artifact_count: task.artifact_count || 0,
      todo_count: task.todo_count || 0,
      depth_mode: false,
    };
  }

  function mapVulnerability(item, task, project) {
    const parentProjectId = task?.parent_project_id || task?.project_id || project?.id || '';
    return {
      ...item,
      project_id: item.task_id,
      parent_project_id: parentProjectId,
      task_title: task?.title || item.task_id,
      project_group_title: project?.title || '',
      recommendation: item.remediation || '',
      affected_scope: item.affected_target || '',
      finding_artifact: item.finding_artifact ? mapArtifact(item.finding_artifact) : null,
      evidence_artifacts: (item.evidence_artifacts || []).map(mapArtifact),
    };
  }

  function mapVulnerabilityReviewRun(item) {
    return {
      ...item,
      artifacts: (item.artifacts || []).map(mapArtifact),
    };
  }

  function mapArtifact(item) {
    const storageKey = item.storage_key || item.path || '';
    const artifactMarker = '/artifacts/';
    const uploadMarker = '/uploads/';
    let displayPath = storageKey;
    if (storageKey.includes(artifactMarker)) displayPath = storageKey.split(artifactMarker).slice(1).join(artifactMarker);
    else if (storageKey.includes(uploadMarker)) displayPath = `uploads/${storageKey.split(uploadMarker).slice(1).join(uploadMarker)}`;
    return { ...item, storage_key: storageKey, path: displayPath };
  }

  function mapCredential(item) {
    return {
      ...item,
      label: item.name,
      active: !!item.enabled,
    };
  }

  function mapIntent(intent) {
    // ``priority`` is optional on payloads produced before the Intent
    // lifecycle migration.  Preserve a real numeric zero, while treating
    // null/undefined/blank legacy values as the neutral default.
    const rawPriority = intent.priority;
    const normalizedPriority =
      typeof rawPriority === 'string' && rawPriority.trim() === '' ? null : rawPriority;
    const parsedPriority = Number(normalizedPriority);
    return {
      id: intent.id,
      from: intent.from_facts || [],
      to: intent.to_fact_id || null,
      to_fact_id: intent.to_fact_id || null,
      todo_id: intent.todo_id || null,
      chain_id: intent.chain_id || null,
      goal_cycle_id: intent.goal_cycle_id || null,
      kind: intent.kind || '',
      description: intent.description,
      priority:
        normalizedPriority === null || normalizedPriority === undefined || !Number.isFinite(parsedPriority)
          ? 50
          : parsedPriority,
      assessment_status: intent.assessment_status || 'open',
      assessment_reason: intent.assessment_reason || '',
      assessed_at: intent.assessed_at || null,
      status: intent.status,
      creator: intent.creator,
      worker: intent.worker,
      worker_pool: intent.worker_pool || [],
      targets: intent.targets || [],
      timeouts: {
        execute_seconds: intent.execute_timeout_seconds || null,
        conclude_seconds: intent.conclude_timeout_seconds || null,
      },
      created_at: intent.created_at,
      concluded_at: intent.concluded_at,
      last_heartbeat_at: intent.last_heartbeat_at,
      activity_kind: intent.activity_kind || '',
      activity_summary: intent.activity_summary || '',
      activity_tool: intent.activity_tool || '',
      activity_started_at: intent.activity_started_at || null,
      activity_updated_at: intent.activity_updated_at || null,
      archived: !!intent.archived,
      retry_count: intent.retry_count || 0,
      infrastructure_retry_count: intent.infrastructure_retry_count || 0,
      failure_class: intent.failure_class || '',
      next_retry_at: intent.next_retry_at || null,
      last_failure_kind: intent.last_failure_kind || '',
      human_intervention_required: !!intent.human_intervention_required,
      human_intervention_reason: intent.human_intervention_reason || '',
      human_intervention_marked_at: intent.human_intervention_marked_at || null,
      activity_kind: intent.activity_kind || '',
      activity_summary: intent.activity_summary || '',
      activity_tool: intent.activity_tool || '',
      activity_started_at: intent.activity_started_at || null,
      activity_updated_at: intent.activity_updated_at || null,
    };
  }

  function mapAuditEvent(event) {
    return {
      ...event,
      project_id: event.task_id || event.project_id,
      phase: event.payload?.phase || '',
      task_type: event.payload?.phase || '',
      payload: event.payload || {},
      event: event.event_type,
      timestamp: event.created_at,
    };
  }

  function mapGuardEvent(event) {
    const payload = event.payload || {};
    const toolName = String(event.message || '').split(':').pop().trim() || 'tool';
    return {
      id: event.id,
      project_id: event.task_id || event.project_id || '',
      intent_id: event.intent_id || '',
      worker: event.worker || '',
      phase: payload.phase || '',
      tool_name: toolName,
      decision: payload.decision || (event.level === 'warning' ? 'BLOCK' : 'ALLOW'),
      reason: payload.reason || '',
      decision_source: payload.source || '',
      mode: '',
      command: payload.command || '',
      normalized_json: JSON.stringify(payload.normalized || {}),
      input_json: JSON.stringify(payload.input || {}),
      raw_model_output: payload.raw_model_output || '',
      model_latency_ms: payload.model_latency_ms || 0,
      created_at: event.created_at,
    };
  }

  function emptyTaskDetail(mappedTask, archivedLocked = false) {
    return {
      project: mappedTask,
      facts: [],
      intents: [],
      goal_status: 'active',
      goal_completed_at: null,
      goal_completion_reason: '',
      goal_completion_evaluator: '',
      goal_completion_evaluation_id: null,
      goal_completion_reason_run_id: null,
      goal_from_fact_ids: [],
      goal_history: [],
      goal_cycles: [],
      goal_evaluations: [],
      hints: [],
      todos: [],
      chains: [],
      red_team_results: [],
      vulns: [],
      artifacts: [],
      lead_artifacts: [],
      archived_locked: archivedLocked,
      events: [],
    };
  }

  function copyTaskDetailBase(baseDetail, mappedTask) {
    const base = baseDetail || {};
    return {
      ...emptyTaskDetail(mappedTask, !!base.archived_locked),
      facts: [...(base.facts || [])],
      intents: [...(base.intents || [])],
      goal_status: base.goal_status || 'active',
      goal_completed_at: base.goal_completed_at || null,
      goal_completion_reason: base.goal_completion_reason || '',
      goal_completion_evaluator: base.goal_completion_evaluator || '',
      goal_completion_evaluation_id: base.goal_completion_evaluation_id || null,
      goal_completion_reason_run_id: base.goal_completion_reason_run_id || null,
      goal_from_fact_ids: [...(base.goal_from_fact_ids || [])],
      goal_history: [...(base.goal_history || [])],
      goal_cycles: [...(base.goal_cycles || [])],
      goal_evaluations: [...(base.goal_evaluations || [])],
      hints: [...(base.hints || [])],
      todos: [...(base.todos || [])],
      chains: [...(base.chains || [])],
      red_team_results: [...(base.red_team_results || [])],
      vulns: [...(base.vulns || [])],
      artifacts: [...(base.artifacts || [])],
      lead_artifacts: [...(base.lead_artifacts || [])],
      events: [...(base.events || [])],
    };
  }

  function emitTaskDetailSection(options, section) {
    if (typeof options.onTaskDetailSection !== 'function') return;
    try { options.onTaskDetailSection(section); }
    catch (error) { console.debug('Task detail section callback failed', error); }
  }

  async function taskDetailIncremental(taskId, baseDetail, options) {
    let mappedTask = baseDetail?.project || null;
    if (!mappedTask) {
      const task = await request('GET', `/tasks/${taskId}`, null, options);
      mappedTask = mapTask(task);
      baseDetail = emptyTaskDetail(mappedTask, !!task.archived);
    }
    const detail = copyTaskDetailBase(baseDetail, mappedTask);
    if (mappedTask.archived || detail.archived_locked) {
      detail.archived_locked = true;
      return detail;
    }

    const allSections = [
      'project_group', 'graph', 'hints', 'todos', 'vulnerabilities',
      'artifacts', 'lead_artifacts', 'goal_cycles', 'goal_evaluations',
    ];
    const selectedSections = Array.isArray(options.sections) && options.sections.length
      ? new Set(options.sections.filter(name => allSections.includes(name)))
      : new Set(allSections);
    const runSection = async (name, loader, apply) => {
      try {
        const data = await loader();
        apply(data);
        emitTaskDetailSection(options, { name, data });
        return data;
      } catch (error) {
        emitTaskDetailSection(options, { name, error });
        return null;
      }
    };

    let projectGroupPromise = Promise.resolve(null);
    if (selectedSections.has('project_group')) {
      projectGroupPromise = runSection(
        'project_group',
        () => request('GET', `/projects/${mappedTask.parent_project_id}`, null, options),
        () => {},
      );
    } else if (selectedSections.has('vulnerabilities')) {
      projectGroupPromise = request('GET', `/projects/${mappedTask.parent_project_id}`, null, options).catch(() => null);
    }

    const pending = [];
    if (selectedSections.has('graph')) {
      pending.push(runSection('graph', async () => {
        const graph = await request('GET', `/tasks/${taskId}/graph`, null, options);
        return { ...graph, facts: graph.facts || [], intents: (graph.intents || []).map(mapIntent) };
      }, graph => {
        detail.facts = graph.facts || [];
        detail.intents = graph.intents || [];
        detail.goal_status = graph.goal_status || 'active';
        detail.goal_completed_at = graph.goal_completed_at || null;
        detail.goal_completion_reason = graph.goal_completion_reason || '';
        detail.goal_completion_evaluator = graph.goal_completion_evaluator || '';
        detail.goal_completion_evaluation_id = graph.goal_completion_evaluation_id || null;
        detail.goal_completion_reason_run_id = graph.goal_completion_reason_run_id || null;
        detail.goal_from_fact_ids = graph.goal_from_fact_ids || [];
        detail.goal_history = graph.goal_history || [];
        detail.project.fact_count = detail.facts.length;
        detail.project.intent_count = detail.intents.length;
      }));
    }
    if (selectedSections.has('hints')) {
      pending.push(runSection('hints', () => request('GET', `/tasks/${taskId}/hints`, null, options), items => {
        detail.hints = items || [];
      }));
    }
    if (selectedSections.has('todos')) {
      pending.push(runSection('todos', () => request('GET', `/tasks/${taskId}/todos?limit=200`, null, options), items => {
        detail.todos = items || [];
      }));
      if (mappedTask.engagement_mode === 'red_team') {
        pending.push(runSection('chains', () => request('GET', `/tasks/${taskId}/red-team/chains`, null, options), items => {
          detail.chains = items || [];
        }));
        pending.push(runSection('red_team_results', () => request('GET', `/tasks/${taskId}/red-team/results`, null, options), items => {
          detail.red_team_results = items || [];
        }));
      }
    }
    if (selectedSections.has('vulnerabilities')) {
      pending.push(runSection('vulnerabilities', async () => {
        const [items, group] = await Promise.all([
          request('GET', `/tasks/${taskId}/vulnerabilities?limit=200`, null, options),
          projectGroupPromise,
        ]);
        return (items || []).map(item => mapVulnerability(item, detail.project, group));
      }, items => { detail.vulns = items; }));
    }
    if (selectedSections.has('artifacts')) {
      pending.push(runSection('artifacts', () => request('GET', `/tasks/${taskId}/artifacts?limit=12`, null, options), items => {
        detail.artifacts = (items || []).map(mapArtifact);
      }));
    }
    if (selectedSections.has('lead_artifacts')) {
      pending.push(runSection('lead_artifacts', async () => {
        const [exploreLeads, bootstrapLeads] = await Promise.all([
          request('GET', `/tasks/${taskId}/artifacts?artifact_type=explore.leads&limit=20`, null, options),
          request('GET', `/tasks/${taskId}/artifacts?artifact_type=bootstrap.initial_leads&limit=20`, null, options),
        ]);
        return [...(exploreLeads || []), ...(bootstrapLeads || [])].map(mapArtifact);
      }, items => { detail.lead_artifacts = items; }));
    }
    if (selectedSections.has('goal_cycles')) {
      pending.push(runSection('goal_cycles', () => request('GET', `/tasks/${taskId}/goal-cycles`, null, options), items => {
        detail.goal_cycles = items || [];
      }));
    }
    if (selectedSections.has('goal_evaluations')) {
      pending.push(runSection('goal_evaluations', () => request('GET', `/tasks/${taskId}/goal-evaluations`, null, options), items => {
        detail.goal_evaluations = items || [];
      }));
    }
    if (selectedSections.has('project_group')) pending.push(projectGroupPromise);
    await Promise.all(pending);
    return detail;
  }

  async function taskDetail(taskId, options = {}) {
    const detailMode = options.detailMode || 'full';
    if (detailMode === 'incremental') {
      return taskDetailIncremental(taskId, options.baseDetail || null, options);
    }

    const task = await request('GET', `/tasks/${taskId}`, null, options);
    const mappedTask = mapTask(task);
    if (detailMode === 'summary') return emptyTaskDetail(mappedTask, !!task.archived);

    const project = await request('GET', `/projects/${task.project_id}`, null, options).catch(() => null);
    if (task.archived) return emptyTaskDetail(mappedTask, true);
    const [graph, hints, todos, vulnerabilities, artifacts, leadArtifacts, bootstrapLeadArtifacts, goalCycles, goalEvaluations] = await Promise.all([
      request('GET', `/tasks/${taskId}/graph`, null, options),
      request('GET', `/tasks/${taskId}/hints`, null, options),
      request('GET', `/tasks/${taskId}/todos?limit=200`, null, options),
      request('GET', `/tasks/${taskId}/vulnerabilities?limit=200`, null, options),
      request('GET', `/tasks/${taskId}/artifacts?limit=12`, null, options),
      request('GET', `/tasks/${taskId}/artifacts?artifact_type=explore.leads&limit=20`, null, options),
      request('GET', `/tasks/${taskId}/artifacts?artifact_type=bootstrap.initial_leads&limit=20`, null, options),
      request('GET', `/tasks/${taskId}/goal-cycles`, null, options),
      request('GET', `/tasks/${taskId}/goal-evaluations`, null, options),
    ]);
    mappedTask.fact_count = graph.facts.length;
    mappedTask.intent_count = graph.intents.length;
    return {
      project: mappedTask,
      facts: graph.facts,
      intents: graph.intents.map(mapIntent),
      goal_status: graph.goal_status || 'active',
      goal_completed_at: graph.goal_completed_at || null,
      goal_completion_reason: graph.goal_completion_reason || '',
      goal_completion_evaluator: graph.goal_completion_evaluator || '',
      goal_completion_evaluation_id: graph.goal_completion_evaluation_id || null,
      goal_completion_reason_run_id: graph.goal_completion_reason_run_id || null,
      goal_from_fact_ids: graph.goal_from_fact_ids || [],
      goal_history: graph.goal_history || [],
      goal_cycles: goalCycles || [],
      goal_evaluations: goalEvaluations || [],
      hints,
      todos,
      chains: task.engagement_mode === 'red_team'
        ? await request('GET', `/tasks/${taskId}/red-team/chains`, null, options).catch(() => [])
        : [],
      red_team_results: task.engagement_mode === 'red_team'
        ? await request('GET', `/tasks/${taskId}/red-team/results`, null, options).catch(() => [])
        : [],
      vulns: vulnerabilities.map(item => mapVulnerability(item, task, project)),
      artifacts: artifacts.map(mapArtifact),
      lead_artifacts: [...leadArtifacts, ...bootstrapLeadArtifacts].map(mapArtifact),
      archived_locked: false,
      events: [],
    };
  }

  function workerEnv(worker) {
    return worker?.config?.environment || {};
  }

  function workerThinkingConfig(config) {
    const legacy = String(config?.thinking || 'off').toLowerCase();
    const configuredLevel = String(config?.thinking_level || '').toLowerCase();
    const level = piThinkingLevels.includes(configuredLevel)
      ? configuredLevel
      : (piThinkingLevels.includes(legacy) ? legacy : 'medium');
    const enabled = typeof config?.thinking_enabled === 'boolean'
      ? config.thinking_enabled
      : legacy !== 'off';
    return { enabled, level };
  }

  function mapWorker(worker) {
    const env = workerEnv(worker);
    const config = worker?.config || {};
    const thinking = workerThinkingConfig(config);
    const providerApi = config.provider_api === 'responses'
      ? 'openai-responses'
      : (config.provider_api || 'openai-completions');
    return {
      name: worker.name,
      persisted_name: worker.name,
      type: worker.worker_type || 'pi',
      task_types: worker.capabilities || [],
      max_running: worker.max_running,
      priority: worker.priority || 0,
      pi_model: config.model || '',
      model: config.model || '',
      pi_base_url: config.base_url || '',
      pi_provider_api: providerApi,
      pi_model_context_window: String(config.context_window || '200000'),
      pi_api_key: '',
      pi_api_key_masked: config.api_key ? '••••••••' : '',
      pi_thinking_enabled: thinking.enabled,
      pi_thinking_level: thinking.level,
      // Keep the old view model readable for index-legacy.html only. New
      // saves persist the independent switch and strength fields below.
      pi_thinking: thinking.enabled ? thinking.level : 'off',
      llm_proxy: !!config.llm_proxy,
      mcp_enabled: String(env.PI_MCP_ENABLED || 'false') === 'true',
      mcp_profile: env.PI_MCP_PROFILE || '',
      mcp_config_json: env.PI_MCP_CONFIG_JSON || '',
      mcp_allowed_servers: env.PI_MCP_ALLOWED_SERVERS || '',
      mcp_allowed_tools: env.PI_MCP_ALLOWED_TOOLS || '',
      disabled: !!worker.disabled,
      cooldown_until: worker.cooldown_until,
      last_failure_kind: worker.last_failure_kind || '',
    };
  }

  function mapDispatchSettings(runtime, workers) {
    const config = runtime.config;
    return {
      ...config,
      bootstrap_concurrency: 1,
      reason_concurrency: 1,
      workers: workers.map(mapWorker),
      config_version: runtime.version,
      config_updated_at: runtime.updated_at,
      dispatcher_applied_version: runtime.applied_version ?? 0,
      dispatcher_applied_at: runtime.applied_at || '',
      effective_workers: workers.filter(worker => !worker.disabled).map(worker => ({ name: worker.name })),
      proxy_url: config.network_proxy_url || '',
      proxy_username: '',
      proxy_password: '',
      proxy_enabled: config.network_proxy_mode !== 'off',
    };
  }

  async function dispatchSettings() {
    const [runtime, workers] = await Promise.all([
      request('GET', '/settings/runtime'),
      request('GET', '/workers'),
    ]);
    return mapDispatchSettings(runtime, workers);
  }

  async function saveDispatch(body) {
    const current = await request('GET', '/settings/runtime');
    const expectedVersion = Number(body.config_version ?? current.version);
    if (expectedVersion !== Number(current.version)) {
      const error = new Error(`配置已被其他操作更新：页面 v${expectedVersion}，当前 v${current.version}，请刷新后重试`);
      error.status = 409;
      throw error;
    }
    const config = { ...current.config };
    // sandbox/browser 族是枚举、字符串与布尔字段，绝不能走 Number() 强转——
    // Number('local') 为 NaN，JSON 序列化成 null，直接触发后端 422。
    const passthroughKeys = new Set([
      'sandbox_backend', 'sandbox_required', 'sandbox_local_fallback',
      'sandbox_template', 'sandbox_runtime_revision',
      'browser_gateway_enabled', 'browser_gateway_bind', 'browser_gateway_base_url',
    ]);
    for (const key of runtimeConfigKeys) {
      if (passthroughKeys.has(key)) continue;
      if (body[key] !== undefined && body[key] !== null && body[key] !== '') {
        const num = Number(body[key]);
        if (Number.isFinite(num)) config[key] = num;
      }
    }
    for (const key of passthroughKeys) {
      if (body[key] !== undefined) config[key] = body[key];
    }
    if (body.network_proxy_mode !== undefined) config.network_proxy_mode = body.network_proxy_mode;
    if (body.network_proxy_url !== undefined) config.network_proxy_url = body.network_proxy_url;
    if (body.dingtalk_enabled !== undefined) config.dingtalk_enabled = Boolean(body.dingtalk_enabled);
    if (body.dingtalk_webhook_url !== undefined) config.dingtalk_webhook_url = String(body.dingtalk_webhook_url);
    if (body.dingtalk_secret !== undefined) config.dingtalk_secret = String(body.dingtalk_secret);
    if (body.dingtalk_app_key !== undefined) config.dingtalk_app_key = String(body.dingtalk_app_key);
    if (body.dingtalk_app_secret !== undefined) config.dingtalk_app_secret = String(body.dingtalk_app_secret);
    const savedRuntime = await request('PUT', '/settings/runtime', {
      config,
      expected_version: expectedVersion,
      actor: 'human.frontend',
    });
    const workers = await request('GET', '/workers');
    return mapDispatchSettings(savedRuntime, workers);
  }

  async function saveWorker(persistedName, body) {
    const targetName = String(body.name || '').trim();
    if (!targetName) throw new Error('Worker 名称不能为空');
    const capabilities = (body.task_types || [])
      .filter(item => ['bootstrap', 'reason', 'explore', 'plan', 'review'].includes(item));
    if (!capabilities.length) throw new Error(`Worker ${targetName} 至少要勾选一个参与阶段`);

    const existing = persistedName
      ? await request('GET', `/workers/${encodeURIComponent(persistedName)}`).catch(() => null)
      : null;
    const oldConfig = existing?.config || {};
    const requestedLevel = String(body.pi_thinking_level || '').toLowerCase();
    const thinkingLevel = piThinkingLevels.includes(requestedLevel)
      ? requestedLevel
      : (piThinkingLevels.includes(legacyThinking) ? legacyThinking : 'medium');
    const thinkingEnabled = typeof body.pi_thinking_enabled === 'boolean'
      ? body.pi_thinking_enabled
      : legacyThinking !== 'off';
    const nextConfig = {
      ...oldConfig,
      model: body.pi_model || '',
      base_url: body.pi_base_url || '',
      provider_api: body.pi_provider_api || 'openai-completions',
      context_window: Number(body.pi_model_context_window ?? 200000),
      thinking_enabled: thinkingEnabled,
      thinking_level: thinkingLevel,
      llm_proxy: !!body.llm_proxy,
      environment: { ...(oldConfig.environment || {}) },
    };
    delete nextConfig.env;
    delete nextConfig.thinking;
    delete nextConfig.thinking_capable;
    delete nextConfig.thinking_tested_at;
    if (body.pi_api_key) nextConfig.api_key = body.pi_api_key;
    // 改名走 rename 端点原地改名：保留 created_at/冷却状态/工具绑定，
    // 且目标名已存在时报 409 而不是静默覆盖。
    const nameChanged = !!persistedName && !!targetName && persistedName !== targetName;
    await request('PUT', `/workers/${encodeURIComponent(nameChanged ? persistedName : targetName)}`, {
      worker_type: 'pi',
      capabilities,
      max_running: Number(body.max_running ?? 1),
      priority: Number(body.priority ?? 0),
      config: nextConfig,
      disabled: !!body.disabled,
    });
    if (nameChanged) {
      await request('POST', `/workers/${encodeURIComponent(persistedName)}/rename`, { new_name: targetName });
    }
    return dispatchSettings();
  }

  async function judgeSettings() {
    const [runtime, defaults] = await Promise.all([
      request('GET', '/settings/runtime'),
      request('GET', '/guard/defaults'),
    ]);
    const config = runtime.config;
    return {
      enabled_default: !!config.guard_enabled_default,
      mode: config.guard_mode,
      base_url: config.guard_model_base_url,
      model: config.guard_model_name,
      timeout_seconds: config.guard_timeout_seconds,
      whitelist_tools: config.guard_whitelist_tools ?? defaults.whitelist_tools ?? [],
      whitelist_bash_verbs: config.guard_whitelist_bash_verbs ?? defaults.whitelist_bash_verbs ?? [],
      hard_block_rules: config.guard_hard_block_rules ?? defaults.hard_block_rules ?? [],
      default_hard_block_rules: defaults.hard_block_rules || [],
      system_prompt: config.guard_system_prompt ?? defaults.system_prompt ?? '',
      default_system_prompt: defaults.system_prompt || '',
      has_key: !!config.guard_api_key,
      masked_key: config.guard_api_key ? '••••••••' : '',
      updated_at: runtime.updated_at,
    };
  }

  async function saveJudge(body, key) {
    const runtime = await request('GET', '/settings/runtime');
    const config = {
      ...runtime.config,
      guard_mode: body.mode ?? runtime.config.guard_mode,
      guard_enabled_default: body.enabled_default !== undefined
        ? !!body.enabled_default
        : runtime.config.guard_enabled_default,
      guard_model_base_url: body.base_url ?? runtime.config.guard_model_base_url,
      guard_model_name: body.model ?? runtime.config.guard_model_name,
      guard_timeout_seconds: Number(body.timeout_seconds ?? runtime.config.guard_timeout_seconds),
      guard_system_prompt: body.system_prompt ?? runtime.config.guard_system_prompt,
      guard_whitelist_tools: body.whitelist_tools ?? runtime.config.guard_whitelist_tools,
      guard_whitelist_bash_verbs: body.whitelist_bash_verbs ?? runtime.config.guard_whitelist_bash_verbs,
      guard_hard_block_rules: body.hard_block_rules ?? runtime.config.guard_hard_block_rules,
    };
    if (key !== undefined) config.guard_api_key = key;
    await request('PUT', '/settings/runtime', {
      config,
      expected_version: runtime.version,
      actor: 'human.frontend',
    });
    return judgeSettings();
  }

  async function legacyApi(method, rawPath, body, options = {}) {
    const url = new URL(rawPath, location.origin);
    const path = url.pathname;
    const query = url.searchParams;
    let match;

    // Extension Tool and Worker overview are native V2 resources.  Keep them in
    // the shared adapter so both management pages use the same API boundary
    // as the rest of the application instead of inventing a second fetch
    // helper or duplicating Worker state in the browser.
    if (path === '/axiom-tools' || path.startsWith('/axiom-tools/')) {
      return request(method, `${path}${url.search}`, body, options);
    }
    if (path === '/phase-capabilities' || path.startsWith('/phase-capabilities/')) {
      return request(method, `${path}${url.search}`, body, options);
    }
    if (path === '/worker-phases' || path.startsWith('/worker-phases/')) {
      return request(method, `${path}${url.search}`, body, options);
    }
    if (path === '/workers/overview') {
      return request(method, `${path}${url.search}`, body, options);
    }
    if ((match = path.match(/^\/workers\/([^/]+)\/axiom-tools$/))) {
      return request(
        method,
        `/workers/${encodeURIComponent(decodeURIComponent(match[1]))}/axiom-tools${url.search}`,
        body,
        options,
      );
    }

    if (path === '/project-groups' && method === 'GET') {
      const projects = await request('GET', '/projects', null, options);
      return projects.map(project => ({
        ...project,
        task_count: project.task_count || 0,
        active_task_count: project.active_task_count || 0,
        vuln_count: project.vulnerability_count || 0,
        blackboard_count: project.knowledge_count || knowledgeCounts.get(project.id) || 0,
        credential_count: project.credential_count || 0,
      }));
    }
    if (path === '/project-groups' && method === 'POST') return request('POST', '/projects', body);
    if ((match = path.match(/^\/project-groups\/([^/]+)$/))) {
      if (method === 'PUT') return request('PATCH', `/projects/${match[1]}`, body);
      if (method === 'DELETE') return request('DELETE', `/projects/${match[1]}`);
    }
    if ((match = path.match(/^\/project-groups\/([^/]+)\/blackboard$/))) {
      const projectId = match[1];
      if (method === 'GET') {
        const items = await request('GET', `/projects/${projectId}/knowledge`);
        knowledgeCounts.set(projectId, items.length);
        return items.map(item => ({ ...item, creator: item.created_by, updated_by: item.updated_by }));
      }
      if (method === 'POST') {
        const created = await request('POST', `/projects/${projectId}/knowledge`, {
          title: body.title || String(body.content || '共享知识').slice(0, 80),
          content: body.content || body.title || '',
          category: 'note',
          created_by: body.creator || 'human',
        });
        knowledgeCounts.set(projectId, (knowledgeCounts.get(projectId) || 0) + 1);
        return created;
      }
    }
    if ((match = path.match(/^\/project-groups\/([^/]+)\/blackboard\/([^/]+)$/))) {
      if (method === 'PUT') return request('PATCH', `/projects/${match[1]}/knowledge/${match[2]}`, {
        title: body.title || String(body.content || '共享知识').slice(0, 80), content: body.content,
      });
      if (method === 'DELETE') {
        const result = await request('DELETE', `/projects/${match[1]}/knowledge/${match[2]}?actor=human`);
        knowledgeCounts.set(match[1], Math.max(0, (knowledgeCounts.get(match[1]) || 1) - 1));
        return result;
      }
    }

    if (path === '/projects' && method === 'GET') return (await request('GET', `/tasks${url.search}`, null, options)).map(mapTask);
    if (path === '/projects' && method === 'POST') {
      const task = await request('POST', '/tasks', {
        project_id: body.parent_project_id,
        title: body.title,
        origin: body.origin,
        goal: body.goal,
        hints: (body.hints || []).map(item => item.content || item).filter(Boolean),
        guard_enabled: !!body.guard_enabled,
        worker_pool: body.worker_pool || [],
        scope: body.task_scope || null,
        run_duration_seconds: body.run_duration_seconds ?? null,
        engagement_mode: body.engagement_mode || 'vulnerability',
        red_team_objective: body.red_team_objective || null,
      });
      return { project: mapTask(task) };
    }
    if ((match = path.match(/^\/projects\/([^/]+)$/))) {
      if (method === 'GET') return taskDetail(match[1], options);
      if (method === 'DELETE') return request('DELETE', `/tasks/${match[1]}`);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/(archive|unarchive)$/)) && method === 'POST') {
      const actor = encodeURIComponent(body?.actor || 'human.frontend');
      const endpoint = match[2] === 'archive' ? 'archive' : 'unarchive';
      return mapTask(await request('POST', `/tasks/${match[1]}/${endpoint}?actor=${actor}`));
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/title$/)) && method === 'PUT') {
      return mapTask(await request('PATCH', `/tasks/${match[1]}`, { title: body.title }));
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/guard$/)) && method === 'PUT') {
      return mapTask(await request('PATCH', `/tasks/${match[1]}`, {
        guard_enabled: !!body?.enabled,
      }));
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/status$/)) && method === 'PUT') {
      const endpoint = body.status === 'stopped' ? 'stop' : 'resume';
      const payload = endpoint === 'resume'
        ? { run_duration_seconds: body.run_duration_seconds ?? null }
        : {};
      return mapTask(await request('POST', `/tasks/${match[1]}/${endpoint}`, payload));
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/worker-pool$/)) && method === 'PUT') {
      const result = await request('PATCH', `/tasks/${match[1]}`, {
        worker_pool: body.worker_pool || [],
      });
      return { project: mapTask(result), updated_unclaimed_intents: 0 };
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/hints$/)) && method === 'POST') {
      return request('POST', `/tasks/${match[1]}/hints`, { content: body.content, creator: body.creator || 'human' });
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/hints\/([^/]+)$/)) && method === 'PUT') {
      return request('PATCH', `/tasks/${match[1]}/hints/${match[2]}`, {
        content: body.content,
      });
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/facts\/([^/]+)$/))) {
      if (method === 'PUT') return request('PATCH', `/tasks/${match[1]}/facts/${match[2]}`, {
        description: body.description,
        actor: body.actor || 'human.frontend',
      });
      if (method === 'DELETE') return request('DELETE', `/tasks/${match[1]}/facts/${match[2]}?actor=human.frontend`);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/facts\/([^/]+)\/archive$/)) && method === 'PUT') {
      return request('POST', `/tasks/${match[1]}/facts/${match[2]}/toggle-archive`, { actor: 'human.frontend' });
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/intents\/([^/]+)$/))) {
      if (method === 'GET') return mapIntent(await request('GET', `/tasks/${match[1]}/intents/${match[2]}`, null, options));
      if (method === 'PUT') return request('PATCH', `/tasks/${match[1]}/intents/${match[2]}`, {
        description: body.description,
        actor: body.actor || 'human.frontend',
      });
      if (method === 'DELETE') return request('DELETE', `/tasks/${match[1]}/intents/${match[2]}?actor=human.frontend`);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/intents\/([^/]+)\/archive$/)) && method === 'PUT') {
      return request('POST', `/tasks/${match[1]}/intents/${match[2]}/toggle-archive`, { actor: 'human.frontend' });
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/intents\/([^/]+)\/reset-retries$/)) && method === 'PUT') {
      const intent = await request('POST', `/tasks/${match[1]}/intents/${match[2]}/reset-retries`, { actor: 'human.frontend' }, options);
      return mapIntent(intent);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/events$/)) && method === 'GET') {
      const afterId = query.get('after_id') || '';
      const initialLoad = !afterId || afterId === '0';
      const params = new URLSearchParams({ limit: initialLoad ? '2000' : (query.get('limit') || '500') });
      if (!initialLoad) params.set('after_id', afterId);
      params.set('task_id', match[1]);
      const events = await request('GET', `/audit-events?${params.toString()}`, null, options);
      return (initialLoad ? events.reverse() : events).map(mapAuditEvent);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/maintainer-status$/)) && method === 'GET') return { maintaining_intent_ids: [] };
    if ((match = path.match(/^\/projects\/([^/]+)\/diagnostics$/)) && method === 'GET') {
      return request('GET', `/tasks/${match[1]}/diagnostics${url.search}`, null, options);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/conversation(?:\/index)?$/)) && method === 'GET') {
      return { project_id: match[1], threads: [], items: [], stats: { workers: [], intents: [], thread_types: {}, timeout_count: 0, thread_count: 0, item_count: 0 }, filters: {} };
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/intents\/([^/]+)\/interventions$/)) && method === 'POST') {
      return request('POST', `/tasks/${match[1]}/intents/${match[2]}/interventions`, body, options);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/vulns$/))) {
      if (method === 'GET') {
        const task = await request('GET', `/tasks/${match[1]}`);
        const project = await request('GET', `/projects/${task.project_id}`).catch(() => null);
        return (await request('GET', `/tasks/${match[1]}/vulnerabilities`)).map(item => mapVulnerability(item, task, project));
      }
      if (method === 'POST') return request('POST', `/tasks/${match[1]}/vulnerabilities`, vulnerabilityBody(body));
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/artifacts$/)) && method === 'GET') {
      return (await request('GET', `/tasks/${match[1]}/artifacts`)).map(mapArtifact);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/vulns\/([^/]+)$/))) {
      if (method === 'PUT') return request('PATCH', `/tasks/${match[1]}/vulnerabilities/${match[2]}`, vulnerabilityBody(body, true));
      if (method === 'DELETE') return request('DELETE', `/tasks/${match[1]}/vulnerabilities/${match[2]}`);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/vulns\/([^/]+)\/human-reviewed$/)) && method === 'PUT') {
      return request('POST', `/tasks/${match[1]}/vulnerabilities/${match[2]}/human-reviewed`, {
        value: !!body.value,
        actor: body.actor || 'human.frontend',
      });
    }

    if (path === '/settings/dispatch' && method === 'GET') return dispatchSettings();
    if (path === '/settings/dispatch' && method === 'PUT') return saveDispatch(body);
    if ((match = path.match(/^\/settings\/dispatch\/workers\/([^/]+)$/))) {
      const persistedName = decodeURIComponent(match[1]);
      if (method === 'PUT') return saveWorker(persistedName === '__new__' ? '' : persistedName, body);
      if (method === 'DELETE') {
        if (persistedName !== '__new__') {
          await request('DELETE', `/workers/${encodeURIComponent(persistedName)}`);
        }
        return dispatchSettings();
      }
    }
    if ((match = path.match(/^\/settings\/dispatch\/workers\/([^/]+)\/rename$/)) && method === 'POST') {
      const persistedName = decodeURIComponent(match[1]);
      const target = String(body?.new_name || '').trim();
      if (persistedName !== '__new__' && target && target !== persistedName) {
        await request('POST', `/workers/${encodeURIComponent(persistedName)}/rename`, { new_name: target });
      }
      return dispatchSettings();
    }
    if ((match = path.match(/^\/settings\/dispatch\/workers\/([^/]+)\/toggle$/)) && method === 'PUT') {
      const workers = await request('GET', '/workers');
      const worker = workers.find(item => item.name === decodeURIComponent(match[1]));
      if (!worker) throw new Error('Worker not found');
      await request('PUT', `/workers/${encodeURIComponent(worker.name)}`, { ...worker, disabled: !worker.disabled });
      return dispatchSettings();
    }
    if (path === '/settings/dispatch/models' && method === 'GET') {
      const worker = query.get('worker_name') || '';
      return request('GET', `/workers/${encodeURIComponent(worker)}/models?base_url=${encodeURIComponent(query.get('base_url') || '')}&api_key=${encodeURIComponent(query.get('api_key') || '')}&provider_api=${encodeURIComponent(query.get('provider_api') || '')}`);
    }
    if (path === '/settings/test-pi' && method === 'POST') {
      return request('POST', `/workers/${encodeURIComponent(body.worker_name || '')}/test`, {
        base_url: body.pi_base_url, api_key: body.pi_api_key, model: body.pi_model, provider_api: body.pi_provider_api, llm_proxy: !!body.llm_proxy,
      });
    }
    if (path === '/settings/judge' && method === 'GET') return judgeSettings();
    if (path === '/settings/judge' && method === 'PUT') return saveJudge(body);
    if (path === '/settings/keys/judge' && method === 'PUT') return saveJudge({}, body.api_key);
    if (path === '/settings/judge/test' && method === 'POST') {
      const [settings, runtime] = await Promise.all([judgeSettings(), request('GET', '/settings/runtime')]);
      const result = await request('POST', `/workers/__guard__/test`, {
        base_url: settings.base_url, api_key: runtime.config.guard_api_key || '', model: settings.model, provider_api: 'openai-completions',
      }).catch(error => ({ ok: false, message: error.message }));
      return result;
    }
    if (path === '/settings' && method === 'GET') {
      const runtime = await request('GET', '/settings/runtime');
      return {
        proxy_enabled: runtime.config.network_proxy_mode !== 'off',
        proxy_url: runtime.config.network_proxy_url,
      };
    }
    if (path === '/settings' && method === 'PUT') return body;
    if (path === '/tool-hooks/events' && method === 'GET') {
      const taskId = query.get('project_id') || '';
      const params = new URLSearchParams({ event_type: 'guard.decision', limit: query.get('limit') || '100' });
      if (taskId) params.set('task_id', taskId);
      let events = (await request('GET', `/audit-events?${params}`)).map(mapGuardEvent);
      const decision = query.get('decision');
      if (decision && decision !== 'all') events = events.filter(item => item.decision.toLowerCase() === decision);
      return events;
    }

    if (path === '/skills' || path.startsWith('/skills/')) return request(method, path, body);
    if (path.startsWith('/integrations/')) return request(method, path, body);

    // MCP is a standalone V2 resource.  The compatibility adapter keeps the
    // established frontend API surface while the page itself no longer lives
    // inside Worker management.
    if (path === '/mcp' || path.startsWith('/mcp/')) return request(method, path, body);
    if (/^\/tasks\/[^/]+\/archives(?:\/|$)/.test(path)) {
      return request(method, `${path}${url.search}`, body);
    }

    throw new Error(`Backend V2 尚未映射前端请求：${method} ${rawPath}`);
  }

  function vulnerabilityBody(body, partial = false) {
    const result = {};
    const assign = (key, value) => { if (!partial || Object.prototype.hasOwnProperty.call(body, key)) result[key] = value; };
    assign('title', body.title);
    assign('type', body.type || '通用漏洞');
    assign('severity', body.severity || 'medium');
    assign('description', body.description || body.title || '');
    assign('prerequisites', body.prerequisites || '');
    assign('impact', body.impact || body.description || body.title || '');
    assign('remediation', body.remediation || body.recommendation || '');
    assign('affected_target', body.affected_target || body.affected_scope || '');
    assign('status', body.status || '待验证');
    assign('status_reason', body.status_reason || '');
    assign('finding_artifact_id', body.finding_artifact_id || null);
    assign('evidence_artifact_ids', body.evidence_artifact_ids || []);
    if (!partial) {
      result.intent_id = body.intent_id || null;
      result.source = body.source || 'human';
      result.reported_by = body.reported_by || 'human';
    }
    return result;
  }

  Object.assign(M.state.state, {
    sideTab: 'detail',
    taskSearch: '',
    taskStatusFilter: 'all',
    projectSearch: '',
    projectStatusFilter: 'all',
    fileArtifactMap: {},
    projectCredentials: [],
    projectCredentialSearch: '',
    credentialEditor: { open: false, loading: false, id: '', projectId: '', name: '', content: '', notes: '', refreshContent: '', enabled: true },
    credentialSaving: false,
    taskScopeEditor: { taskId: '', targetsText: '', excludeText: '', notes: '', saving: false },
  });

  Object.assign(M.core.methods, { api: legacyApi });

  Object.assign(M.projects.methods, {
    manualReasonDisabledReason() {
      const task = this.project?.project;
      if (!task) return '任务信息尚未加载';
      if (this.manualReasonTriggering) return '正在提交人工 Reason 请求';
      if (task.legacy_readonly) return '历史只读任务不能触发 Reason';
      if (task.archived) return '已归档任务不能触发 Reason';
      if (task.status !== 'active') return '只有运行中的任务可以触发 Reason';
      if (task.run_deadline_at && Date.parse(task.run_deadline_at) <= Date.now()) return '任务运行时间已经结束';
      if (task.reason) return `${task.reason.id || 'ReasonRun'} 已在进行中`;
      const bootstrapCompleted = (this.project?.intents || []).some(intent => (
        intent.kind === 'bootstrap' && intent.status === 'completed' && !intent.archived
      ));
      if (!bootstrapCompleted) return 'Bootstrap 完成后才能触发 Reason';
      return '';
    },
    canTriggerManualReason() {
      return !this.manualReasonDisabledReason();
    },
    async triggerManualReason() {
      const taskId = this.selectedProjectId;
      if (!taskId || !this.canTriggerManualReason()) return;
      this.manualReasonTriggering = true;
      try {
        const run = await request('POST', `/tasks/${taskId}/reason-runs/trigger`, {
          actor: this.actorName(),
        });
        await this.loadProject(taskId);
        await this.loadProjects();
        if (this.view === 'graph') this.updateGraph?.();
        this.showToast(`已触发 ${run.id}，等待 Reason Worker 领取`);
      } catch (error) {
        if (error?.status === 409) await this.loadProject(taskId);
        this.showToast(error?.message || '触发 Reason 失败', 'error');
      } finally {
        this.manualReasonTriggering = false;
      }
    },
    openTaskContinuation(task = null) {
      const selected = task || this.project?.project || this.selectedTaskSummary();
      if (!selected || selected.legacy_readonly || selected.status !== 'completed') return;
      this.continuationForm = {
        open: true,
        projectId: selected.id,
        projectTitle: selected.title || '',
        goal: selected.goal || '',
        reason: '',
        focus: '',
        durationMinutes: Math.max(1, Math.round(Number(selected.run_duration_seconds || 14400) / 60)),
        saving: false,
      };
    },
    closeTaskContinuation() {
      if (this.continuationForm.saving) return;
      this.continuationForm.open = false;
    },
    async continueCompletedTask() {
      const form = this.continuationForm;
      if (!form.projectId || !String(form.goal || '').trim() || !String(form.reason || '').trim() || form.saving) return;
      form.saving = true;
      try {
        const result = await request('POST', `/tasks/${form.projectId}/continuations`, {
          goal: String(form.goal).trim(),
          reason: String(form.reason).trim(),
          focus: String(form.focus || '').trim(),
          run_duration_seconds: Math.max(1, Number(form.durationMinutes || 1)) * 60,
          actor: this.actorName(),
        });
        this.continuationForm.open = false;
        await this.loadProjects();
        if (this.selectedProjectId === form.projectId) {
          await this.loadProject(form.projectId);
          if (this.view === 'graph') this.updateGraph();
        }
        this.showToast(`任务已进入 Goal 第 ${result.goal_cycle.sequence} 轮，${result.reason_run_id} 等待领取`);
      } catch (error) {
        if (error?.status === 409) {
          await this.loadProjects();
          if (this.selectedProjectId === form.projectId) await this.loadProject(form.projectId);
        }
        this.showToast(error?.message || '继续任务失败', 'error');
      } finally {
        form.saving = false;
      }
    },
    projectCanWriteHints() {
      return !this.replay.active
        && !this.project?.project?.legacy_readonly
        && ['active', 'stopped', 'completed'].includes(this.project?.project?.status);
    },
    async loadAllProjectDetails() {
      await Promise.all([this.loadProjects(), this.loadDashboardSummary?.()]);
      this.projectDetailsLoaded = true;
    },
    async loadDashboardSummary() {
      try {
        const params = new URLSearchParams({
          token_range: this.dashboardTokenRange || '30d',
          model: this.dashboardTokenModel || 'all',
        });
        this.dashboardSummary = await request('GET', `/dashboard/summary?${params.toString()}`, null, {
          signal: this._pageAbortController?.signal,
        });
        this.projectDetailsLoaded = true;
        this.projectDetailsError = '';
      } catch (error) {
        this.projectDetailsError = error?.message || String(error);
      }
    },
    applyVulnerabilityRouteParams(params) {
      const taskId = String(params?.get('task') || '');
      const vulnerabilityId = taskId ? String(params?.get('vulnerability') || '') : '';
      this.vulnerabilityRouteTaskId = taskId;
      this.vulnerabilityRouteId = vulnerabilityId;
      this.vulnerabilitySelectionBlocked = false;
      this.vulnerabilityReviews = [];
      this.vulnerabilityReviewsError = '';
      if (!taskId) return;
      this.vulnFilter = { ...(this.vulnFilter || {}), projectId: taskId, keyword: '' };
      this.vulnerabilityPage = 0;
      this.selectedVulnerabilityKey = vulnerabilityId ? `${taskId}:${vulnerabilityId}` : '';
    },
    vulnerabilityHash(taskId = '', vulnerabilityId = '') {
      const params = new URLSearchParams();
      if (taskId) params.set('task', taskId);
      if (taskId && vulnerabilityId) params.set('vulnerability', vulnerabilityId);
      const query = params.toString();
      return `#/facts${query ? `?${query}` : ''}`;
    },
    openVulnerabilityLocation(taskId, vulnerabilityId) {
      if (!taskId || !vulnerabilityId) return;
      const target = this.vulnerabilityHash(taskId, vulnerabilityId);
      if (location.hash === target) {
        this.applyVulnerabilityRouteParams(new URLSearchParams(target.split('?')[1] || ''));
        this.loadVulnerabilityPage();
      } else {
        location.hash = target;
      }
    },
    clearVulnerabilityRouteTarget(taskId = '') {
      const retainedTaskId = String(taskId || this.vulnerabilityRouteTaskId || '');
      this.vulnerabilityRouteTaskId = retainedTaskId;
      this.vulnerabilityRouteId = '';
      if (window.history?.replaceState) {
        window.history.replaceState(null, '', this.vulnerabilityHash(retainedTaskId));
      }
    },
    selectVulnerability(vulnerability) {
      if (!vulnerability) return;
      this.vulnerabilitySelectionBlocked = false;
      this.selectedVulnerabilityKey = this.vulnerabilityKey(vulnerability);
      void this.loadVulnerabilityReviews(vulnerability);
    },
    async loadVulnerabilityReviews(vulnerability = null) {
      const selected = vulnerability || this.selectedVulnerability();
      const requestId = ++this._vulnerabilityReviewRequestId;
      if (!selected?.project_id || !selected?.id) {
        this.vulnerabilityReviews = [];
        this.vulnerabilityReviewsError = '';
        return [];
      }
      this.vulnerabilityReviewsLoading = true;
      this.vulnerabilityReviewsError = '';
      try {
        const rows = await request(
          'GET',
          `/tasks/${selected.project_id}/vulnerabilities/${selected.id}/reviews`,
          null,
          { signal: this._pageAbortController?.signal },
        );
        if (requestId !== this._vulnerabilityReviewRequestId) return this.vulnerabilityReviews;
        this.vulnerabilityReviews = (rows || []).map(mapVulnerabilityReviewRun);
        return this.vulnerabilityReviews;
      } catch (error) {
        if (error?.name === 'AbortError') return this.vulnerabilityReviews;
        if (requestId === this._vulnerabilityReviewRequestId) {
          this.vulnerabilityReviews = [];
          this.vulnerabilityReviewsError = error?.message || '加载审核记录失败';
        }
        return [];
      } finally {
        if (requestId === this._vulnerabilityReviewRequestId) {
          this.vulnerabilityReviewsLoading = false;
        }
      }
    },
    latestVulnerabilityReview() {
      return (this.vulnerabilityReviews || [])[0] || null;
    },
    vulnerabilityReviewStateLabel(run) {
      return ({
        pending: '等待审核',
        claimed: '正在启动',
        running: '审核中',
        concluding: '正在收口',
        completed: '已完成',
        failed: '审核失败',
        cancelled: '已取消',
      }[run?.status] || run?.status || '未排队');
    },
    vulnerabilityReviewStateClass(run) {
      return ({
        pending: 'border-amber-200 bg-amber-50 text-amber-700',
        claimed: 'border-blue-200 bg-blue-50 text-blue-700',
        running: 'border-blue-200 bg-blue-50 text-blue-700',
        concluding: 'border-violet-200 bg-violet-50 text-violet-700',
        completed: 'border-emerald-200 bg-emerald-50 text-emerald-700',
        failed: 'border-rose-200 bg-rose-50 text-rose-700',
        cancelled: 'border-slate-200 bg-slate-50 text-slate-500',
      }[run?.status] || 'border-slate-200 bg-slate-50 text-slate-500');
    },
    vulnerabilityReviewDecisionLabel(decision) {
      return ({
        verified: '已验证',
        duplicate: '重复',
        ignored: '忽略',
        'needs-human': '继续待验证',
      }[decision] || decision || '未裁决');
    },
    vulnerabilityReviewTriggerLabel(trigger) {
      return ({
        reported: '漏洞上报',
        updated: '关键内容更新',
        manual: '人工重审',
        restored: '归档恢复',
        migration: '历史补队列',
      }[trigger] || trigger || '未知触发');
    },
    vulnerabilityReviewCanRetry() {
      const selected = this.selectedVulnerability();
      const latest = this.latestVulnerabilityReview();
      if (!selected || selected.archived || this.vulnerabilityReviewSubmitting) return false;
      return !latest || !['pending', 'claimed', 'running', 'concluding'].includes(latest.status);
    },
    async triggerVulnerabilityReview(vulnerability = null) {
      const selected = vulnerability || this.selectedVulnerability();
      if (!selected || !this.vulnerabilityReviewCanRetry()) return;
      if (!window.confirm(`确认重新审核 ${selected.id}？漏洞将恢复为待验证。`)) return;
      this.vulnerabilityReviewSubmitting = true;
      try {
        await request(
          'POST',
          `/tasks/${selected.project_id}/vulnerabilities/${selected.id}/reviews`,
          { actor: this.actorName(), reason: '人工要求重新审核' },
        );
        await this.loadVulnerabilityPage({ selectFirst: false });
        const refreshed = (this.vulnerabilityItems || []).find(
          item => item.project_id === selected.project_id && item.id === selected.id,
        ) || selected;
        this.selectedVulnerabilityKey = this.vulnerabilityKey(refreshed);
        await this.loadVulnerabilityReviews(refreshed);
        this.showToast('已创建 Reviewer 审核任务');
      } catch (error) {
        if (error?.status === 409) await this.loadVulnerabilityReviews(selected);
        this.showToast(error?.message || '重新审核失败', 'error');
      } finally {
        this.vulnerabilityReviewSubmitting = false;
      }
    },
    openReviewDuplicate(run) {
      const selected = this.selectedVulnerability();
      if (!selected?.project_id || !run?.duplicate_of_vulnerability_id) return;
      this.openVulnerabilityLocation(selected.project_id, run.duplicate_of_vulnerability_id);
    },
    switchFactView(mode) {
      this.factViewMode = mode === 'result' ? 'result' : 'vulnerability';
      if (this.factViewMode === 'result') {
        this.loadRedTeamResultPage();
      } else {
        this.loadVulnerabilityPage({ selectFirst: false });
      }
    },
    rtTaskTitle(taskId) {
      const task = (this.projects || []).find(item => item.id === taskId);
      return task ? `${taskId} ${task.title || ''}`.trim() : taskId || '-';
    },
    rtTypeLabel(type) {
      return ({ permission: '权限跃迁', data: '数据获取', credential: '凭证获取', pivot: '横向控制' })[type] || type || '-';
    },
    rtTypeClass(type) {
      return ({ permission: 'ok', data: 'info', credential: 'warn', pivot: 'warn' })[type] || 'off';
    },
    rtTypeTone(type) {
      return ({ permission: 'sto-sev-high', data: 'sto-sev-low', credential: 'sto-sev-medium', pivot: 'sto-sev-critical' })[type] || 'sto-sev-info';
    },
    rtTypeMark(type) {
      return ({ permission: '权', data: '数', credential: '证', pivot: '横' })[type] || '果';
    },
    rtPointsLabel(item) {
      if (!item) return '';
      if (item.status === 'verified') return `+${item.points_awarded || 0} 分`;
      return item.points_declared ? `自评 ${item.points_declared}` : '';
    },
    selectedRtResult() {
      return (this.rtResultItems || []).find(item => item.id === this.selectedRtResultId) || null;
    },
    selectRtResult(item) {
      this.selectedRtResultId = item?.id || '';
      this.loadRtResultChain(item);
    },
    async loadRtResultChain(result) {
      if (!result?.task_id || !result.chain_id) {
        this.rtChainView = null;
        this.rtChainLoading = false;
        return;
      }
      const cacheKey = `${result.task_id}:${result.chain_id}`;
      const cached = this._rtChainCache?.[cacheKey];
      if (cached) {
        this.rtChainView = cached;
        this.rtChainLoading = false;
        return;
      }
      const requestId = (this._rtChainRequestId || 0) + 1;
      this._rtChainRequestId = requestId;
      this.rtChainLoading = true;
      try {
        const [graph, chains] = await Promise.all([
          request('GET', `/tasks/${result.task_id}/graph`),
          request('GET', `/tasks/${result.task_id}/red-team/chains`).catch(() => []),
        ]);
        if (requestId !== this._rtChainRequestId) return;
        const chain = (chains || []).find(item => item.id === result.chain_id);
        const steps = (graph?.intents || [])
          .filter(item => item.chain_id === result.chain_id && !item.archived)
          .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')) || String(a.id).localeCompare(String(b.id)))
          .map(item => ({ id: item.id, description: item.description || '', status: item.status || '' }));
        const view = { taskId: result.task_id, chainId: result.chain_id, title: chain?.title || '', steps };
        this._rtChainCache = { ...(this._rtChainCache || {}), [cacheKey]: view };
        if (this.selectedRtResult()?.chain_id === result.chain_id && this.selectedRtResult()?.task_id === result.task_id) {
          this.rtChainView = view;
        }
      } catch (error) {
        if (requestId !== this._rtChainRequestId) return;
        this.rtChainView = { taskId: result.task_id, chainId: result.chain_id, title: '', steps: [] };
      } finally {
        if (requestId === this._rtChainRequestId) this.rtChainLoading = false;
      }
    },
    async openResultChainIntent(result, intentId) {
      if (!result?.task_id || !intentId) return;
      this.todoNavExpandedId = result.chain_id || '';
      this.todoNavIntentId = intentId;
      this._humanTabReturn = { taskId: result.task_id, tab: 'todos' };
      await this.openTaskOverview(result.task_id);
      this.humanTab = 'todos';
      this.todoNavExpandedId = result.chain_id || '';
      this.todoNavIntentId = intentId;
    },
    openResultEvidence(result, ref) {
      const match = String(ref || '').match(/^artifact:([A-Za-z0-9_-]+)$/);
      if (!match || !result?.task_id) return;
      return this.openArtifactLocation(result.task_id, match[1]);
    },
    rtStatusLabel(status) {
      return ({
        reported: '已上报', reviewing: '审核中', verified: '已验证',
        rejected: '已驳回', duplicate: '重复', 'needs-human': '需人工',
      })[status] || status || '-';
    },
    rtStatusClass(status) {
      return ({ reported: 'info', reviewing: 'warn', verified: 'ok', rejected: 'err', duplicate: 'off', 'needs-human': 'warn' })[status] || 'off';
    },
    rtVerifiedPoints() {
      return (this.rtResultItems || [])
        .filter(item => item.status === 'verified')
        .reduce((sum, item) => sum + (Number(item.points_awarded) || 0), 0);
    },
    async loadRedTeamResultPage() {
      const requestId = ++this._rtResultListRequestId;
      this.rtResultLoading = true;
      try {
        const params = new URLSearchParams({ limit: '100', offset: '0' });
        const keyword = String(this.rtResultFilter.keyword || '').trim();
        if (keyword) params.set('search', keyword);
        if (this.rtResultFilter.projectId) params.set('task_id', this.rtResultFilter.projectId);
        if (this.rtResultFilter.status) params.set('status', this.rtResultFilter.status);
        if (this.rtResultFilter.type) params.set('type', this.rtResultFilter.type);
        const resultPage = await requestPage(`/red-team/results?${params}`, {
          signal: this._pageAbortController?.signal,
        });
        if (requestId !== this._rtResultListRequestId) return;
        this.rtResultItems = resultPage.items;
        this.rtResultTotal = resultPage.total;
        if (!this.rtResultItems.some(item => item.id === this.selectedRtResultId)) {
          this.selectedRtResultId = this.rtResultItems[0]?.id || '';
        }
        this.loadRtResultChain(this.selectedRtResult());
      } catch (error) {
        if (error?.name === 'AbortError') return;
        this.showToast(error?.message || '红队成果加载失败', 'error');
      } finally {
        this.rtResultLoading = false;
      }
    },
    scheduleRtResultSearch() {
      clearTimeout(this._rtResultSearchTimer);
      this._rtResultSearchTimer = setTimeout(() => this.loadRedTeamResultPage(), 250);
    },
    async loadVulnerabilityPage(options = {}) {
      const requestId = ++this._vulnerabilityListRequestId;
      this.vulnerabilityLoading = true;
      try {
        const params = new URLSearchParams({
          limit: String(this.vulnerabilityPageSize),
          offset: String(this.vulnerabilityPage * this.vulnerabilityPageSize),
        });
        const keyword = String(this.vulnFilter.keyword || '').trim();
        if (keyword) params.set('search', keyword);
        if (this.vulnFilter.projectId) params.set('task_id', this.vulnFilter.projectId);
        params.set('sort', String(this.vulnerabilitySort || 'updated'));
        const page = await requestPage(`/vulnerabilities?${params}`, {
          signal: this._pageAbortController?.signal,
        });
        const taskMap = new Map((this.projects || []).map(task => [task.id, task]));
        const projectMap = new Map((this.projectGroups || []).map(project => [project.id, project]));
        let items = page.items.map(item => {
          const task = taskMap.get(item.task_id) || { id: item.task_id, project_id: '' };
          const parentProjectId = task.parent_project_id || task.project_id || '';
          return mapVulnerability(item, task, projectMap.get(parentProjectId));
        });
        const routeTaskId = String(this.vulnerabilityRouteTaskId || '');
        const routeVulnerabilityId = String(this.vulnerabilityRouteId || '');
        let routeLookupFailed = false;
        let routeTarget = routeTaskId && routeVulnerabilityId
          ? items.find(item => item.task_id === routeTaskId && item.id === routeVulnerabilityId)
          : null;
        if (routeTaskId && routeVulnerabilityId && !routeTarget) {
          try {
            const raw = await request(
              'GET',
              `/tasks/${routeTaskId}/vulnerabilities/${routeVulnerabilityId}`,
              null,
              { signal: this._pageAbortController?.signal },
            );
            if (raw?.archived) throw new Error('漏洞已归档');
            const task = taskMap.get(routeTaskId) || { id: routeTaskId, project_id: '' };
            const parentProjectId = task.parent_project_id || task.project_id || '';
            routeTarget = mapVulnerability(raw, task, projectMap.get(parentProjectId));
            items = [routeTarget, ...items.filter(item => this.vulnerabilityKey(item) !== this.vulnerabilityKey(routeTarget))];
          } catch (error) {
            if (error?.name === 'AbortError') return;
            routeLookupFailed = true;
            this.vulnerabilitySelectionBlocked = true;
            this.selectedVulnerabilityKey = '';
            this.clearVulnerabilityRouteTarget(routeTaskId);
            this.showToast(error?.message === '漏洞已归档' ? '漏洞已归档，无法在当前列表中打开' : '漏洞不存在或无法打开', 'error');
          }
        }
        if (requestId !== this._vulnerabilityListRequestId) return;
        this.vulnerabilityItems = items;
        this.vulnerabilityTotal = page.total;
        if (routeTarget) {
          this.vulnerabilitySelectionBlocked = false;
          this.selectedVulnerabilityKey = this.vulnerabilityKey(routeTarget);
          await this.loadVulnerabilityReviews(routeTarget);
        } else if (!routeLookupFailed && options.selectFirst !== false && this.vulnerabilityItems.length) {
          this.vulnerabilitySelectionBlocked = false;
          this.selectedVulnerabilityKey = this.vulnerabilityKey(this.vulnerabilityItems[0]);
          await this.loadVulnerabilityReviews(this.vulnerabilityItems[0]);
        } else if (!this.vulnerabilityItems.some(
          item => this.vulnerabilityKey(item) === this.selectedVulnerabilityKey
        )) {
          this.vulnerabilityReviews = [];
        }
      } catch (error) {
        if (error?.name !== 'AbortError') this.showToast(error.message || String(error), 'error');
      } finally {
        if (requestId === this._vulnerabilityListRequestId) this.vulnerabilityLoading = false;
      }
    },
    scheduleVulnerabilitySearch() {
      clearTimeout(this._vulnerabilitySearchTimer);
      this._vulnerabilitySearchTimer = setTimeout(() => {
        this.vulnerabilityPage = 0;
        this.loadVulnerabilityPage();
      }, 250);
    },
    async applyVulnerabilityFilters() {
      this.vulnerabilityPage = 0;
      await this.loadVulnerabilityPage();
    },
    vulnerabilityPageCount() { return Math.max(1, Math.ceil((this.vulnerabilityTotal || 0) / this.vulnerabilityPageSize)); },
    async setVulnerabilityPage(page) {
      const next = Math.max(0, Math.min(this.vulnerabilityPageCount() - 1, Number(page) || 0));
      if (next === this.vulnerabilityPage) return;
      this.vulnerabilityPage = next;
      await this.loadVulnerabilityPage();
    },
    async refreshTaskDetailSections(events = [], force = false) {
      const taskId = this.selectedProjectId;
      if (!taskId || !this.project) return;
      const signal = this._pageAbortController?.signal;
      const task = await request('GET', `/tasks/${taskId}`, null, { signal });
      this.project.project = mapTask(task);
      if (task.archived) {
        this.project.facts = [];
        this.project.intents = [];
        this.project.goal_status = 'active';
        this.project.goal_completed_at = null;
        this.project.goal_completion_reason = '';
        this.project.goal_completion_evaluator = '';
        this.project.goal_completion_evaluation_id = null;
        this.project.goal_completion_reason_run_id = null;
        this.project.goal_from_fact_ids = [];
        this.project.goal_history = [];
        this.project.goal_cycles = [];
        this.project.goal_evaluations = [];
        this.project.todos = [];
        this.project.chains = [];
        this.project.red_team_results = [];
        this.project.vulns = [];
        this.project.artifacts = [];
        this.project.lead_artifacts = [];
        this.project.archived_locked = true;
        return;
      }
      this.project.archived_locked = false;
      const eventTypes = new Set((events || []).map(event => event.event_type || event.event || ''));
      const hasPrefix = (...prefixes) => [...eventTypes].some(type => prefixes.some(prefix => type.startsWith(prefix)));
      const intentChanged = hasPrefix('intent.', 'reason.', 'reason_workspace.', 'reason_run.', 'archive.', 'task.continued');
      const resultChanged = hasPrefix('intent.completed', 'reason.committed', 'reason_workspace.committed', 'archive.');

      const updates = [];
      if (force || intentChanged) {
        updates.push(request('GET', `/tasks/${taskId}/graph`, null, { signal }).then(graph => {
          this.project.facts = graph.facts || [];
          this.project.intents = (graph.intents || []).map(mapIntent);
          this.project.goal_status = graph.goal_status || 'active';
          this.project.goal_completed_at = graph.goal_completed_at || null;
          this.project.goal_completion_reason = graph.goal_completion_reason || '';
          this.project.goal_completion_evaluator = graph.goal_completion_evaluator || '';
          this.project.goal_completion_evaluation_id = graph.goal_completion_evaluation_id || null;
          this.project.goal_completion_reason_run_id = graph.goal_completion_reason_run_id || null;
          this.project.goal_from_fact_ids = graph.goal_from_fact_ids || [];
          this.project.goal_history = graph.goal_history || [];
        }));
        updates.push(request('GET', `/tasks/${taskId}/goal-cycles`, null, { signal }).then(items => { this.project.goal_cycles = items || []; }));
        updates.push(request('GET', `/tasks/${taskId}/goal-evaluations`, null, { signal }).then(items => { this.project.goal_evaluations = items || []; }));
      }
      if (force || resultChanged) {
        updates.push(request('GET', `/tasks/${taskId}/todos?limit=200`, null, { signal }).then(items => { this.project.todos = items || []; }));
        if (this.project?.project?.engagement_mode === 'red_team') {
          updates.push(request('GET', `/tasks/${taskId}/red-team/chains`, null, { signal }).then(items => { this.project.chains = items || []; }));
          updates.push(request('GET', `/tasks/${taskId}/red-team/results`, null, { signal }).then(items => { this.project.red_team_results = items || []; }));
        }
        updates.push(request('GET', `/tasks/${taskId}/vulnerabilities?limit=200`, null, { signal }).then(items => {
          const task = this.project.project;
          const group = (this.projectGroups || []).find(item => item.id === task.parent_project_id);
          this.project.vulns = (items || []).map(item => mapVulnerability(item, { id: task.id, project_id: task.parent_project_id, title: task.title }, group));
        }));
        updates.push(request('GET', `/tasks/${taskId}/artifacts?limit=12`, null, { signal }).then(items => { this.project.artifacts = (items || []).map(mapArtifact); }));
        updates.push(Promise.all([
          request('GET', `/tasks/${taskId}/artifacts?artifact_type=explore.leads&limit=20`, null, { signal }),
          request('GET', `/tasks/${taskId}/artifacts?artifact_type=bootstrap.initial_leads&limit=20`, null, { signal }),
        ]).then(([exploreLeads, bootstrapLeads]) => {
          this.project.lead_artifacts = [...(exploreLeads || []), ...(bootstrapLeads || [])].map(mapArtifact);
        }));
      }
      try {
        await Promise.all(updates);
      } catch (error) {
        if (error?.name !== 'AbortError') console.debug('Task detail incremental refresh failed', error);
      }
    },
    filteredTasks() {
      const keyword = String(this.taskSearch || '').trim().toLowerCase();
      const status = this.taskStatusFilter || 'all';
      return (this.projects || []).filter(task => {
        if (status === 'archived') {
          if (!task.archived) return false;
        } else {
          if (task.archived) return false;
          if (status !== 'all' && task.status !== status) return false;
        }
        if (!keyword) return true;
        return [task.id, task.title, task.origin, task.goal, this.projectGroupTitle(task.parent_project_id)]
          .some(value => String(value || '').toLowerCase().includes(keyword));
      }).sort((a, b) => this.taskHealthRank(a) - this.taskHealthRank(b));
    },
    taskHealthRank(task) {
      if (task.archived) return 4;
      if (task.status === 'active') return (task.working_intent_count || 0) > 0 ? 0 : 1;
      if (task.status === 'stopped') return 2;
      return 3;
    },
    filteredProjectGroups() {
      const keyword = String(this.projectSearch || '').trim().toLowerCase();
      const status = this.projectStatusFilter || 'all';
      return (this.projectGroups || []).filter(project => {
        if (status !== 'all' && project.status !== status) return false;
        if (!keyword) return true;
        return [project.id, project.title, project.description]
          .some(value => String(value || '').toLowerCase().includes(keyword));
      });
    },
    selectedTaskSummary() {
      const tasks = this.filteredTasks();
      if (this.taskManagementSelectionBlocked) return null;
      if (!tasks.length) return null;
      let selected = tasks.find(task => task.id === this.selectedTaskManagementId);
      if (!selected) {
        selected = tasks[0];
        this.selectedTaskManagementId = selected.id;
      }
      if (this.taskWorkerPoolEditor.projectId !== selected.id) this.syncTaskWorkerPoolEditor(selected);
      if (this.taskScopeEditor.taskId !== selected.id) this.syncTaskScopeEditor(selected);
      if (!this.taskDiagnostics[selected.id] && !this.taskDiagnosticsLoading[selected.id]) {
        this.scheduleTaskDiagnostics?.(selected.id);
      }
      return selected;
    },
    async saveSelectedProjectGroup() {
      const project = this.selectedProjectGroup();
      if (!project) return;
      try {
        await request('PATCH', `/projects/${project.id}`, {
          title: this.projectGroupSettingsForm.title || project.title,
          description: this.projectGroupSettingsForm.description ?? project.description,
        });
        await this.loadProjectGroups();
        this.showToast('项目资料已保存');
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    async toggleSelectedProjectArchive() {
      const project = this.selectedProjectGroup();
      if (!project) return;
      try {
        await request('PATCH', `/projects/${project.id}`, { status: project.status === 'active' ? 'archived' : 'active' });
        await this.loadProjectGroups();
        this.showToast(project.status === 'active' ? '项目已归档' : '项目已恢复');
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    selectManagedProject(projectId) {
      this.selectedProjectManagementId = projectId;
      const project = this.projectGroups.find(item => item.id === projectId);
      this.projectGroupSettingsForm = {
        title: project?.title || '', description: project?.description || '', status: project?.status || 'active', notes: '',
      };
      this.reloadBlackboardGroup(projectId);
      const firstTask = this.tasksForGroup(projectId)[0];
      this.selectedProjectManagementTaskId = firstTask?.id || '';
      if (this.selectedProjectManagementTaskId) this.loadProjectDetailForManagement(this.selectedProjectManagementTaskId);
      this.loadProjectCredentials(projectId);
    },
    syncTaskScopeEditor(task) {
      const scope = task?.task_scope || {};
      this.taskScopeEditor = {
        taskId: task?.id || '',
        targetsText: (scope.targets || []).join('\n'),
        excludeText: (scope.exclude || []).join('\n'),
        notes: scope.notes || '',
        saving: false,
      };
    },
    taskScopeChanged(task) {
      if (!task || this.taskScopeEditor.taskId !== task.id) return false;
      const lines = value => splitLines(value);
      const scope = task.task_scope || {};
      return JSON.stringify(lines(this.taskScopeEditor.targetsText)) !== JSON.stringify(scope.targets || [])
        || JSON.stringify(lines(this.taskScopeEditor.excludeText)) !== JSON.stringify(scope.exclude || [])
        || String(this.taskScopeEditor.notes || '').trim() !== String(scope.notes || '').trim();
    },
    async saveTaskScope() {
      const task = this.selectedTaskSummary();
      if (!task || task.legacy_readonly || task.status === 'completed') return;
      try {
        this.taskScopeEditor.saving = true;
        const targets = splitLines(this.taskScopeEditor.targetsText);
        const exclude = splitLines(this.taskScopeEditor.excludeText);
        const updated = await request('PATCH', `/tasks/${task.id}`, { scope: {
          enabled: targets.length > 0 || exclude.length > 0,
          targets,
          exclude,
        }});
        const mapped = mapTask(updated);
        const index = this.projects.findIndex(item => item.id === task.id);
        if (index >= 0) this.projects.splice(index, 1, mapped);
        this.syncTaskScopeEditor(mapped);
        this.showToast('任务 Scope 已保存');
      } catch (error) { this.showToast(error.message, 'error'); }
      finally { this.taskScopeEditor.saving = false; }
    },
    async loadProjectCredentials(projectId = '') {
      const id = projectId || this.selectedProjectManagementId;
      if (!id) { this.projectCredentials = []; return; }
      try {
        this.projectCredentials = (await request('GET', `/projects/${id}/credentials`)).map(mapCredential);
      } catch (error) {
        this.projectCredentials = [];
        this.showToast(error.message, 'error');
      }
    },
    filteredProjectCredentials() {
      const keyword = String(this.projectCredentialSearch || '').trim().toLowerCase();
      if (!keyword) return this.projectCredentials || [];
      return (this.projectCredentials || []).filter(item => [item.name, item.notes]
        .some(value => String(value || '').toLowerCase().includes(keyword)));
    },
    async openProjectCredentialEditor(credential = null) {
      const projectId = credential?.project_id || this.selectedProjectManagementId;
      this.credentialEditor = {
        open: true,
        loading: !!credential,
        id: credential?.id || '',
        projectId,
        name: credential?.name || '',
        content: '',
        notes: credential?.notes || '',
        refreshContent: '',
        enabled: credential ? !!credential.enabled : true,
      };
      if (!credential) return;
      try {
        const detail = await request('GET', `/projects/${projectId}/credentials/${credential.id}`);
        let refreshContent = '';
        if (detail.refresh_id) {
          const refresh = await request('GET', `/projects/${projectId}/credential-refreshes/${detail.refresh_id}`);
          refreshContent = refresh.content || '';
        }
        if (!this.credentialEditor.open || this.credentialEditor.id !== credential.id) return;
        this.credentialEditor = {
          open: true,
          loading: false,
          id: detail.id,
          projectId,
          name: detail.name || '',
          content: detail.content || '',
          notes: detail.notes || '',
          refreshContent,
          enabled: !!detail.enabled,
        };
      } catch (error) {
        if (this.credentialEditor.id === credential.id) this.credentialEditor.open = false;
        this.showToast(error.message, 'error');
      }
    },
    async saveProjectCredential() {
      const editor = this.credentialEditor;
      if (!editor.projectId || !editor.name.trim() || !String(editor.content || '').trim()) return;
      try {
        this.credentialSaving = true;
        const payload = {
          name: editor.name.trim(),
          content: String(editor.content || ''),
          notes: String(editor.notes || '').trim() || null,
          refresh_content: String(editor.refreshContent || '').trim()
            ? String(editor.refreshContent)
            : null,
          enabled: !!editor.enabled,
        };
        if (editor.id) {
          await request('PUT', `/projects/${editor.projectId}/credentials/${editor.id}`, payload);
        } else {
          await request('POST', `/projects/${editor.projectId}/credentials`, payload);
        }
        this.credentialEditor.open = false;
        await this.loadProjectCredentials(editor.projectId);
        this.showToast(editor.id ? '凭证已更新' : '凭证已添加');
      } catch (error) { this.showToast(error.message, 'error'); }
      finally { this.credentialSaving = false; }
    },
    async toggleProjectCredential(credential) {
      try {
        await request('PATCH', `/projects/${credential.project_id}/credentials/${credential.id}/enabled`, { enabled: !credential.enabled });
        await this.loadProjectCredentials(credential.project_id);
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    async deleteProjectCredential(credential) {
      if (!credential || !confirm(`删除凭证 ${credential.label}？`)) return;
      try {
        await request('DELETE', `/projects/${credential.project_id}/credentials/${credential.id}`);
        await this.loadProjectCredentials(credential.project_id);
        this.showToast('凭证已删除');
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    async updateTodoStatus(todoId, status, reason = '') {
      if (!this.selectedProjectId) return;
      try {
        let statusReason = String(reason || '').trim();
        if (['partial', 'blocked', 'completed'].includes(status) && !statusReason) {
          statusReason = String(prompt('请填写状态说明', '') || '').trim();
          if (!statusReason) return;
        }
        await request('PATCH', `/tasks/${this.selectedProjectId}/todos/${todoId}`, { status, actor: this.actorName(), status_reason: statusReason });
        await this.loadProject(this.selectedProjectId);
        this.sideTab = 'todo';
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    canActOnSelectedFacts() { return false; },
  });

  Object.assign(M.graph.methods, {
    intentRetryCount(intentOrId) {
      if (!intentOrId) return 0;
      const intentId = typeof intentOrId === 'string' ? intentOrId : intentOrId.id;
      const intent = typeof intentOrId === 'string'
        ? (this.project?.intents || []).find(item => item.id === intentId)
        : intentOrId;
      return Number(intent?.retry_count || 0);
    },
  });

  Object.assign(M.files.methods, {
    applyArtifactRouteParams(params) {
      const nextTask = String(params?.get('task') || '');
      const nextFolder = nextTask ? String(params?.get('folder') || '') : '';
      const nextArtifact = nextFolder ? String(params?.get('artifact') || '') : '';
      const locationChanged = nextTask !== this.artifactCenterSelectedTask
        || nextFolder !== this.artifactCenterFolderKey;
      this.artifactCenterSelectedTask = nextTask;
      this.artifactCenterFolderKey = nextFolder;
      this.artifactCenterRouteArtifactId = nextArtifact;
      if (locationChanged) {
        this.artifactCenterSearch = '';
        this.artifactCenterType = 'all';
        this.artifactCenterPhase = 'all';
        this.artifactCenterSource = 'all';
        this.artifactCenterPage = 0;
        this.artifactCenterFolderPage = 0;
        this.artifactCenterSelected = null;
      } else if (!nextArtifact) {
        this.artifactCenterSelected = null;
      }
    },
    artifactCenterHash(taskId = '', folderKey = '', artifactId = '') {
      const params = new URLSearchParams();
      if (taskId) params.set('task', taskId);
      if (taskId && folderKey) params.set('folder', folderKey);
      if (taskId && folderKey && artifactId) params.set('artifact', artifactId);
      const query = params.toString();
      return `#/files${query ? `?${query}` : ''}`;
    },
    navigateArtifactCenter(taskId = '', folderKey = '', artifactId = '') {
      const target = this.artifactCenterHash(taskId, folderKey, artifactId);
      if (location.hash === target) {
        this.applyArtifactRouteParams(new URLSearchParams(target.split('?')[1] || ''));
        this.loadArtifactCenter();
      } else {
        location.hash = target;
      }
    },
    artifactCenterMode() {
      if (!this.artifactCenterSelectedTask) return 'global';
      if (!this.artifactCenterFolderKey) return 'task-root';
      return 'folder';
    },
    artifactCenterCurrentTask() {
      return (this.artifactCenterTasks || []).find(task => task.id === this.artifactCenterSelectedTask) || null;
    },
    async toggleExplorerTask(taskId) {
      this.artifactCenterSelectedTask = taskId;
      if (this.explorerExpanded[taskId]) {
        delete this.explorerExpanded[taskId];
        return;
      }
      this.explorerExpanded[taskId] = true;
      if (!this.explorerFolders[taskId]) await this.loadExplorerFolders(taskId);
    },
    async loadExplorerFolders(taskId, page = 0) {
      try {
        const params = new URLSearchParams({ limit: '50', offset: String(page * 50) });
        const resp = await requestPage(`/tasks/${taskId}/artifact-folders?${params}`, { signal: this._pageAbortController?.signal });
        const prev = page > 0 ? (this.explorerFolders[taskId] || []) : [];
        this.explorerFolders = { ...this.explorerFolders, [taskId]: [...prev, ...resp.items] };
        this.explorerFolderMeta = { ...this.explorerFolderMeta, [taskId]: { page, total: resp.total, hasMore: (page + 1) * 50 < resp.total } };
      } catch (error) { if (error?.name !== 'AbortError') this.showToast(error.message, 'error'); }
    },
    explorerFolderHasMore(taskId) {
      const meta = (this.explorerFolderMeta || {})[taskId];
      return meta ? meta.hasMore : false;
    },
    async loadMoreExplorerFolders(taskId) {
      const meta = (this.explorerFolderMeta || {})[taskId];
      if (meta && meta.hasMore) await this.loadExplorerFolders(taskId, meta.page + 1);
    },
    async toggleExplorerIntent(taskId, folderKey) {
      const key = `${taskId}:${folderKey}`;
      this.artifactCenterSelectedTask = taskId;
      this.artifactCenterFolderKey = folderKey;
      if (this.explorerExpanded[key]) {
        delete this.explorerExpanded[key];
        return;
      }
      this.explorerExpanded[key] = true;
      if (!this.explorerFiles[key]) await this.loadExplorerFiles(taskId, folderKey);
    },
    async loadExplorerFiles(taskId, folderKey) {
      const key = `${taskId}:${folderKey}`;
      try {
        const params = new URLSearchParams({ limit: '200' });
        if (folderKey === 'task') params.set('unscoped_only', 'true');
        else params.set('intent_id', folderKey);
        const page = await requestPage(`/tasks/${taskId}/artifacts?${params}`, { signal: this._pageAbortController?.signal });
        const task = (this.artifactCenterTasks || []).find(t => t.id === taskId);
        const items = page.items.map(item => ({
          ...mapArtifact(item),
          task_title: task?.title || item.task_id,
          phase: this.artifactSourcePhase(item),
          task_id: taskId,
        })).filter(item => !item.archived);
        this.explorerFiles = { ...this.explorerFiles, [key]: items };
      } catch (error) { if (error?.name !== 'AbortError') this.showToast(error.message, 'error'); }
    },
    explorerNodeVisible(kind, key, text) {
      const q = String(this.explorerFilter || '').trim().toLowerCase();
      if (!q) return true;
      return String(text || '').toLowerCase().includes(q);
    },
    async openEditorTab(item) {
      if (!item?.id) return;
      const taskId = item.task_id || this.artifactCenterSelectedTask;
      const folderKey = item.intent_id || item.review_run_id || this.artifactCenterFolderKey || 'task';
      // 同步树展开路径
      this.explorerExpanded[taskId] = true;
      if (!this.explorerFolders[taskId]) await this.loadExplorerFolders(taskId);
      this.explorerExpanded[`${taskId}:${folderKey}`] = true;
      if (!this.explorerFiles[`${taskId}:${folderKey}`]) await this.loadExplorerFiles(taskId, folderKey);
      this.navigateArtifactCenter(taskId, folderKey, item.id);
      if (!(this.editorTabs || []).some(tab => tab.id === item.id)) {
        this.editorTabs = [...(this.editorTabs || []), item];
      }
      this.editorActiveId = item.id;
      const cached = (this.editorTabs || []).find(tab => tab.id === item.id);
      if (cached && cached._loaded) {
        this.artifactCenterSelected = cached;
        return;
      }
      await this.openArtifactCenter(item);
      const loaded = this.artifactCenterSelected;
      if (loaded && loaded.id === item.id) {
        loaded._loaded = true;
        this.editorTabs = (this.editorTabs || []).map(tab => tab.id === item.id ? loaded : tab);
      }
    },
    activateEditorTab(id) {
      const tab = (this.editorTabs || []).find(t => t.id === id);
      if (!tab) return;
      this.editorActiveId = id;
      if (tab._loaded) {
        this.artifactCenterSelected = tab;
        return;
      }
      this.openEditorTab(tab);
    },
    closeEditorTab(id) {
      const tabs = (this.editorTabs || []).filter(tab => tab.id !== id);
      this.editorTabs = tabs;
      if (this.editorActiveId !== id) return;
      const last = tabs[tabs.length - 1] || null;
      if (last) this.activateEditorTab(last.id);
      else { this.editorActiveId = ''; this.artifactCenterSelected = null; }
    },
    openArtifactFolder(folder) {
      if (!this.artifactCenterSelectedTask || !folder?.folder_key) return;
      this.navigateArtifactCenter(this.artifactCenterSelectedTask, folder.folder_key);
    },
    backArtifactCenterTask() {
      if (!this.artifactCenterSelectedTask) return;
      this.navigateArtifactCenter(this.artifactCenterSelectedTask);
    },
    closeArtifactCenterPreview() {
      if (!this.artifactCenterSelectedTask || !this.artifactCenterFolderKey) {
        this.artifactCenterSelected = null;
        return;
      }
      this.navigateArtifactCenter(this.artifactCenterSelectedTask, this.artifactCenterFolderKey);
    },
    async loadArtifactCenter() {
      const requestId = ++this._artifactCenterRequestId;
      this.artifactCenterLoading = true;
      try {
        const signal = this._pageAbortController?.signal;
        const tasks = (await request('GET', '/tasks', null, { signal }))
          .map(mapTask)
          .filter(task => !task.archived);
        const taskMap = new Map(tasks.map(task => [task.id, task]));
        if (requestId !== this._artifactCenterRequestId) return;
        this.artifactCenterTasks = tasks;
        if (this.artifactCenterSelectedTask && !taskMap.has(this.artifactCenterSelectedTask)) {
          this.showToast('任务不存在或已被删除', 'error');
          this.navigateArtifactCenter();
          return;
        }

        const mode = this.artifactCenterMode();
        if (mode === 'task-root') {
          const params = new URLSearchParams({
            limit: String(this.artifactCenterFolderPageSize),
            offset: String(this.artifactCenterFolderPage * this.artifactCenterFolderPageSize),
          });
          const search = String(this.artifactCenterSearch || '').trim();
          if (search) params.set('search', search);
          const page = await requestPage(
            `/tasks/${this.artifactCenterSelectedTask}/artifact-folders?${params}`,
            { signal },
          );
          if (requestId !== this._artifactCenterRequestId) return;
          this.artifactCenterFolders = page.items;
          this.artifactCenterFolderTotal = page.total;
          this.artifactCenterItems = [];
          this.artifactCenterTotal = 0;
          this.artifactCenterSelected = null;
          return;
        }

        const params = new URLSearchParams({
          limit: String(mode === 'folder' ? 50 : this.artifactCenterPageSize),
          offset: String(this.artifactCenterPage * (mode === 'folder' ? 50 : this.artifactCenterPageSize)),
        });
        const search = String(this.artifactCenterSearch || '').trim();
        if (search) params.set('search', search);
        if (this.artifactCenterType !== 'all') params.set('artifact_type', this.artifactCenterType);
        if (this.artifactCenterPhase !== 'all') params.set('phase', this.artifactCenterPhase);
        if (this.artifactCenterSource !== 'all') params.set('source', this.artifactCenterSource);
        let endpoint = '/artifacts';
        if (mode === 'folder') {
          endpoint = `/tasks/${this.artifactCenterSelectedTask}/artifacts`;
          if (this.artifactCenterFolderKey === 'task') params.set('unscoped_only', 'true');
          else params.set('intent_id', this.artifactCenterFolderKey);
        }
        const page = await requestPage(`${endpoint}?${params}`, { signal });
        const items = page.items.map(item => {
          const mapped = mapArtifact(item);
          const task = taskMap.get(item.task_id);
          return {
            ...mapped,
            task_title: task?.title || item.task_id,
            phase: this.artifactSourcePhase(mapped),
            task_id: item.task_id,
          };
        });
        if (requestId !== this._artifactCenterRequestId) return;
        this.artifactCenterFolders = [];
        this.artifactCenterItems = items;
        this.artifactCenterTotal = page.total;
        await this.restoreArtifactCenterSelection(requestId);
      } catch (error) { if (error?.name !== 'AbortError') this.showToast(error.message, 'error'); }
      finally {
        if (requestId === this._artifactCenterRequestId) this.artifactCenterLoading = false;
      }
    },
    async restoreArtifactCenterSelection(requestId) {
      const artifactId = this.artifactCenterRouteArtifactId;
      if (!artifactId || !this.artifactCenterSelectedTask || !this.artifactCenterFolderKey) return;
      if (this.artifactCenterSelected?.id === artifactId) return;
      let target = (this.artifactCenterItems || []).find(item => item.id === artifactId);
      if (!target) {
        try {
          const raw = await request(
            'GET',
            `/tasks/${this.artifactCenterSelectedTask}/artifacts/${artifactId}`,
            null,
            { signal: this._pageAbortController?.signal },
          );
          if (requestId !== this._artifactCenterRequestId) return;
          target = mapArtifact(raw);
          const realFolder = target.intent_id || target.review_run_id || 'task';
          if (realFolder !== this.artifactCenterFolderKey) {
            this.navigateArtifactCenter(this.artifactCenterSelectedTask, realFolder, target.id);
            return;
          }
          const task = this.artifactCenterCurrentTask();
          target = {
            ...target,
            task_title: task?.title || target.task_id,
            phase: this.artifactSourcePhase(target),
          };
          this.artifactCenterItems = [
            target,
            ...(this.artifactCenterItems || []).filter(item => item.id !== target.id),
          ];
        } catch (error) {
          if (error?.name === 'AbortError') return;
          this.showToast('产物不存在或已归档', 'error');
          this.navigateArtifactCenter(this.artifactCenterSelectedTask, this.artifactCenterFolderKey);
          return;
        }
      }
      await this.openEditorTab(target);
      this.$nextTick(() => {
        const selector = `[data-artifact-id="${CSS.escape(String(target.id))}"]`;
        document.querySelector(selector)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      });
    },
    scheduleArtifactCenterSearch() {
      clearTimeout(this._artifactSearchTimer);
      this._artifactSearchTimer = setTimeout(() => {
        if (this.artifactCenterMode() === 'task-root') this.artifactCenterFolderPage = 0;
        else this.artifactCenterPage = 0;
        this.loadArtifactCenter();
      }, 250);
    },
    artifactCenterEffectivePageSize() { return this.artifactCenterMode() === 'folder' ? 50 : this.artifactCenterPageSize; },
    artifactCenterPageCount() { return Math.max(1, Math.ceil((this.artifactCenterTotal || 0) / this.artifactCenterEffectivePageSize())); },
    async setArtifactCenterPage(page) {
      const next = Math.max(0, Math.min(this.artifactCenterPageCount() - 1, Number(page) || 0));
      if (next === this.artifactCenterPage) return;
      this.artifactCenterPage = next;
      await this.loadArtifactCenter();
    },
    artifactCenterFolderPageCount() { return Math.max(1, Math.ceil((this.artifactCenterFolderTotal || 0) / this.artifactCenterFolderPageSize)); },
    async setArtifactCenterFolderPage(page) {
      const next = Math.max(0, Math.min(this.artifactCenterFolderPageCount() - 1, Number(page) || 0));
      if (next === this.artifactCenterFolderPage) return;
      this.artifactCenterFolderPage = next;
      await this.loadArtifactCenter();
    },
    artifactCenterTypes() { return [...new Set((this.artifactCenterItems || []).map(item => item.artifact_type).filter(Boolean))].sort(); },
    artifactCenterPhases() { return [...new Set((this.artifactCenterItems || []).map(item => item.phase).filter(Boolean))].sort(); },
    artifactSourcePhase(item) {
      const type = String(item?.artifact_type || '').toLowerCase();
      const typedPhase = ['bootstrap', 'reason', 'explore', 'plan', 'review', 'asm'].find(phase => type.startsWith(`${phase}.`));
      if (typedPhase) return typedPhase;
      const match = String(item?.path || '').match(/(?:^|\/)(bootstrap|reason|explore|plan|review|asm)(?:[-_\/]|$)/i);
      return match ? match[1].toLowerCase() : (type === 'upload' ? 'upload' : 'unknown');
    },
    artifactCenterFiltered() {
      return (this.artifactCenterItems || []).filter(item => {
        return !item.archived;
      });
    },
    artifactFolderLabel(folder) { return folder?.folder_key === 'task' ? '任务文件' : (folder?.folder_key || '未知目录'); },
    artifactFolderDescription(folder) {
      if (folder?.folder_key === 'task') return '人工上传及未关联 Intent 的任务级产物';
      if (folder?.review_run_id) return '漏洞 Reviewer 审核记录与最小复测证据';
      return folder?.intent_description || '该 Intent 的正式产物';
    },
    artifactFolderPhase(folder) {
      if (folder?.folder_key === 'task') return 'upload';
      if (folder?.review_run_id) return 'review';
      return folder?.intent_kind || 'intent';
    },
    artifactFileName(item) { return String(item?.path || '').split('/').pop() || item?.id || 'artifact'; },
    artifactDisplayPath(item) {
      const path = String(item?.path || '');
      if (this.artifactCenterFolderKey && this.artifactCenterFolderKey !== 'task') {
        const prefix = `${this.artifactCenterFolderKey}/`;
        return path.startsWith(prefix) ? path.slice(prefix.length) : path;
      }
      if (this.artifactCenterFolderKey === 'task') return this.artifactFileName(item);
      return path;
    },
    isArtifactCenterSelected(item) { return !!item?.id && this.artifactCenterSelected?.id === item.id; },
    selectArtifactCenterItem(item) {
      if (!item?.id) return;
      const taskId = item.task_id || this.artifactCenterSelectedTask;
      const folderKey = item.intent_id || item.review_run_id || (this.artifactCenterFolderKey || 'task');
      this.navigateArtifactCenter(taskId, folderKey, item.id);
    },
    async openArtifactLocation(taskId, artifactOrId) {
      if (!taskId || !artifactOrId) return;
      try {
        const item = typeof artifactOrId === 'string'
          ? await request('GET', `/tasks/${taskId}/artifacts/${artifactOrId}`)
          : artifactOrId;
        if (!item?.id) throw new Error('Artifact metadata is unavailable');
        if (item.archived) throw new Error('产物已归档，无法在当前文件中心打开');
        this.navigateArtifactCenter(taskId, item.intent_id || item.review_run_id || 'task', item.id);
      } catch (error) {
        this.showToast(error.message || '无法打开产物', 'error');
      }
    },
    artifactTypeIcon(type) { return type === 'upload' ? '↑' : /finding|vuln/i.test(type || '') ? '!' : /lead/i.test(type || '') ? '↗' : /evidence/i.test(type || '') ? '◈' : /api|asset|inventory/i.test(type || '') ? '◫' : '▤'; },
    openTabContextMenu(event, tabId) {
      const section = event.target.closest('section');
      const rect = (section || document.body).getBoundingClientRect();
      // 菜单 absolute 挂在 section(定位上下文),坐标按 section 原点计算并钳在其可视范围内
      const x = Math.max(0, Math.min(event.clientX - rect.left, rect.width - 170));
      const y = Math.max(0, Math.min(event.clientY - rect.top, rect.height - 130));
      this.tabContextMenu = { open: true, x, y, tabId: tabId || this.editorActiveId };
    },
    tabContextMenuClose(action) {
      const anchor = this.tabContextMenu.tabId;
      this.tabContextMenu.open = false;
      const tabs = this.editorTabs || [];
      if (action === 'all') {
        this.editorTabs = [];
        this.editorActiveId = '';
        this.artifactCenterSelected = null;
        return;
      }
      if (action === 'others') {
        const keep = tabs.filter(tab => tab.id === anchor);
        this.editorTabs = keep;
        if (keep.length) this.activateEditorTab(keep[0].id);
        else { this.editorActiveId = ''; this.artifactCenterSelected = null; }
        return;
      }
      if (action === 'right') {
        const index = tabs.findIndex(tab => tab.id === anchor);
        if (index < 0) return;
        const keep = tabs.slice(0, index + 1);
        this.editorTabs = keep;
        if (!keep.some(tab => tab.id === this.editorActiveId)) {
          if (keep.length) this.activateEditorTab(keep[keep.length - 1].id);
          else { this.editorActiveId = ''; this.artifactCenterSelected = null; }
        }
      }
    },
    artifactKindLabel(item) {
      const type = String(item?.artifact_type || '').toLowerCase();
      if (type === 'explore.findings') return 'Findings';
      if (type === 'explore.leads') return 'Leads';
      if (type === 'explore.evidence') return 'Evidence';
      if (type === 'review.report') return 'Review';
      if (type === 'review.evidence') return 'Review Evidence';
      return item?.artifact_type || 'Artifact';
    },
    artifactKindClass(item) {
      const type = String(item?.artifact_type || '').toLowerCase();
      if (type === 'explore.findings') return 'border-emerald-200 bg-emerald-50 text-emerald-700';
      if (type === 'explore.leads') return 'border-amber-200 bg-amber-50 text-amber-700';
      if (type === 'explore.evidence') return 'border-blue-200 bg-blue-50 text-blue-700';
      if (type === 'review.report') return 'border-violet-200 bg-violet-50 text-violet-700';
      if (type === 'review.evidence') return 'border-cyan-200 bg-cyan-50 text-cyan-700';
      return 'border-slate-200 bg-slate-100 text-slate-500';
    },
    formatBytes(size) { const n = Number(size || 0); if (n < 1024) return `${n} B`; if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`; return `${(n / 1024 / 1024).toFixed(1)} MB`; },
    artifactPreviewKind(name) {
      const filename = String(name || '').toLowerCase();
      if (this.isImageFile(filename)) return 'image';
      if (this.isMdFile(filename)) return 'markdown';
      if (/\.(json|har)$/.test(filename)) return 'markdown';
      if (/\.(txt|log|csv|tsv|jsonl|ndjson|xml|ya?ml|js|mjs|cjs|jsx|ts|tsx|py|sh|bash|zsh|fish|html?|css|scss|sql|ini|conf|cfg|env|toml|properties|http)$/.test(filename)) return 'markdown';
      return 'unsupported';
    },
    artifactPreviewUrl(item) { return `${apiRoot}/tasks/${item.task_id}/artifacts/${item.id}/content`; },
    artifactDownloadUrl(item) { return `${apiRoot}/tasks/${item.task_id}/artifacts/${item.id}/download`; },
    artifactContentUrl(item) { return this.artifactDownloadUrl(item); },
    async openArtifactCenter(item) {
      const name = this.artifactFileName(item);
      const previewKind = this.artifactPreviewKind(name);
      const previewRequestId = `${item.id}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
      const selected = {
        ...item,
        _previewRequestId: previewRequestId,
        _previewKind: previewKind,
        _isImage: previewKind === 'image',
        _isMarkdown: previewKind === 'markdown',
        _loading: false,
        _notice: '',
        _warning: '',
        _error: '',
        _content: '',
        _rendered: '',
      };
      this.artifactCenterSelected = selected;
      const currentSelection = () => {
        const current = this.artifactCenterSelected;
        return current?._previewRequestId === previewRequestId ? current : null;
      };
      if (previewKind === 'image') return;
      if (previewKind === 'unsupported') {
        currentSelection()._notice = '该文件类型不支持在线预览，请下载查看。';
        return;
      }
      if (Number(item.size || 0) > 5 * 1024 * 1024) {
        currentSelection()._notice = `文件过大（${this.formatBytes(item.size)}），在线预览上限为 5 MB，请下载查看。`;
        return;
      }
      currentSelection()._loading = true;
      try {
        const payload = await request('GET', `/tasks/${item.task_id}/artifacts/${item.id}/content`);
        const current = currentSelection();
        if (!current) return;
        const content = typeof payload?.content === 'string' ? payload.content : '';
        current._content = content;
        if (previewKind === 'markdown') {
          if (typeof marked === 'undefined') await this.ensureMarkdownRuntime?.();
          current._rendered = this.renderMarkdown(content);
        } else if (previewKind === 'json' && content.trim()) {
          try {
            current._content = JSON.stringify(JSON.parse(content), null, 2);
          } catch (_) {
            current._warning = 'JSON 格式无效，已按原文显示。';
          }
        }
      } catch (error) {
        const current = currentSelection();
        if (current) current._error = `读取失败：${error.message}`;
      } finally {
        const current = currentSelection();
        if (current) current._loading = false;
      }
    },
    async archiveArtifactCenter(item) {
      if (!confirm(`归档产物 ${this.artifactFileName(item)}？`)) return;
      try {
        await request('DELETE', `/tasks/${item.task_id}/artifacts/${item.id}`);
        this.closeEditorTab(item.id);
        this.showToast('产物已归档');
        const key = `${item.task_id}:${item.intent_id || item.review_run_id || 'task'}`;
        if (this.explorerFiles[key]) await this.loadExplorerFiles(item.task_id, item.intent_id || item.review_run_id || 'task');
        this.navigateArtifactCenter(item.task_id, item.intent_id || item.review_run_id || 'task');
      }
      catch (error) { this.showToast(error.message, 'error'); }
    },
    async uploadArtifactCenter(event) {
      const file = event.target.files[0]; if (!file) return;
      const taskId = this.artifactCenterSelectedTask;
      if (!taskId) { this.showToast('请先选择一个任务', 'error'); event.target.value = ''; return; }
      const form = new FormData(); form.append('file', file); form.append('actor', this.actorName());
      try {
        const created = await request('POST', `/tasks/${taskId}/artifacts/upload`, form);
        this.showToast('产物已上传到任务文件');
        delete this.explorerFolders[taskId];
        if (this.explorerExpanded[taskId]) await this.loadExplorerFolders(taskId);
        await this.openArtifactLocation(taskId, created);
      }
      catch (error) { this.showToast(error.message, 'error'); }
      event.target.value = '';
    },
    async loadFileTree() {
      if (!this.projectGroups.length) await this.loadProjectGroups();
      if (!this.projects.length) await this.loadProjects();
      this.fileGroups = this.projectGroups.map(group => ({
        id: group.id,
        title: group.title,
        hasCrawler: false,
        tasks: this.projects.filter(task => !task.archived && task.parent_project_id === group.id).map(task => ({
          id: task.id, title: task.title, folders: ['human', 'ai'],
        })),
      }));
    },
    async selectFolder(groupId, taskId, folder, isGroupLevel) {
      this.fileGroupId = groupId;
      this.fileTaskId = taskId;
      this.fileFolder = folder;
      this.fileIsGroupLevel = false;
      this.filePreview = { open: false, name: '', path: '', content: '', isMarkdown: false, isImage: false, rawUrl: '', rendered: '' };
      const artifacts = (await request('GET', `/tasks/${taskId}/artifacts?limit=200`)).map(mapArtifact);
      const filtered = artifacts.filter(item => folder === 'human' ? item.artifact_type === 'upload' : item.artifact_type !== 'upload');
      this.fileArtifactMap = {};
      this.fileList = filtered.map(item => {
        const name = item.path.split('/').pop();
        const path = item.path || `${item.id}/${name}`;
        this.fileArtifactMap[path] = item;
        return { path, name, size: item.size, artifact_id: item.id, created_at: item.created_at };
      });
    },
    async previewFile(path, size) {
      const artifact = this.fileArtifactMap[path];
      if (!artifact) return;
      const name = artifact.path.split('/').pop();
      const rawUrl = `${apiRoot}/tasks/${this.fileTaskId}/artifacts/${artifact.id}/download`;
      if (this.isImageFile(name)) {
        this.filePreview = { open: true, name, path, content: '', isMarkdown: false, isImage: true, rawUrl, rendered: '' };
        return;
      }
      if (size > 5 * 1024 * 1024) {
        this.filePreview = { open: true, name, path, content: `[文件过大 (${(size/1024/1024).toFixed(1)}MB)，不支持预览]`, isMarkdown: false, isImage: false, rawUrl, rendered: '' };
        return;
      }
      const response = await fetch(rawUrl);
      const content = await response.text();
      const isMarkdown = this.isMdFile(name);
      if (isMarkdown && typeof marked === 'undefined') await this.ensureMarkdownRuntime?.();
      this.filePreview = { open: true, name, path, content, isMarkdown, isImage: false, rawUrl, rendered: isMarkdown ? this.renderMarkdown(content) : '' };
    },
    async deleteCurrentFile(path) {
      const artifact = this.fileArtifactMap[path];
      if (!artifact || !confirm(`删除 ${path.split('/').pop()}？`)) return;
      await request('DELETE', `/tasks/${this.fileTaskId}/artifacts/${artifact.id}`);
      await this.selectFolder(this.fileGroupId, this.fileTaskId, this.fileFolder, false);
    },
    async uploadFileToTask(event) {
      const file = event.target.files[0];
      if (!file || !this.fileTaskId) return;
      const form = new FormData();
      form.append('file', file);
      form.append('actor', this.actorName());
      try {
        await request('POST', `/tasks/${this.fileTaskId}/artifacts/upload`, form);
        await this.selectFolder(this.fileGroupId, this.fileTaskId, 'human', false);
        this.showToast('文件已上传');
      } catch (error) { this.showToast(error.message, 'error'); }
      event.target.value = '';
    },
  });
})();
