# Persist attempts before remote side effects

Audicast assigns and atomically persists its own `attempt_id` before calling NotebookLM. A returned `artifact_id` is only the remote mapping for that attempt; when acceptance is uncertain, the attempt must be reconciled before any replacement attempt is allowed, trading faster blind retries for recoverability, traceability, and duplicate prevention.
