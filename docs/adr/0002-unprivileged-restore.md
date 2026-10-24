# ADR 0002: Restore as an unprivileged role with a TOC policy

**Context:** archives are untrusted; SQL inside them executes during restore. Experiments on PostgreSQL 16 showed that a NOSUPERUSER database owner restores realistic dumps (including trusted extensions such as pgcrypto) and cannot `COPY FROM PROGRAM`, read server files, create untrusted extensions, event triggers or roles.
**Decision:** restore as `afterstate_restore` with `--no-owner --no-privileges --exit-on-error`, and comment out object classes that need superuser or reach outside the database in a `--use-list` file. A failure caused by missing privileges is reported as such, never worked around.
**Consequences:** dumps depending on superuser-only objects fail with an explanation. That is a finding about recoverability on a hardened target, not a platform error.
