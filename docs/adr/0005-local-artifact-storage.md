# ADR 0005: Local filesystem artifact storage behind an interface

**Decision:** ship `LocalArtifactStorage` (streamed, size-limited, hashed on write, atomic commit, generated keys, traversal-proof) behind the `ArtifactStorage` protocol. An S3-compatible backend would download to scratch for `path_for`. **Why:** one complete, reliable workflow first; the restore tools need a local file anyway. **Consequences:** API and worker must share the volume; S3 is future work and is listed in the limitations.
