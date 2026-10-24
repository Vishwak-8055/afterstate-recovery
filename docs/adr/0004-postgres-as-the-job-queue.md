# ADR 0004: PostgreSQL is the job queue; leases, fencing and a reaper

**Options:** Redis/Celery, Kafka, a hosted queue, or the platform database. **Decision:** the platform database. Claims use `SELECT ... FOR UPDATE SKIP LOCKED`, a per-workspace advisory lock for the concurrency limit, a heartbeat lease, and `lease_owner` fencing on every write. A reaper settles runs whose lease expired; cleanup tasks are written before resources exist. A partial unique index allows one queued-or-active run per project.
**Why:** no extra infrastructure to run or lose; state transitions and the queue commit atomically; interrupted runs are visible and recoverable from the same store. Throughput needs here are low (restores take seconds to hours).
**Consequences:** vertical limits of one PostgreSQL; a worker executes one run at a time, so concurrency comes from more worker processes.
