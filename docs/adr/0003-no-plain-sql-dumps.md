# ADR 0003: Plain-SQL dumps are unsupported

**Context:** a plain dump is a script for `psql`, whose meta-commands include shell escapes; pg_dump 16.15 itself emits `\restrict` lines because of this class of issue. Scanning scripts for dangerous lines invites parser differentials.
**Decision:** refuse plain SQL and unrecognised formats at registration and again before restore (`FORMAT_UNSUPPORTED`). Support `pg_restore`-driven archives only, where SQL is sent over a connection to the unprivileged role.
**Consequences:** teams using plain dumps must switch to `-Fc` or `-Ft`. The Recovery Lab demonstrates the refusal on a real plain dump.
