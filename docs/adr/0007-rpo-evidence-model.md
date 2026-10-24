# ADR 0007: RPO is evidence-based and may be unknown

**Context:** data-loss claims are easy to fabricate. **Decision:** RPO is computed only from (1) a recovery-point probe, the newest timestamp found in the restored data, or (2) a declared backup time, which can prove a miss (data after the backup is lost) but cannot prove compliance. Otherwise the status is `UNKNOWN`, and a configured-but-unknown objective yields `NEEDS_ATTENTION`. **Consequences:** teams get an honest answer and a concrete fix (add a probe or declare the backup time); WAL/PITR evidence is future work.
