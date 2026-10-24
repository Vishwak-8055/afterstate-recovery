# ADR 0001: Restore into disposable local PostgreSQL clusters, not Docker-in-Docker

**Status:** accepted. **Context:** each recovery needs an isolated, disposable PostgreSQL with minimal privileges and no network.
**Options:** (a) spawn Docker containers per run, which requires the worker to hold the Docker socket (root on the host); (b) Docker-in-Docker (privileged); (c) disposable clusters started by the worker as an unprivileged user.
**Decision:** (c). The worker image contains the PostgreSQL server binaries. Each run gets a 0700 directory, a cluster with `listen_addresses=''` and a unix socket inside that directory, an unprivileged restore role and a read-only validator role. The worker container itself is non-root, read-only, capability-less and network-isolated.
**Consequences:** no docker.sock anywhere; isolation is process-level (see threat model) and can be strengthened later behind the same `RestoreEnvironment` shape (for example gVisor/Firecracker). Verifiable without Docker, which is how it was tested.
