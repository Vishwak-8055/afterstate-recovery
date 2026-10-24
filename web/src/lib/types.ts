export type RunState = "QUEUED" | "PREPARING" | "RESTORING" | "VALIDATING" | "FINALIZING" | "SUCCEEDED" | "FAILED" | "CANCELLED" | "NEEDS_CLEANUP" | "NEEDS_ATTENTION";
export type Outcome = "PASS" | "FAIL" | "SKIPPED" | "INCONCLUSIVE";
export type Role = "owner" | "admin" | "operator" | "viewer";
export interface Membership { workspace_id: string; name: string; slug: string; role: Role }
export interface Me { user: { id: string; email: string; display_name: string }; memberships: Membership[]; csrf_token: string | null }
export interface Page<T> { items: T[]; total: number; page: number; page_size: number }
export interface Metrics { recovery_seconds: number | null; restore_seconds: number | null; validation_seconds: number | null; total_seconds: number | null; restore_throughput_mb_s: number | null; restored_database_bytes: number | null; artifact_size_bytes: number | null }
export interface RunSummary {
  id: string; project_id: string; project_name: string | null; artifact_id: string; artifact_name: string | null; trigger: string; attempt: number;
  parent_run_id: string | null; state: RunState; settled_state: RunState | null; failure_class: string | null; failure_message: string | null;
  summary: string | null; queued_at: string; started_at: string | null; finished_at: string | null; cleanup_state: string; lab_scenario: string | null;
  cancel_requested: boolean; progress: { items_done?: number; items_total?: number; stage?: string }; metrics: Metrics; objectives: Record<string, string>;
}
export interface Artifact {
  id: string; name: string; label: string; original_filename: string; size_bytes: number; sha256: string; expected_sha256: string | null; format: string;
  status: "verified" | "rejected" | "unverified"; status_detail: string; dump_server_version: string | null; dump_tool_version: string | null;
  source_database: string | null; archive_created_at: string | null; backup_created_at: string | null; toc_entries: number | null; compression: string | null;
  notes: string; last_verified_at: string | null; created_at: string; lab_scenario: string | null; restore_verified?: boolean; last_restore_at?: string | null;
}
export interface Rule { id?: string; position?: number; name: string; kind: string; required: boolean; config: Record<string, unknown> }
export interface Project {
  id: string; name: string; description: string; artifact_id: string | null; artifact_label: string | null; restore_options: { jobs?: number };
  restore_timeout_seconds: number; validation_timeout_seconds: number; rto_seconds: number | null; rpo_seconds: number | null; max_test_age_days: number;
  max_attempts: number; app_suite: string | null; rules: Rule[]; rule_count?: number; latest_run?: RunSummary | null; lab_scenario: string | null;
}
export interface CheckResult { position: number; key: string; category: string; name: string; outcome: Outcome; required: boolean; message: string; expected: unknown; actual: unknown; coverage: string | null; duration_ms: number | null }
export interface Stage { name: string; state: string; started_at: string | null; finished_at: string | null; duration_ms: number | null; error: string | null; details: Record<string, unknown> }
export interface Objective { status: "MET" | "MISSED" | "UNKNOWN" | "NOT_CONFIGURED"; target_seconds: number | null; measured_seconds: number | null; detail: string; basis: string | null }
export interface RunDetail extends Omit<RunSummary, "metrics" | "objectives"> {
  metrics: Record<string, unknown>; objectives: Record<string, Objective>; environment: Record<string, unknown>; config: Record<string, unknown>;
  stages: Stage[]; results: CheckResult[]; cleanup: { state: string; tasks: { resource_type: string; resource: string; state: string; attempts: number; error: string | null }[] };
  evidence: { kind: string; sha256: string; size_bytes: number }[]; retry_of: string | null; retries: string[]; fault: Record<string, unknown> | null;
}
export interface RunEvent { id: number; ts: string; level: string; stage: string | null; kind: string; message: string }
export interface EventsResponse { events: RunEvent[]; next_after: number; state: RunState; settled_state: RunState | null; progress: RunSummary["progress"]; current_stage: string | null; terminal: boolean }
export interface Readiness { project_id: string; name: string; readiness: "verified" | "stale" | "at_risk" | "unverified" | "running"; reason: string; latest_run: RunSummary | null; last_success_at: string | null; last_success_age_days: number | null; max_test_age_days: number }
export interface Overview {
  readiness: Record<string, number>; projects: Readiness[]; projects_total: number; unverified_artifacts: { count: number; items: { id: string; name: string; created_at: string; status: string }[] };
  objectives_missed: { project: string; project_id: string; run_id: string; objectives: Record<string, string> }[]; cleanup_pending: number;
  failure_rate_30d: { finished: number; failed: number; needs_attention: number; succeeded: number; rate: number | null };
  series: { run_id: string; at: string; project: string; state: string; recovery_seconds: number | null; restore_seconds: number | null; throughput_mb_s: number | null }[];
  recent_runs: RunSummary[]; activity: { id: number; at: string; actor: string; action: string; outcome: string; target_type: string }[];
}
export interface RuleField { name: string; label: string; type: string; required?: boolean; placeholder?: string; default?: unknown; options?: string[] }
export interface RuleKindSpec { kind: string; label: string; stage: string; help: string; fields: RuleField[] }
export interface Meta { rule_catalog: RuleKindSpec[]; app_suites: { key: string; description: string }[]; restore_skipped_object_classes: string[]; postgresql: string }
export interface Schedule { id: string; name: string; project_id: string; project_name?: string; cron_expr: string; timezone: string; enabled: boolean; missed_run_policy: string; retry_max_attempts: number; retry_backoff_seconds: number; next_run_at: string | null; last_fired_at: string | null }
export interface LabScenario { key: string; title: string; summary: string; initial_conditions: string[]; expected: string; fault: string | null; status: "not_run" | "running" | "matched" | "mismatch"; note: string; runs: { id: string; attempt: number; state: string; failure_class: string | null; cleanup_state: string; cleanup_attempts: number }[]; multi_run: boolean }
