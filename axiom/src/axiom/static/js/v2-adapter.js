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
  const runtimeConfigKeys = [
    'runner_interval_seconds',
    'global_max_active_runs',
    'global_max_active_tasks',
    'per_task_max_active_runs',
    'per_task_explore_concurrency',
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
    'reason_batch_size',
    'reason_low_watermark',
    'reason_max_wait_seconds',
    'reason_min_interval_seconds',
    'reason_target_open_intents',
    'reason_max_new_intents_per_run',
    'explore_execute_min_seconds',
    'explore_execute_max_seconds',
    'explore_conclude_min_seconds',
    'explore_conclude_max_seconds',
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

  async function request(method, path, body, options = {}) {
    const headers = { ...(options.headers || {}) };
    const init = { method, headers };
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

  function mapTask(task) {
    const activeReasonRun = task.active_reason_run || null;
    const legacyReason = !activeReasonRun && task.reason_worker ? {
      id: '',
      status: 'running',
      worker: task.reason_worker,
      trigger: 'legacy',
      last_heartbeat_at: task.reason_heartbeat_at,
      activity_kind: task.reason_activity_kind || 'analyzing',
      activity_summary: task.reason_activity_summary || '正在分析任务上下文并规划下一批 Intent',
      activity_tool: task.reason_activity_tool || '',
      activity_started_at: task.reason_activity_started_at || task.reason_heartbeat_at,
      activity_updated_at: task.reason_activity_updated_at || task.reason_heartbeat_at,
      created_at: task.reason_activity_started_at || task.reason_heartbeat_at,
    } : null;
    return {
      id: task.id,
      parent_project_id: task.project_id,
      title: task.title,
      origin: originText(task),
      goal: task.goal,
      status: taskStatus(task.status),
      phase: task.phase,
      bootstrap_enabled: true,
      bootstrap_status: task.phase === 'bootstrap' ? 'running' : (task.status === 'completed' ? 'completed' : ''),
      kk_report_enabled: !!task.external_report_enabled,
      guard_enabled: !!task.guard_enabled,
      worker_pool: task.worker_pool || [],
      task_scope: task.scope || { enabled: false, targets: [], exclude: [], notes: '' },
      legacy_readonly: !!task.legacy_readonly,
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
        activity_summary: activeReasonRun.activity_summary || (activeReasonRun.status === 'pending'
          ? '等待可用的 Reason Worker 领取'
          : '正在分析任务上下文并规划下一批 Intent'),
        activity_tool: activeReasonRun.activity_tool || '',
        activity_started_at: activeReasonRun.activity_started_at || activeReasonRun.created_at,
        activity_updated_at: activeReasonRun.activity_updated_at || activeReasonRun.last_heartbeat_at || activeReasonRun.created_at,
        created_at: activeReasonRun.created_at,
      } : legacyReason,
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
      todo_count: task.todo_count || 0,
      depth_mode: false,
    };
  }

  function mapVulnerability(item, task, project) {
    return {
      ...item,
      project_id: item.task_id,
      parent_project_id: task?.project_id || project?.id || '',
      task_title: task?.title || item.task_id,
      project_group_title: project?.title || '',
      recommendation: item.remediation || '',
      affected_scope: item.affected_target || '',
      finding_artifact: item.finding_artifact ? mapArtifact(item.finding_artifact) : null,
      evidence_artifacts: (item.evidence_artifacts || []).map(mapArtifact),
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
    return {
      id: intent.id,
      from: intent.from_facts || [],
      to: intent.to_fact_id || null,
      todo_id: intent.todo_id || null,
      description: intent.description,
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

  async function taskDetail(taskId) {
    const [task, graph, hints, todos, vulnerabilities, artifacts, audit] = await Promise.all([
      request('GET', `/tasks/${taskId}`),
      request('GET', `/tasks/${taskId}/graph`),
      request('GET', `/tasks/${taskId}/hints`),
      request('GET', `/tasks/${taskId}/todos`),
      request('GET', `/tasks/${taskId}/vulnerabilities`),
      request('GET', `/tasks/${taskId}/artifacts`),
      request('GET', `/audit-events?task_id=${encodeURIComponent(taskId)}&limit=500`),
    ]);
    const project = await request('GET', `/projects/${task.project_id}`).catch(() => null);
    const mappedTask = mapTask(task);
    mappedTask.fact_count = graph.facts.length;
    mappedTask.intent_count = graph.intents.length;
    mappedTask.vulnerability_count = vulnerabilities.length;
    return {
      project: mappedTask,
      facts: graph.facts,
      intents: graph.intents.map(mapIntent),
      goal_from_fact_ids: graph.goal_from_fact_ids || [],
      hints,
      todos,
      vulns: vulnerabilities.map(item => mapVulnerability(item, task, project)),
      artifacts: artifacts.map(mapArtifact),
      events: audit.map(mapAuditEvent),
    };
  }

  function workerEnv(worker) {
    return worker?.config?.environment || {};
  }

  function mapWorker(worker) {
    const env = workerEnv(worker);
    const config = worker?.config || {};
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
      pi_provider_api: config.provider_api || 'openai-completions',
      pi_model_context_window: String(config.context_window || '200000'),
      pi_api_key: '',
      pi_api_key_masked: config.api_key ? '••••••••' : '',
      pi_thinking: config.thinking || 'off',
      pi_thinking_capable: config.thinking_capable || 'unknown',
      pi_thinking_tested_at: config.thinking_tested_at || '',
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
    for (const key of runtimeConfigKeys) {
      if (body[key] !== undefined && body[key] !== null && body[key] !== '') {
        config[key] = Number(body[key]);
      }
    }
    if (body.network_proxy_mode !== undefined) config.network_proxy_mode = body.network_proxy_mode;
    if (body.network_proxy_url !== undefined) config.network_proxy_url = body.network_proxy_url;
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
      .filter(item => ['bootstrap', 'reason', 'explore'].includes(item));
    if (!capabilities.length) throw new Error(`Worker ${targetName} 至少要勾选一个参与阶段`);

    const existing = persistedName
      ? await request('GET', `/workers/${encodeURIComponent(persistedName)}`).catch(() => null)
      : null;
    const oldConfig = existing?.config || {};
    const nextConfig = {
      ...oldConfig,
      model: body.pi_model || '',
      base_url: body.pi_base_url || '',
      provider_api: body.pi_provider_api || 'openai-completions',
      context_window: Number(body.pi_model_context_window ?? 200000),
      thinking: body.pi_thinking || 'off',
      thinking_capable: body.pi_thinking_capable || 'unknown',
      thinking_tested_at: body.pi_thinking_tested_at || '',
      environment: { ...(oldConfig.environment || {}) },
    };
    delete nextConfig.env;
    if (body.pi_api_key) nextConfig.api_key = body.pi_api_key;
    await request('PUT', `/workers/${encodeURIComponent(targetName)}`, {
      worker_type: 'pi',
      capabilities,
      max_running: Number(body.max_running ?? 1),
      priority: Number(body.priority ?? 0),
      config: nextConfig,
      disabled: !!body.disabled,
    });
    if (persistedName && persistedName !== targetName) {
      await request('DELETE', `/workers/${encodeURIComponent(persistedName)}`);
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

  async function legacyApi(method, rawPath, body) {
    const url = new URL(rawPath, location.origin);
    const path = url.pathname;
    const query = url.searchParams;
    let match;

    if (path === '/project-groups' && method === 'GET') {
      const projects = await request('GET', '/projects');
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

    if (path === '/projects' && method === 'GET') return (await request('GET', `/tasks${url.search}`)).map(mapTask);
    if (path === '/projects' && method === 'POST') {
      const task = await request('POST', '/tasks', {
        project_id: body.parent_project_id,
        title: body.title,
        origin: body.origin,
        goal: body.goal,
        hints: (body.hints || []).map(item => item.content || item).filter(Boolean),
        guard_enabled: !!body.guard_enabled,
        external_report_enabled: !!body.kk_report_enabled,
        worker_pool: body.worker_pool || [],
        scope: body.task_scope || null,
        run_duration_seconds: body.run_duration_seconds ?? null,
      });
      return { project: mapTask(task) };
    }
    if ((match = path.match(/^\/projects\/([^/]+)$/))) {
      if (method === 'GET') return taskDetail(match[1]);
      if (method === 'DELETE') return request('DELETE', `/tasks/${match[1]}`);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/title$/)) && method === 'PUT') {
      return mapTask(await request('PATCH', `/tasks/${match[1]}`, { title: body.title }));
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/status$/)) && method === 'PUT') {
      const endpoint = body.status === 'stopped' ? 'stop' : 'resume';
      const payload = endpoint === 'resume'
        ? { run_duration_seconds: body.run_duration_seconds ?? null }
        : {};
      return mapTask(await request('POST', `/tasks/${match[1]}/${endpoint}`, payload));
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/worker-pool$/)) && method === 'PUT') {
      const result = await request('PATCH', `/tasks/${match[1]}`, { worker_pool: body.worker_pool || [] });
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
      if (method === 'GET') return mapIntent(await request('GET', `/tasks/${match[1]}/intents/${match[2]}`));
      if (method === 'PUT') return request('PATCH', `/tasks/${match[1]}/intents/${match[2]}`, {
        description: body.description,
        actor: body.actor || 'human.frontend',
      });
      if (method === 'DELETE') return request('DELETE', `/tasks/${match[1]}/intents/${match[2]}?actor=human.frontend`);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/intents\/([^/]+)\/archive$/)) && method === 'PUT') {
      return request('POST', `/tasks/${match[1]}/intents/${match[2]}/toggle-archive`, { actor: 'human.frontend' });
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/events$/)) && method === 'GET') {
      const params = new URLSearchParams({ limit: query.get('limit') || '500' });
      if (query.has('after_id')) params.set('after_id', query.get('after_id') || '0');
      params.set('task_id', match[1]);
      const events = await request('GET', `/audit-events?${params.toString()}`);
      return (query.has('after_id') ? events : events.reverse()).map(mapAuditEvent);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/maintainer-status$/)) && method === 'GET') return { maintaining_intent_ids: [] };
    if ((match = path.match(/^\/projects\/([^/]+)\/diagnostics$/)) && method === 'GET') {
      return request('GET', `/tasks/${match[1]}/diagnostics`);
    }
    if ((match = path.match(/^\/projects\/([^/]+)\/conversation(?:\/index)?$/)) && method === 'GET') {
      return { project_id: match[1], threads: [], items: [], stats: { workers: [], intents: [], thread_types: {}, timeout_count: 0, thread_count: 0, item_count: 0 }, filters: {} };
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
    if ((match = path.match(/^\/settings\/dispatch\/workers\/([^/]+)\/toggle$/)) && method === 'PUT') {
      const workers = await request('GET', '/workers');
      const worker = workers.find(item => item.name === decodeURIComponent(match[1]));
      if (!worker) throw new Error('Worker not found');
      await request('PUT', `/workers/${encodeURIComponent(worker.name)}`, { ...worker, disabled: !worker.disabled });
      return dispatchSettings();
    }
    if (path === '/settings/dispatch/models' && method === 'GET') {
      const worker = query.get('worker_name') || '';
      return request('GET', `/workers/${encodeURIComponent(worker)}/models?base_url=${encodeURIComponent(query.get('base_url') || '')}&api_key=${encodeURIComponent(query.get('api_key') || '')}`);
    }
    if (path === '/settings/test-pi' && method === 'POST') {
      return request('POST', `/workers/${encodeURIComponent(body.worker_name || '')}/test`, {
        base_url: body.pi_base_url, api_key: body.pi_api_key, model: body.pi_model, provider_api: body.pi_provider_api,
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
    taskScopeEditor: { taskId: '', enabled: false, targetsText: '', excludeText: '', notes: '', saving: false },
  });

  Object.assign(M.core.methods, {
    api: legacyApi,
    async fetchText(path) {
      let match;
      if ((match = path.match(/^\/projects\/([^/]+)\/vulns\/([^/]+)\/export$/))) {
        return request('GET', `/tasks/${match[1]}/vulnerabilities/${match[2]}/export`);
      }
      if ((match = path.match(/^\/projects\/([^/]+)\/export/))) {
        const format = new URL(path, location.origin).searchParams.get('format') || 'yaml';
        if (format === 'vulns_md') return request('GET', `/tasks/${match[1]}/vulnerabilities-export`);
        return request('GET', `/tasks/${match[1]}/export?format=${encodeURIComponent(format)}`);
      }
      return request('GET', path);
    },
  });

  Object.assign(M.projects.methods, {
    projectCanWriteHints() {
      return !this.replay.active
        && !this.project?.project?.legacy_readonly
        && ['active', 'stopped', 'completed'].includes(this.project?.project?.status);
    },
    async loadAllProjectDetails(force = false) {
      const taskIds = (this.projects || []).map(task => task.id);
      const liveIds = new Set(taskIds);
      for (const cache of [this.projectDetailsCache, this.projectEventsCache, this.taskDiagnostics, this.taskDiagnosticsLoading]) {
        for (const id of Object.keys(cache || {})) {
          if (!liveIds.has(id)) delete cache[id];
        }
      }
      if (!force && this.projectDetailsLoaded
        && taskIds.every(id => this.projectDetailsCache[id] && this.projectEventsCache[id])) return;

      // Route changes, polling and the initial Alpine render can all request
      // the same aggregate data at once.  Share one in-flight load instead of
      // racing three full vulnerability/event responses against each other.
      if (this._projectDetailsLoadPromise) {
        await this._projectDetailsLoadPromise;
        if (!force) return;
      }

      const load = (async () => {
        this.projectDetailsLoading = true;
        this.projectDetailsError = '';
        try {
        const [tasks, vulnerabilities, auditEvents] = await Promise.all([
          request('GET', '/tasks'),
          request('GET', '/vulnerabilities'),
          request('GET', '/audit-events?limit=2000'),
        ]);
        const taskMap = new Map(tasks.map(task => [task.id, task]));
        const projectMap = new Map((this.projectGroups || []).map(project => [project.id, project]));
        const vulnsByTask = new Map();
        for (const item of vulnerabilities) {
          if (!vulnsByTask.has(item.task_id)) vulnsByTask.set(item.task_id, []);
          vulnsByTask.get(item.task_id).push(item);
        }
        const eventsByTask = new Map();
        for (const event of auditEvents) {
          if (!event.task_id) continue;
          if (!eventsByTask.has(event.task_id)) eventsByTask.set(event.task_id, []);
          eventsByTask.get(event.task_id).push(mapAuditEvent(event));
        }
        for (const taskId of taskIds) {
          const task = taskMap.get(taskId);
          if (!task) continue;
          const mapped = mapTask(task);
          this.projectDetailsCache[taskId] = {
            project: mapped,
            facts: [],
            intents: [],
            hints: [],
            todos: [],
            vulns: (vulnsByTask.get(taskId) || []).map(item => mapVulnerability(item, task, projectMap.get(task.project_id))),
            artifacts: [],
            events: eventsByTask.get(taskId) || [],
          };
          this.projectEventsCache[taskId] = eventsByTask.get(taskId) || [];
        }
        this.projectDetailsLoaded = true;
        } catch (error) {
          this.projectDetailsError = error?.message || String(error);
          console.error(error);
        } finally {
          this.projectDetailsLoading = false;
        }
      })();
      this._projectDetailsLoadPromise = load;
      try {
        await load;
      } finally {
        if (this._projectDetailsLoadPromise === load) this._projectDetailsLoadPromise = null;
      }
    },
    async loadGlobalSkillUsage() {
      try {
        const data = await request('GET', '/skills/usage');
        this.globalSkillUsage = data.items || [];
      } catch (_) {
        this.globalSkillUsage = [];
      }
    },
    filteredTasks() {
      const keyword = String(this.taskSearch || '').trim().toLowerCase();
      const status = this.taskStatusFilter || 'all';
      return (this.projects || []).filter(task => {
        if (status !== 'all' && task.status !== status) return false;
        if (!keyword) return true;
        return [task.id, task.title, task.origin, task.goal, this.projectGroupTitle(task.parent_project_id)]
          .some(value => String(value || '').toLowerCase().includes(keyword));
      });
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
      if (!tasks.length) return null;
      let selected = tasks.find(task => task.id === this.selectedTaskManagementId);
      if (!selected) {
        selected = tasks[0];
        this.selectedTaskManagementId = selected.id;
      }
      if (this.taskWorkerPoolEditor.projectId !== selected.id) this.syncTaskWorkerPoolEditor(selected);
      if (this.taskScopeEditor.taskId !== selected.id) this.syncTaskScopeEditor(selected);
      if (!this.taskDiagnostics[selected.id] && !this.taskDiagnosticsLoading[selected.id]) {
        this.loadTaskDiagnostics(selected.id, true);
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
        enabled: !!scope.enabled,
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
      return this.taskScopeEditor.enabled !== !!scope.enabled
        || JSON.stringify(lines(this.taskScopeEditor.targetsText)) !== JSON.stringify(scope.targets || [])
        || JSON.stringify(lines(this.taskScopeEditor.excludeText)) !== JSON.stringify(scope.exclude || [])
        || String(this.taskScopeEditor.notes || '').trim() !== String(scope.notes || '').trim();
    },
    async saveTaskScope() {
      const task = this.selectedTaskSummary();
      if (!task || task.legacy_readonly || task.status === 'completed') return;
      try {
        this.taskScopeEditor.saving = true;
        const updated = await request('PATCH', `/tasks/${task.id}`, { scope: {
          enabled: !!this.taskScopeEditor.enabled,
          targets: splitLines(this.taskScopeEditor.targetsText),
          exclude: splitLines(this.taskScopeEditor.excludeText),
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
    async updateTodoStatus(todoId, status) {
      if (!this.selectedProjectId) return;
      try {
        await request('PATCH', `/tasks/${this.selectedProjectId}/todos/${todoId}`, { status, actor: this.actorName(), status_reason: '' });
        await this.loadProject(this.selectedProjectId);
        this.sideTab = 'todo';
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    async previewExternalVulnerability(vulnerability) {
      if (!vulnerability) return;
      try {
        const payload = await request('POST', `/tasks/${vulnerability.project_id}/vulnerabilities/${vulnerability.id}/external-preview`, {});
        this.yamlPreviewTitle = `${vulnerability.id} · 外部上报预览`;
        this.yamlPreviewText = JSON.stringify(payload, null, 2);
        this.yamlPreviewHtml = `<pre class="whitespace-pre-wrap break-words">${this.escapeHtml(this.yamlPreviewText)}</pre>`;
        this.showYamlModal = true;
      } catch (error) { this.showToast(error.message, 'error'); }
    },
    async reportExternalVulnerability(vulnerability) {
      if (!vulnerability || !confirm(`确认外部上报 ${vulnerability.id}？`)) return;
      try {
        const result = await request('POST', `/tasks/${vulnerability.project_id}/vulnerabilities/${vulnerability.id}/external-report`, {});
        const status = result.attempt?.status || 'failed';
        this.showToast(status === 'succeeded' ? '外部上报成功' : `外部上报失败：${result.attempt?.error || status}`, status === 'succeeded' ? 'success' : 'error');
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
    async loadArtifactCenter() {
      this.artifactCenterLoading = true;
      try {
        const tasks = (await request('GET', '/tasks')).map(mapTask);
        this.artifactCenterTasks = tasks;
        const selected = this.artifactCenterSelectedTask;
        const source = selected ? tasks.filter(item => item.id === selected) : tasks;
        const groups = await Promise.all(source.map(async task => (await request('GET', `/tasks/${task.id}/artifacts`)).map(item => {
          const mapped = mapArtifact(item);
          return { ...mapped, task_title: task.title, phase: this.artifactSourcePhase(mapped), task_id: task.id };
        })));
        this.artifactCenterItems = groups.flat();
      } catch (error) { this.showToast(error.message, 'error'); }
      finally { this.artifactCenterLoading = false; }
    },
    artifactCenterTypes() { return [...new Set((this.artifactCenterItems || []).map(item => item.artifact_type).filter(Boolean))].sort(); },
    artifactCenterPhases() { return [...new Set((this.artifactCenterItems || []).map(item => item.phase).filter(Boolean))].sort(); },
    artifactSourcePhase(item) {
      const type = String(item?.artifact_type || '').toLowerCase();
      const typedPhase = ['bootstrap', 'reason', 'explore', 'asm'].find(phase => type.startsWith(`${phase}.`));
      if (typedPhase) return typedPhase;
      const match = String(item?.path || '').match(/(?:^|\/)(bootstrap|reason|explore|asm)(?:[-_\/]|$)/i);
      return match ? match[1].toLowerCase() : (type === 'upload' ? 'upload' : 'unknown');
    },
    artifactCenterFiltered() {
      const query = String(this.artifactCenterSearch || '').trim().toLowerCase();
      return (this.artifactCenterItems || []).filter(item => {
        if (this.artifactCenterType !== 'all' && item.artifact_type !== this.artifactCenterType) return false;
        if (this.artifactCenterPhase !== 'all' && item.phase !== this.artifactCenterPhase) return false;
        if (this.artifactCenterSource === 'upload' && item.artifact_type !== 'upload') return false;
        if (this.artifactCenterSource === 'worker' && item.artifact_type === 'upload') return false;
        if (!query) return true;
        return [item.path, item.summary, item.intent_id, item.task_title, item.artifact_type].some(value => String(value || '').toLowerCase().includes(query));
      });
    },
    artifactFileName(item) { return String(item?.path || '').split('/').pop() || item?.id || 'artifact'; },
    artifactTypeIcon(type) { return type === 'upload' ? '↑' : /finding|vuln/i.test(type || '') ? '!' : /lead/i.test(type || '') ? '↗' : /evidence/i.test(type || '') ? '◈' : /api|asset|inventory/i.test(type || '') ? '◫' : '▤'; },
    artifactKindLabel(item) {
      const type = String(item?.artifact_type || '').toLowerCase();
      if (type === 'explore.findings') return 'Findings';
      if (type === 'explore.leads') return 'Leads';
      if (type === 'explore.evidence') return 'Evidence';
      return item?.artifact_type || 'Artifact';
    },
    artifactKindClass(item) {
      const type = String(item?.artifact_type || '').toLowerCase();
      if (type === 'explore.findings') return 'border-emerald-200 bg-emerald-50 text-emerald-700';
      if (type === 'explore.leads') return 'border-amber-200 bg-amber-50 text-amber-700';
      if (type === 'explore.evidence') return 'border-blue-200 bg-blue-50 text-blue-700';
      return 'border-slate-200 bg-slate-100 text-slate-500';
    },
    formatBytes(size) { const n = Number(size || 0); if (n < 1024) return `${n} B`; if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`; return `${(n / 1024 / 1024).toFixed(1)} MB`; },
    artifactPreviewKind(name) {
      const filename = String(name || '').toLowerCase();
      if (this.isImageFile(filename)) return 'image';
      if (this.isMdFile(filename)) return 'markdown';
      if (/\.(json|har)$/.test(filename)) return 'json';
      if (/\.(txt|log|csv|tsv|jsonl|ndjson|xml|ya?ml|js|mjs|cjs|jsx|ts|tsx|py|sh|bash|zsh|fish|html?|css|scss|sql|ini|conf|cfg|env|toml|properties|http)$/.test(filename)) return 'text';
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
          current._rendered = marked.parse(content);
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
      try { await request('DELETE', `/tasks/${item.task_id}/artifacts/${item.id}`); this.artifactCenterSelected = null; await this.loadArtifactCenter(); this.showToast('产物已归档'); }
      catch (error) { this.showToast(error.message, 'error'); }
    },
    async uploadArtifactCenter(event) {
      const file = event.target.files[0]; if (!file) return;
      const taskId = this.artifactCenterSelectedTask || this.artifactCenterTasks[0]?.id;
      if (!taskId) { this.showToast('请先选择一个任务', 'error'); event.target.value = ''; return; }
      const form = new FormData(); form.append('file', file); form.append('actor', this.actorName());
      try { await request('POST', `/tasks/${taskId}/artifacts/upload`, form); await this.loadArtifactCenter(); this.showToast('产物已上传'); }
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
        tasks: this.projects.filter(task => task.parent_project_id === group.id).map(task => ({
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
      const artifacts = (await request('GET', `/tasks/${taskId}/artifacts`)).map(mapArtifact);
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
      this.filePreview = { open: true, name, path, content, isMarkdown, isImage: false, rawUrl, rendered: isMarkdown ? marked.parse(content) : '' };
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
