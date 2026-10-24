# Recovery engine reference

## Supported inputs
* `pg_dump -Fc` (custom) and `-Ft` (tar) archives written by pg_dump of the same or an older major version than the restore tooling.
  Tested with PostgreSQL **16.15** only. The shipped image restores into PostgreSQL 16.
* **Plain-SQL dumps are refused** (ADR 0003): they are scripts for `psql`, which supports shell escapes. Nothing in a backup is executed.
* File-size limit `AFTERSTATE_MAX_ARTIFACT_BYTES`, enforced while streaming the upload and again before restore.

## Artifact verification (6 checks, all required)
exists and within size limit; SHA-256 equals the value recorded at registration; format supported; table of contents readable
(`pg_restore --list`); server version compatible; archive readable end to end (`pg_restore -f /dev/null`, no database involved).
A `pg_restore` crash (segmentation fault on a malformed table of contents was observed) is classified `ARTIFACT_CORRUPT`.

## Restore
`pg_restore --no-owner --no-privileges --exit-on-error --verbose --use-list <policy-filtered TOC>` as role `afterstate_restore`
(NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS). Objects skipped by policy: event triggers, publications,
subscriptions, foreign data wrappers/servers/tables, user mappings, procedural languages, ACLs, and `plpgsql` extension entries.
Progress is the count of real `pg_restore` events against the number of selected TOC entries. Timeout, disk cap
(`AFTERSTATE_MAX_ENVIRONMENT_BYTES`) and cancellation kill the whole process group.

## Verification rules
| Kind | Stage | Coverage | Outcome rules |
|---|---|---|---|
| schema, table, column, constraint, index, extension | structural | complete | FAIL when absent or different |
| row_count | data | partial | FAIL outside exact/min/max; missing table is FAIL |
| sql_assertion | data | partial | `true` / `no_rows` / `equals` / `between`; any SQL error is **INCONCLUSIVE** |
| dataset_checksum | data | **complete** | SHA-256 over `row_to_json` of every row, in query order |
| recovery_point | data | partial | newest timestamp in the data; feeds RPO |
| http_check | application | n/a | against the predefined suite (`reference-shop`), else SKIPPED |

User SQL is checked lexically (single read-only statement, no DML/DDL/side-effect functions), then executed as the non-superuser
`afterstate_validator` in a `READ ONLY` transaction with a statement timeout. The validator session ignores database-level setting
overrides a hostile backup could install (`tests/integration/test_environment_security.py`).
Compute `expected_sha256` for a rule from the source database with `afterstate dataset-checksum` (same algorithm).

## Verdict
`SUCCEEDED` only if no required check failed, was skipped or inconclusive **and** each configured objective is MET.

| Situation | State | failure_class |
|---|---|---|
| execution failure (artifact, environment, restore, timeout, resource) | FAILED | the specific class |
| required check FAIL | FAILED | VALIDATION_FAILED |
| required check SKIPPED or INCONCLUSIVE | NEEDS_ATTENTION | VALIDATION_INCONCLUSIVE |
| objective MISSED / UNKNOWN | NEEDS_ATTENTION | OBJECTIVE_MISSED / OBJECTIVE_UNKNOWN |
| cancelled | CANCELLED | CANCELLED |
| optional check not PASS | SUCCEEDED, listed as a warning | none |

Retryable (queued again within the project's attempt limit): `ENVIRONMENT_UNAVAILABLE`, `WORKER_INTERRUPTED`, `INTERNAL_ERROR`.
Corruption, bad configuration and missed objectives are never retried: retrying would only repeat the verdict.

## Objectives and metrics
* **RTO** = artifact verification + provisioning + restore + validation, from a monotonic clock. Cleanup and report generation are excluded.
* **RPO** is never fabricated. With a recovery-point probe: `test start - newest recovered timestamp`. Without one, a declared backup time
  *proves a miss* if the backup is older than the target (data written since is lost) but cannot prove compliance; otherwise **UNKNOWN**.
* Also recorded per run: backup size, restore seconds and throughput (MiB/s), restored database bytes, validation seconds, stage durations,
  check counts, cleanup seconds, archive items done/total.

## Recovery Lab
Ten scenarios on real `pg_dump` archives: successful restore, corrupted backup, unsupported format, missing schema, failed invariant,
restore timeout, interrupted worker (a real child process is `SIGKILL`ed mid-restore), unavailable database (the restored PostgreSQL is
`SIGKILL`ed), validation query failure, cleanup failure (first cleanup attempt fails). Faults are honoured only for runs created by the Lab.
Reset deletes only rows carrying that scenario's `lab_scenario` marker in that workspace.
