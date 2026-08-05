-- Frozen AXIOM SQLite schema at Alembic 20260805_0035.
-- Generated from a schema-only production snapshot; contains no user data.

CREATE TABLE alembic_version (
	version_num VARCHAR(32) NOT NULL, 
	CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

CREATE TABLE archive_batch_members (
	task_id VARCHAR NOT NULL, 
	archive_id VARCHAR NOT NULL, 
	entity_type VARCHAR NOT NULL, 
	entity_id VARCHAR NOT NULL, 
	relation VARCHAR NOT NULL, 
	CONSTRAINT pk_archive_batch_members PRIMARY KEY (archive_id, entity_type, entity_id), 
	CONSTRAINT ck_archive_batch_members_ck_archive_batch_members_entity_type CHECK (entity_type IN ('fact','intent','artifact','vulnerability','todo')), 
	CONSTRAINT ck_archive_batch_members_ck_archive_batch_members_relation CHECK (relation IN ('root','producer','output','descendant','related')), 
	CONSTRAINT fk_archive_batch_members_archive_id_archive_batches FOREIGN KEY(archive_id) REFERENCES archive_batches (id) ON DELETE CASCADE
);

CREATE TABLE archive_batches (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	root_type VARCHAR NOT NULL, 
	root_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	reason TEXT NOT NULL, 
	actor VARCHAR NOT NULL, 
	closure_hash VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	restored_at VARCHAR, 
	restored_by VARCHAR, 
	CONSTRAINT pk_archive_batches PRIMARY KEY (id), 
	CONSTRAINT ck_archive_batches_ck_archive_batches_root_type CHECK (root_type IN ('fact','intent','artifact','vulnerability','todo')), 
	CONSTRAINT ck_archive_batches_ck_archive_batches_status CHECK (status IN ('active','restored')), 
	CONSTRAINT fk_archive_batches_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

CREATE TABLE "artifacts" (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	intent_id VARCHAR, 
	artifact_type VARCHAR NOT NULL, 
	storage_key TEXT NOT NULL, 
	summary TEXT NOT NULL, 
	content_hash VARCHAR NOT NULL, 
	size INTEGER NOT NULL, 
	created_by VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	archived BOOLEAN NOT NULL, 
	review_run_id VARCHAR, 
	CONSTRAINT pk_artifacts PRIMARY KEY (task_id, id), 
	CONSTRAINT fk_artifacts_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT uq_artifacts_task_id_storage_key UNIQUE (task_id, storage_key), 
	CONSTRAINT fk_artifacts_task_id_intent_id_intents FOREIGN KEY(task_id, intent_id) REFERENCES intents (task_id, id) ON DELETE RESTRICT, 
	CONSTRAINT fk_artifacts_task_id_review_run_id_vulnerability_review_runs FOREIGN KEY(task_id, review_run_id) REFERENCES vulnerability_review_runs (task_id, id) ON DELETE RESTRICT
);

CREATE TABLE audit_events (
	id VARCHAR NOT NULL, 
	project_id VARCHAR, 
	task_id VARCHAR, 
	intent_id VARCHAR, 
	workspace_id VARCHAR, 
	worker VARCHAR NOT NULL, 
	event_type VARCHAR NOT NULL, 
	level VARCHAR NOT NULL, 
	message TEXT NOT NULL, 
	payload_json TEXT NOT NULL, 
	created_at VARCHAR NOT NULL, reason_run_id VARCHAR, review_run_id VARCHAR, 
	CONSTRAINT pk_audit_events PRIMARY KEY (id)
);

CREATE TABLE axiom_tools (
	"key" VARCHAR NOT NULL, 
	title VARCHAR NOT NULL, 
	version VARCHAR NOT NULL, 
	description TEXT NOT NULL, 
	source VARCHAR NOT NULL, 
	package_path TEXT NOT NULL, 
	entrypoint TEXT NOT NULL, 
	manifest_json TEXT NOT NULL, 
	input_schema_json TEXT NOT NULL, 
	supported_phases_json TEXT NOT NULL, 
	risk_tags_json TEXT NOT NULL, 
	package_hash VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	status_error TEXT DEFAULT '' NOT NULL, 
	enabled BOOLEAN DEFAULT 1 NOT NULL, 
	discovered_at VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_axiom_tools PRIMARY KEY ("key"), 
	CONSTRAINT ck_axiom_tools_ck_axiom_tools_source CHECK (source IN ('bundled','managed')), 
	CONSTRAINT ck_axiom_tools_ck_axiom_tools_status CHECK (status IN ('ready','invalid','missing'))
);

CREATE TABLE "context_revisions" (
	task_id VARCHAR NOT NULL, 
	revision INTEGER NOT NULL, 
	phase VARCHAR NOT NULL, 
	intent_id VARCHAR, 
	manifest_key TEXT NOT NULL, 
	content_hash VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	lease_id VARCHAR, 
	reason_run_id VARCHAR, 
	review_run_id VARCHAR, 
	CONSTRAINT pk_context_revisions PRIMARY KEY (task_id, revision), 
	CONSTRAINT uq_context_revisions_task_id_reason_run_id_lease_id UNIQUE (task_id, reason_run_id, lease_id), 
	CONSTRAINT uq_context_revisions_task_id_intent_id_lease_id UNIQUE (task_id, intent_id, lease_id), 
	CONSTRAINT fk_context_revisions_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT uq_context_revisions_task_id_review_run_id_lease_id UNIQUE (task_id, review_run_id, lease_id)
);

CREATE TABLE credential_refreshes (
	id VARCHAR NOT NULL, 
	credential_id VARCHAR NOT NULL, 
	content TEXT NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_credential_refreshes PRIMARY KEY (id), 
	CONSTRAINT fk_credential_refreshes_credential_id_credentials FOREIGN KEY(credential_id) REFERENCES credentials (id) ON DELETE CASCADE, 
	CONSTRAINT uq_credential_refreshes_credential_id UNIQUE (credential_id)
);

CREATE TABLE "credentials" (
	id VARCHAR NOT NULL, 
	project_id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	content TEXT NOT NULL, 
	notes TEXT, 
	CONSTRAINT pk_credentials PRIMARY KEY (id), 
	CONSTRAINT fk_credentials_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
);

CREATE TABLE external_report_attempts (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	vulnerability_id VARCHAR NOT NULL, 
	attempt_no INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	endpoint TEXT NOT NULL, 
	payload_json TEXT NOT NULL, 
	response_status INTEGER, 
	response_body TEXT NOT NULL, 
	error TEXT NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	completed_at VARCHAR, 
	CONSTRAINT pk_external_report_attempts PRIMARY KEY (id), 
	CONSTRAINT fk_external_report_attempts_task_vulnerability FOREIGN KEY(task_id, vulnerability_id) REFERENCES vulnerabilities (task_id, id) ON DELETE CASCADE, 
	CONSTRAINT uq_external_report_attempts_task_vulnerability_attempt UNIQUE (task_id, vulnerability_id, attempt_no)
);

CREATE TABLE facts (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	description TEXT NOT NULL, 
	created_at VARCHAR NOT NULL, 
	archived BOOLEAN NOT NULL, 
	CONSTRAINT pk_facts PRIMARY KEY (task_id, id), 
	CONSTRAINT fk_facts_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

CREATE TABLE goal_evaluations (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	intent_id VARCHAR, 
	workspace_id VARCHAR, 
	achieved BOOLEAN NOT NULL, 
	reason TEXT NOT NULL, 
	evidence_refs_json TEXT NOT NULL, 
	evaluator VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, reason_run_id VARCHAR, goal_cycle_id VARCHAR, 
	CONSTRAINT pk_goal_evaluations PRIMARY KEY (id), 
	CONSTRAINT fk_goal_evaluations_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

CREATE TABLE id_counters (
	scope VARCHAR NOT NULL, 
	kind VARCHAR NOT NULL, 
	next_value INTEGER NOT NULL, 
	CONSTRAINT pk_id_counters PRIMARY KEY (scope, kind)
);

CREATE TABLE intent_interventions (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	intent_id VARCHAR NOT NULL, 
	lease_id VARCHAR NOT NULL, 
	actor VARCHAR DEFAULT 'human.frontend' NOT NULL, 
	message TEXT NOT NULL, 
	client_message_id VARCHAR NOT NULL, 
	status VARCHAR DEFAULT 'pending' NOT NULL, 
	error TEXT DEFAULT '' NOT NULL, 
	created_at VARCHAR NOT NULL, 
	sent_at VARCHAR, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_intent_interventions PRIMARY KEY (task_id, id), 
	CONSTRAINT fk_intent_interventions_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT fk_intent_interventions_task_id_intent_id_intents FOREIGN KEY(task_id, intent_id) REFERENCES intents (task_id, id) ON DELETE CASCADE, 
	CONSTRAINT ck_intent_interventions_status CHECK (status IN ('pending','sent','accepted','failed','expired'))
);

CREATE TABLE intent_sources (
	task_id VARCHAR NOT NULL, 
	intent_id VARCHAR NOT NULL, 
	fact_id VARCHAR NOT NULL, 
	CONSTRAINT pk_intent_sources PRIMARY KEY (task_id, intent_id, fact_id), 
	CONSTRAINT fk_intent_sources_task_id_fact_id_facts FOREIGN KEY(task_id, fact_id) REFERENCES facts (task_id, id) ON DELETE RESTRICT, 
	CONSTRAINT fk_intent_sources_task_id_intent_id_intents FOREIGN KEY(task_id, intent_id) REFERENCES intents (task_id, id) ON DELETE CASCADE
);

CREATE TABLE "intents" (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	todo_id VARCHAR, 
	description TEXT NOT NULL, 
	status VARCHAR NOT NULL, 
	creator VARCHAR NOT NULL, 
	worker VARCHAR, 
	worker_pool_json TEXT NOT NULL, 
	targets_json TEXT NOT NULL, 
	execute_timeout_seconds INTEGER, 
	conclude_timeout_seconds INTEGER, 
	last_heartbeat_at VARCHAR, 
	retry_count INTEGER NOT NULL, 
	infrastructure_retry_count INTEGER NOT NULL, 
	next_retry_at VARCHAR, 
	last_failure_kind VARCHAR NOT NULL, 
	failure_class VARCHAR NOT NULL, 
	to_fact_id VARCHAR, 
	created_at VARCHAR NOT NULL, 
	concluded_at VARCHAR, 
	archived BOOLEAN NOT NULL, 
	kind VARCHAR DEFAULT 'explore' NOT NULL, lease_id VARCHAR, claimed_at VARCHAR, activity_kind VARCHAR DEFAULT '' NOT NULL, activity_summary TEXT DEFAULT '' NOT NULL, activity_tool VARCHAR DEFAULT '' NOT NULL, activity_started_at VARCHAR, activity_updated_at VARCHAR, goal_cycle_id VARCHAR, 
	CONSTRAINT pk_intents PRIMARY KEY (task_id, id), 
	CONSTRAINT fk_intents_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT fk_intents_task_id_to_fact_id_facts FOREIGN KEY(task_id, to_fact_id) REFERENCES facts (task_id, id) ON DELETE RESTRICT, 
	CONSTRAINT ck_intents_ck_intents_status CHECK (status IN ('pending','claimed','running','concluding','completed','failed','cancelled')), 
	CONSTRAINT ck_intents_ck_intents_kind CHECK (kind IN ('bootstrap','reason','explore'))
);

CREATE TABLE "llm_token_usage" (
	source_key VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	intent_id VARCHAR, 
	reason_run_id VARCHAR, 
	lease_id VARCHAR NOT NULL, 
	phase VARCHAR NOT NULL, 
	worker VARCHAR DEFAULT ('') NOT NULL, 
	provider VARCHAR DEFAULT ('') NOT NULL, 
	model VARCHAR DEFAULT ('') NOT NULL, 
	requested_model VARCHAR DEFAULT ('') NOT NULL, 
	session_id VARCHAR DEFAULT ('') NOT NULL, 
	session_file TEXT NOT NULL, 
	session_entry_id VARCHAR NOT NULL, 
	usage_kind VARCHAR NOT NULL, 
	occurred_at VARCHAR NOT NULL, 
	input_tokens INTEGER DEFAULT '0' NOT NULL, 
	cache_read_tokens INTEGER DEFAULT '0' NOT NULL, 
	cache_write_tokens INTEGER DEFAULT '0' NOT NULL, 
	output_tokens INTEGER DEFAULT '0' NOT NULL, 
	reasoning_tokens INTEGER DEFAULT '0' NOT NULL, 
	reported_total_tokens INTEGER, 
	source VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	review_run_id VARCHAR, 
	CONSTRAINT pk_llm_token_usage PRIMARY KEY (source_key), 
	CONSTRAINT ck_llm_token_usage_ck_llm_token_usage_source CHECK (source IN ('live','backfill','legacy')), 
	CONSTRAINT ck_llm_token_usage_ck_llm_token_usage_nonnegative_tokens CHECK (input_tokens >= 0 AND cache_read_tokens >= 0 AND cache_write_tokens >= 0 AND output_tokens >= 0 AND reasoning_tokens >= 0 AND (reported_total_tokens IS NULL OR reported_total_tokens >= 0)), 
	CONSTRAINT ck_llm_token_usage_ck_llm_token_usage_usage_kind CHECK (usage_kind IN ('assistant','compaction','branch_summary','tool_result','legacy_audit')), 
	CONSTRAINT fk_llm_token_usage_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT ck_llm_token_usage_ck_llm_token_usage_phase CHECK (phase IN ('bootstrap','explore','reason','review','unknown'))
);

CREATE TABLE llm_token_usage_sources (
	source_key VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	intent_id VARCHAR, 
	reason_run_id VARCHAR, 
	lease_id VARCHAR NOT NULL, 
	session_file TEXT NOT NULL, 
	session_id VARCHAR DEFAULT '' NOT NULL, 
	status VARCHAR NOT NULL, 
	error TEXT DEFAULT '' NOT NULL, 
	source VARCHAR NOT NULL, 
	scanned_at VARCHAR NOT NULL, review_run_id VARCHAR, 
	CONSTRAINT pk_llm_token_usage_sources PRIMARY KEY (source_key), 
	CONSTRAINT ck_llm_token_usage_sources_ck_llm_token_usage_sources_status CHECK (status IN ('complete','incomplete')), 
	CONSTRAINT ck_llm_token_usage_sources_ck_llm_token_usage_sources_source CHECK (source IN ('live','backfill','legacy')), 
	CONSTRAINT fk_llm_token_usage_sources_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

CREATE TABLE mcp_servers (
	id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	transport VARCHAR NOT NULL, 
	command VARCHAR NOT NULL, 
	args_json TEXT NOT NULL, 
	env_json TEXT NOT NULL, 
	url TEXT NOT NULL, 
	source_format VARCHAR NOT NULL, 
	raw_config_json TEXT NOT NULL, 
	disabled BOOLEAN NOT NULL, 
	discovery_status VARCHAR NOT NULL, 
	discovery_error TEXT NOT NULL, 
	discovered_at VARCHAR NOT NULL, 
	server_info_json TEXT NOT NULL, 
	instructions TEXT NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_mcp_servers PRIMARY KEY (id), 
	CONSTRAINT uq_mcp_servers_name UNIQUE (name)
);

CREATE TABLE mcp_tools (
	id VARCHAR NOT NULL, 
	server_id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	title VARCHAR NOT NULL, 
	description TEXT NOT NULL, 
	input_schema_json TEXT NOT NULL, 
	output_schema_json TEXT NOT NULL, 
	annotations_json TEXT NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	source VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_mcp_tools PRIMARY KEY (id), 
	CONSTRAINT fk_mcp_tools_server_id_mcp_servers FOREIGN KEY(server_id) REFERENCES mcp_servers (id) ON DELETE CASCADE, 
	CONSTRAINT uq_mcp_tools_server_id_name UNIQUE (server_id, name)
);

CREATE TABLE "phase_capability_bindings" (
	scope VARCHAR NOT NULL, 
	capability_kind VARCHAR NOT NULL, 
	capability_key TEXT NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT ck_phase_capability_bindings_ck_phase_capability_bindings_capability_kind CHECK (capability_kind IN ('skill','mcp_tool','axiom_tool')), 
	CONSTRAINT uq_phase_capability_bindings_scope_kind_key UNIQUE (scope, capability_kind, capability_key), 
	CONSTRAINT ck_phase_capability_bindings_ck_phase_capability_bindings_scope CHECK (scope IN ('common','bootstrap','reason','explore','review'))
);

CREATE TABLE project_knowledge (
	id VARCHAR NOT NULL, 
	project_id VARCHAR NOT NULL, 
	title TEXT NOT NULL, 
	content TEXT NOT NULL, 
	category VARCHAR NOT NULL, 
	created_by VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	archived BOOLEAN NOT NULL, 
	CONSTRAINT pk_project_knowledge PRIMARY KEY (project_id, id), 
	CONSTRAINT fk_project_knowledge_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
);

CREATE TABLE projects (
	id VARCHAR NOT NULL, 
	title TEXT NOT NULL, 
	description TEXT NOT NULL, 
	status VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_projects PRIMARY KEY (id), 
	CONSTRAINT ck_projects_ck_projects_status CHECK (status IN ('active','archived'))
);

CREATE TABLE reason_run_sources (
	task_id VARCHAR NOT NULL, 
	reason_run_id VARCHAR NOT NULL, 
	fact_id VARCHAR NOT NULL, 
	CONSTRAINT fk_reason_run_sources_task_id_fact_id_facts FOREIGN KEY(task_id, fact_id) REFERENCES facts (task_id, id) ON DELETE CASCADE, 
	CONSTRAINT fk_reason_run_sources_task_id_reason_run_id_reason_runs FOREIGN KEY(task_id, reason_run_id) REFERENCES reason_runs (task_id, id) ON DELETE CASCADE, 
	CONSTRAINT uq_reason_run_sources_reason_run_id_fact_id UNIQUE (reason_run_id, fact_id)
);

CREATE TABLE reason_runs (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	"trigger" VARCHAR NOT NULL, 
	trigger_payload_json TEXT DEFAULT '{}' NOT NULL, 
	input_fingerprint VARCHAR NOT NULL, 
	worker_pool_json TEXT DEFAULT '[]' NOT NULL, 
	worker VARCHAR, 
	settings_version INTEGER NOT NULL, 
	execute_timeout_seconds INTEGER NOT NULL, 
	conclude_timeout_seconds INTEGER NOT NULL, 
	target_open_intents INTEGER NOT NULL, 
	max_new_intents INTEGER NOT NULL, 
	explore_execute_min_seconds INTEGER NOT NULL, 
	explore_execute_max_seconds INTEGER NOT NULL, 
	explore_conclude_min_seconds INTEGER NOT NULL, 
	explore_conclude_max_seconds INTEGER NOT NULL, 
	lease_id VARCHAR, 
	claimed_at VARCHAR, 
	last_heartbeat_at VARCHAR, 
	retry_count INTEGER DEFAULT '0' NOT NULL, 
	infrastructure_retry_count INTEGER DEFAULT '0' NOT NULL, 
	next_retry_at VARCHAR, 
	last_failure_kind VARCHAR DEFAULT '' NOT NULL, 
	failure_class VARCHAR DEFAULT '' NOT NULL, 
	activity_kind VARCHAR DEFAULT '' NOT NULL, 
	activity_summary TEXT DEFAULT '' NOT NULL, 
	activity_tool VARCHAR DEFAULT '' NOT NULL, 
	activity_started_at VARCHAR, 
	activity_updated_at VARCHAR, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	concluded_at VARCHAR, goal_cycle_id VARCHAR, 
	CONSTRAINT pk_reason_runs PRIMARY KEY (id), 
	CONSTRAINT ck_reason_runs_ck_reason_runs_status CHECK (status IN ('pending','claimed','running','concluding','completed','failed','cancelled')), 
	CONSTRAINT fk_reason_runs_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT uq_reason_runs_task_id_id UNIQUE (task_id, id)
);

CREATE TABLE "reason_workspaces" (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	intent_id VARCHAR, 
	status VARCHAR NOT NULL, 
	context_revision INTEGER NOT NULL, 
	draft_json TEXT NOT NULL, 
	worker VARCHAR NOT NULL, 
	failure_reason TEXT NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	committed_at VARCHAR, 
	lease_id VARCHAR NOT NULL, 
	result_json TEXT DEFAULT '{}' NOT NULL, 
	reason_run_id VARCHAR, 
	CONSTRAINT pk_reason_workspaces PRIMARY KEY (id), 
	CONSTRAINT fk_reason_workspaces_task_id_intent_id_intents FOREIGN KEY(task_id, intent_id) REFERENCES intents (task_id, id) ON DELETE CASCADE, 
	CONSTRAINT ck_reason_workspaces_ck_reason_workspaces_status CHECK (status IN ('open','committed','abandoned')), 
	CONSTRAINT fk_reason_workspaces_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT fk_reason_workspaces_task_id_reason_run_id_reason_runs FOREIGN KEY(task_id, reason_run_id) REFERENCES reason_runs (task_id, id) ON DELETE CASCADE, 
	CONSTRAINT ck_reason_workspaces_ck_reason_workspaces_owner CHECK ((intent_id IS NOT NULL AND reason_run_id IS NULL) OR (intent_id IS NULL AND reason_run_id IS NOT NULL)), 
	CONSTRAINT uq_reason_workspaces_task_id_intent_id_lease_id UNIQUE (task_id, intent_id, lease_id), 
	CONSTRAINT uq_reason_workspaces_task_id_reason_run_id_lease_id UNIQUE (task_id, reason_run_id, lease_id)
);

CREATE TABLE runtime_settings (
	id VARCHAR NOT NULL, 
	config_json TEXT NOT NULL, 
	version INTEGER NOT NULL, 
	updated_by VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, applied_version INTEGER DEFAULT '0' NOT NULL, applied_at VARCHAR, 
	CONSTRAINT pk_runtime_settings PRIMARY KEY (id)
);

CREATE TABLE task_goal_cycles (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	sequence INTEGER NOT NULL, 
	goal TEXT NOT NULL, 
	status VARCHAR NOT NULL, 
	continuation_reason TEXT DEFAULT '' NOT NULL, 
	focus TEXT DEFAULT '' NOT NULL, 
	actor VARCHAR NOT NULL, 
	started_at VARCHAR NOT NULL, 
	completed_at VARCHAR, 
	CONSTRAINT pk_task_goal_cycles PRIMARY KEY (id), 
	CONSTRAINT ck_task_goal_cycles_ck_task_goal_cycles_status CHECK (status IN ('active','completed')), 
	CONSTRAINT fk_task_goal_cycles_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT uq_task_goal_cycles_task_sequence UNIQUE (task_id, sequence)
);

CREATE TABLE task_hints (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	content TEXT NOT NULL, 
	creator VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	archived BOOLEAN NOT NULL, 
	CONSTRAINT pk_task_hints PRIMARY KEY (task_id, id), 
	CONSTRAINT fk_task_hints_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

CREATE TABLE "tasks" (
	id VARCHAR NOT NULL, 
	project_id VARCHAR NOT NULL, 
	title TEXT NOT NULL, 
	origin_host VARCHAR NOT NULL, 
	origin_scheme VARCHAR, 
	origin_port VARCHAR, 
	origin_input TEXT NOT NULL, 
	goal TEXT NOT NULL, 
	status VARCHAR NOT NULL, 
	phase VARCHAR NOT NULL, 
	scope_json TEXT NOT NULL, 
	worker_pool_json TEXT NOT NULL, 
	guard_enabled BOOLEAN NOT NULL, 
	external_report_enabled BOOLEAN NOT NULL, 
	legacy_readonly BOOLEAN NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	completed_at VARCHAR, 
	run_duration_seconds INTEGER, 
	run_started_at VARCHAR, 
	run_deadline_at VARCHAR, 
	last_pause_reason VARCHAR, 
	current_goal_cycle_id VARCHAR, 
	archived BOOLEAN DEFAULT 0 NOT NULL, 
	CONSTRAINT pk_tasks PRIMARY KEY (id), 
	CONSTRAINT ck_tasks_ck_tasks_run_duration_seconds CHECK (run_duration_seconds IS NULL OR (run_duration_seconds BETWEEN 60 AND 31536000 AND run_duration_seconds % 60 = 0)), 
	CONSTRAINT ck_tasks_ck_tasks_last_pause_reason CHECK (last_pause_reason IS NULL OR last_pause_reason IN ('manual','run_deadline_reached')), 
	CONSTRAINT ck_tasks_ck_tasks_status CHECK (status IN ('running','stopped','completed','failed')), 
	CONSTRAINT fk_tasks_project_id_projects FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, 
	CONSTRAINT ck_tasks_ck_tasks_phase CHECK (phase IN ('bootstrap','reason','explore','waiting','none'))
);

CREATE TABLE todo_sources (
	task_id VARCHAR NOT NULL, 
	todo_id VARCHAR NOT NULL, 
	source_type VARCHAR NOT NULL, 
	source_id VARCHAR NOT NULL, 
	CONSTRAINT pk_todo_sources PRIMARY KEY (task_id, todo_id, source_type, source_id), 
	CONSTRAINT ck_todo_sources_ck_todo_sources_source_type CHECK (source_type IN ('fact','hint','artifact','vulnerability','knowledge','origin')), 
	CONSTRAINT fk_todo_sources_task_id_todo_id_todos FOREIGN KEY(task_id, todo_id) REFERENCES todos (task_id, id) ON DELETE CASCADE
);

CREATE TABLE todos (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	title TEXT NOT NULL, 
	category VARCHAR NOT NULL, 
	theme VARCHAR NOT NULL, 
	priority INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	created_by VARCHAR NOT NULL, 
	status_reason TEXT NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	completed_at VARCHAR, 
	archived BOOLEAN NOT NULL, 
	CONSTRAINT pk_todos PRIMARY KEY (task_id, id), 
	CONSTRAINT ck_todos_ck_todos_category CHECK (category IN ('recon','authentication','authorization','injection','business_logic','file','network','configuration','client_side','other')), 
	CONSTRAINT ck_todos_ck_todos_priority CHECK (priority BETWEEN 0 AND 100), 
	CONSTRAINT ck_todos_ck_todos_status CHECK (status IN ('pending','active','completed','rejected','obsolete')), 
	CONSTRAINT fk_todos_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

CREATE TABLE vulnerabilities (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	intent_id VARCHAR, 
	title TEXT NOT NULL, 
	type VARCHAR NOT NULL, 
	severity VARCHAR NOT NULL, 
	description TEXT NOT NULL, 
	prerequisites TEXT NOT NULL, 
	impact TEXT NOT NULL, 
	reproduction_steps_json TEXT NOT NULL, 
	remediation TEXT NOT NULL, 
	curl_command TEXT NOT NULL, 
	evidence_json TEXT NOT NULL, 
	affected_target VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	status_reason TEXT NOT NULL, 
	source VARCHAR NOT NULL, 
	reported_by VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	archived BOOLEAN NOT NULL, format_version INTEGER DEFAULT '1' NOT NULL, 
	CONSTRAINT pk_vulnerabilities PRIMARY KEY (task_id, id), 
	CONSTRAINT fk_vulnerabilities_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT fk_vulnerabilities_task_id_intent_id_intents FOREIGN KEY(task_id, intent_id) REFERENCES intents (task_id, id) ON DELETE RESTRICT
);

CREATE TABLE vulnerability_artifacts (
	task_id VARCHAR NOT NULL, 
	vulnerability_id VARCHAR NOT NULL, 
	artifact_id VARCHAR NOT NULL, 
	role VARCHAR NOT NULL, 
	display_order INTEGER DEFAULT '0' NOT NULL, 
	CONSTRAINT pk_vulnerability_artifacts PRIMARY KEY (task_id, vulnerability_id, artifact_id), 
	CONSTRAINT ck_vulnerability_artifacts_ck_vulnerability_artifacts_role CHECK (role IN ('finding','evidence')), 
	CONSTRAINT fk_vulnerability_artifacts_task_id_artifact_id_artifacts FOREIGN KEY(task_id, artifact_id) REFERENCES artifacts (task_id, id) ON DELETE RESTRICT, 
	CONSTRAINT fk_vulnerability_artifacts_task_id_vulnerability_id_vulnerabilities FOREIGN KEY(task_id, vulnerability_id) REFERENCES vulnerabilities (task_id, id) ON DELETE CASCADE
);

CREATE TABLE vulnerability_review_runs (
	id VARCHAR NOT NULL, 
	task_id VARCHAR NOT NULL, 
	vulnerability_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	"trigger" VARCHAR NOT NULL, 
	trigger_payload_json TEXT DEFAULT '{}' NOT NULL, 
	input_fingerprint VARCHAR NOT NULL, 
	worker_pool_json TEXT DEFAULT '[]' NOT NULL, 
	worker VARCHAR, 
	settings_version INTEGER DEFAULT '0' NOT NULL, 
	execute_timeout_seconds INTEGER NOT NULL, 
	conclude_timeout_seconds INTEGER NOT NULL, 
	lease_id VARCHAR, 
	claimed_at VARCHAR, 
	last_heartbeat_at VARCHAR, 
	retry_count INTEGER DEFAULT '0' NOT NULL, 
	infrastructure_retry_count INTEGER DEFAULT '0' NOT NULL, 
	next_retry_at VARCHAR, 
	last_failure_kind VARCHAR DEFAULT '' NOT NULL, 
	failure_class VARCHAR DEFAULT '' NOT NULL, 
	activity_kind VARCHAR DEFAULT '' NOT NULL, 
	activity_summary TEXT DEFAULT '' NOT NULL, 
	activity_tool VARCHAR DEFAULT '' NOT NULL, 
	activity_started_at VARCHAR, 
	activity_updated_at VARCHAR, 
	decision VARCHAR, 
	decision_reason TEXT DEFAULT '' NOT NULL, 
	duplicate_of_vulnerability_id VARCHAR, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	concluded_at VARCHAR, 
	CONSTRAINT pk_vulnerability_review_runs PRIMARY KEY (id), 
	CONSTRAINT ck_vulnerability_review_runs_ck_vulnerability_review_runs_status CHECK (status IN ('pending','claimed','running','concluding','completed','failed','cancelled')), 
	CONSTRAINT ck_vulnerability_review_runs_ck_vulnerability_review_runs_trigger CHECK (trigger IN ('reported','updated','manual','restored','migration')), 
	CONSTRAINT ck_vulnerability_review_runs_ck_vulnerability_review_runs_decision CHECK (decision IS NULL OR decision IN ('verified','duplicate','ignored','needs-human')), 
	CONSTRAINT fk_vulnerability_review_runs_task_id_tasks FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE, 
	CONSTRAINT fk_vulnerability_review_runs_task_id_vulnerability_id_vulnerabilities FOREIGN KEY(task_id, vulnerability_id) REFERENCES vulnerabilities (task_id, id) ON DELETE CASCADE, 
	CONSTRAINT fk_vulnerability_review_runs_task_id_duplicate_of_vulnerability_id_vulnerabilities FOREIGN KEY(task_id, duplicate_of_vulnerability_id) REFERENCES vulnerabilities (task_id, id) ON DELETE RESTRICT, 
	CONSTRAINT uq_vulnerability_review_runs_task_id_id UNIQUE (task_id, id)
);

CREATE TABLE "worker_axiom_tool_bindings" (
	worker_name VARCHAR NOT NULL, 
	phase VARCHAR NOT NULL, 
	tool_key VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_worker_axiom_tool_bindings PRIMARY KEY (worker_name, phase, tool_key), 
	CONSTRAINT fk_worker_axiom_tool_bindings_worker_name_workers FOREIGN KEY(worker_name) REFERENCES workers (name) ON DELETE CASCADE, 
	CONSTRAINT fk_worker_axiom_tool_bindings_tool_key_axiom_tools FOREIGN KEY(tool_key) REFERENCES axiom_tools ("key") ON DELETE CASCADE, 
	CONSTRAINT ck_worker_axiom_tool_bindings_ck_worker_axiom_tool_bindings_phase CHECK (phase IN ('bootstrap','reason','explore','review'))
);

CREATE TABLE workers (
	name VARCHAR NOT NULL, 
	worker_type VARCHAR NOT NULL, 
	capabilities_json TEXT NOT NULL, 
	max_running INTEGER NOT NULL, 
	priority INTEGER NOT NULL, 
	config_json TEXT NOT NULL, 
	disabled BOOLEAN NOT NULL, 
	cooldown_until VARCHAR, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_workers PRIMARY KEY (name), 
	CONSTRAINT ck_workers_ck_workers_max_running CHECK (max_running > 0)
);

CREATE INDEX ix_archive_batch_members_task_entity ON archive_batch_members (task_id, entity_type, entity_id);

CREATE INDEX ix_archive_batches_task_root ON archive_batches (task_id, root_type, root_id);

CREATE INDEX ix_archive_batches_task_status ON archive_batches (task_id, status);

CREATE INDEX ix_artifacts_active_created ON artifacts (archived, created_at);

CREATE INDEX ix_artifacts_task_active_created ON artifacts (task_id, archived, created_at);

CREATE INDEX ix_artifacts_task_active_intent_created ON artifacts (task_id, archived, intent_id, created_at);

CREATE INDEX ix_artifacts_task_active_review_created ON artifacts (task_id, archived, review_run_id, created_at);

CREATE INDEX ix_artifacts_type_created ON artifacts (artifact_type, created_at);

CREATE INDEX ix_audit_events_project_created ON audit_events (project_id, created_at);

CREATE INDEX ix_audit_events_task_created ON audit_events (task_id, created_at);

CREATE INDEX ix_context_revisions_task_intent ON context_revisions (task_id, intent_id);

CREATE INDEX ix_context_revisions_task_reason_run ON context_revisions (task_id, reason_run_id);

CREATE INDEX ix_context_revisions_task_review_run ON context_revisions (task_id, review_run_id);

CREATE INDEX ix_credentials_project_enabled ON credentials (project_id, enabled);

CREATE INDEX ix_external_report_attempts_task_created ON external_report_attempts (task_id, created_at);

CREATE INDEX ix_goal_evaluations_task_created ON goal_evaluations (task_id, created_at);

CREATE INDEX ix_intent_interventions_queue ON intent_interventions (task_id, intent_id, lease_id, status, created_at);

CREATE INDEX ix_llm_token_usage_model_occurred ON llm_token_usage (provider, model, occurred_at);

CREATE INDEX ix_llm_token_usage_sources_task_status ON llm_token_usage_sources (task_id, status);

CREATE INDEX ix_llm_token_usage_task_occurred ON llm_token_usage (task_id, occurred_at);

CREATE INDEX ix_llm_token_usage_task_phase_worker ON llm_token_usage (task_id, phase, worker);

CREATE INDEX ix_phase_capability_bindings_kind_key ON phase_capability_bindings (capability_kind, capability_key);

CREATE INDEX ix_project_knowledge_project_updated ON project_knowledge (project_id, updated_at);

CREATE INDEX ix_reason_run_sources_task_fact ON reason_run_sources (task_id, fact_id);

CREATE INDEX ix_reason_runs_retry ON reason_runs (status, next_retry_at);

CREATE INDEX ix_reason_runs_task_status ON reason_runs (task_id, status);

CREATE INDEX ix_reason_workspaces_task_status ON reason_workspaces (task_id, status);

CREATE INDEX ix_task_goal_cycles_task_sequence ON task_goal_cycles (task_id, sequence);

CREATE INDEX ix_tasks_archived_status ON tasks (archived, status);

CREATE INDEX ix_tasks_status_run_deadline ON tasks (status, run_deadline_at);

CREATE INDEX ix_todos_task_status_priority ON todos (task_id, status, priority);

CREATE INDEX ix_vulnerabilities_active_updated ON vulnerabilities (archived, updated_at);

CREATE INDEX ix_vulnerabilities_severity_updated ON vulnerabilities (severity, updated_at);

CREATE INDEX ix_vulnerabilities_status_updated ON vulnerabilities (status, updated_at);

CREATE INDEX ix_vulnerabilities_task_updated ON vulnerabilities (task_id, updated_at);

CREATE INDEX ix_vulnerability_artifacts_task_vulnerability_role ON vulnerability_artifacts (task_id, vulnerability_id, role);

CREATE INDEX ix_vulnerability_review_runs_retry ON vulnerability_review_runs (status, next_retry_at);

CREATE INDEX ix_vulnerability_review_runs_task_status ON vulnerability_review_runs (task_id, status);

CREATE INDEX ix_vulnerability_review_runs_vulnerability_created ON vulnerability_review_runs (task_id, vulnerability_id, created_at);

CREATE INDEX ix_worker_axiom_tool_bindings_tool_phase ON worker_axiom_tool_bindings (tool_key, phase);

CREATE UNIQUE INDEX uq_intent_interventions_client ON intent_interventions (task_id, intent_id, client_message_id);

CREATE UNIQUE INDEX uq_reason_runs_one_open_per_task ON reason_runs (task_id) WHERE status IN ('pending','claimed','running','concluding');

CREATE UNIQUE INDEX uq_vulnerability_review_runs_one_open ON vulnerability_review_runs (task_id, vulnerability_id) WHERE status IN ('pending','claimed','running','concluding');
