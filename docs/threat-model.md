# Threat model

**Assets:** backup contents (often sensitive), platform credentials and sessions, the integrity of verdicts and evidence, the host running restores.
**Principle:** a backup is untrusted data. It may contain malicious SQL, malformed structures, or personal data.

## Trust boundaries
1. Browser to API: authenticated by cookie; every mutation needs a CSRF token and an acceptable Origin.
2. API to database/storage: trusted internal network.
3. Worker to **restore environment**: the boundary that matters. Everything inside is attacker-influenced.
4. Reports and logs to the UI: attacker-influenced strings (error text that echoes row data) must not leak or execute.

## Threats and controls
| Threat | Control | Verified by |
|---|---|---|
| SQL in a backup runs programs or reads server files (`COPY FROM PROGRAM`, `pg_read_file`) | Restore as an **unprivileged** role; superuser is never used for restore or validation | `test_the_restore_role_cannot_escape_the_database` (11 probes) |
| Backup installs untrusted extensions, event triggers, languages | Role cannot; TOC policy also skips those object classes | same suite, `test_toc_policy_*` |
| Plain-SQL dump executes `psql` meta-commands | Format refused (ADR 0003) | `test_plain_sql_dump_is_rejected_as_unsupported_and_never_executed` |
| Malformed archive crashes `pg_restore` | Crash is classified as a corrupt artifact; core dumps disabled; process group killed | `test_toc_corruption_that_crashes_pg_restore_is_classified_corrupt` |
| Restored database is reachable from the network | `listen_addresses=''`, unix socket in a 0700 directory, `pg_hba` rejects TCP | `test_the_environment_has_no_network_listener_and_a_private_directory` |
| Backup alters session behaviour to fool validation (`ALTER DATABASE ... SET`, function shadowing) | Validator uses startup options that outrank database settings; `pg_catalog` resolves first | `test_a_hostile_backup_cannot_change_how_the_validator_session_behaves`, `..._shadow_builtins...` |
| User validation SQL writes or escapes | Lexical gate + non-superuser + `READ ONLY` + timeout | `tests/unit/test_security.py` (35 cases), integration suite |
| Cross-tenant access by guessing IDs | Every query scoped by workspace; non-members get 404 | `test_workspaces_are_isolated_even_when_ids_are_guessed` (mutation-tested) |
| Privilege escalation inside a workspace | Server-side RBAC; denials audited | `test_roles_are_enforced_on_the_server_and_denials_are_audited` |
| CSRF | Token compare + Origin check | `test_mutations_require_a_valid_csrf_token` (mutation-tested) |
| Credential stuffing, account enumeration | argon2id, per-account and per-IP limits, identical error and timing for unknown users | `test_login_attempts_are_rate_limited_per_account`, `test_bad_credentials_...` |
| Secrets or personal data in logs, events, reports | Pattern and exact-value redaction incl. PostgreSQL row-echo errors | `tests/unit/test_security.py` redaction cases |
| Tampered evidence | SHA-256 recorded; reports refused if the bytes changed | `test_end_to_end_...` (tamper step) |
| Worker crash leaks environments | Cleanup task recorded before creation; reaper + verified teardown; teardown refuses unowned paths | `test_worker_killed_...`, `test_cleanup_refuses_to_delete_paths_it_does_not_own` |
| Zombie worker overwrites a recovered run | Lease fencing on every write | `test_stale_owner_cannot_write_after_losing_its_lease` |
| SSRF through webhooks | http(s) only, no redirects, resolved addresses must be public | `test_webhook_urls_cannot_target_internal_addresses...` |
| Oversized uploads / bodies | Streaming size limit, JSON body limit, 413 | `test_upload_size_limit_is_enforced_while_streaming` |
| Fault injection abused | Faults honoured only for Lab runs; ignored otherwise | `test_faults_are_ignored_for_runs_that_are_not_lab_runs` |

## Residual risks (not eliminated)
* **Process-level, not kernel-level, isolation.** Restore environments are unprivileged processes and directories, not namespaces or VMs. A memory-safety bug in `pg_restore` or PostgreSQL triggered by a hostile archive is bounded by the container (non-root, read-only root, no capabilities, no network) but not prevented. Run workers on hosts you can afford to lose and keep them off the production network.
* **Resource limits are partial.** Timeout, disk cap and process-group kill are enforced by the engine. CPU and memory ceilings are delegated to container limits.
* **Webhook DNS rebinding.** The address check happens before the request; use egress filtering for strict guarantees.
* **Rate limiting is per API process.** Use the reverse proxy for global limits.
* **Orphaned SysV shared-memory interlock** can remain after a `SIGKILL` of PostgreSQL (tiny, cleared on reboot).
* **No MFA or SSO** in this release.
* A backup can legitimately contain data that makes verification slow (large tables); budgets bound it, they do not make it cheap.
