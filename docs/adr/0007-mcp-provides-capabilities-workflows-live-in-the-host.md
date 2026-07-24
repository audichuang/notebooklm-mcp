# MCP provides capabilities; workflows live in the host

The MCP contract provides durable generation attempts, atomic state, reconciliation, idempotent finalize and Feedback Source operations, resumable series generation, media preparation, publication, and optional QA helpers. It does not require human approvals, stop unattended series between episodes, or encode one Audicast operating policy; those choices belong to the skill or host.

Core reliability work therefore prioritizes one Manifest Store, attempt history, safe handling of unknown outcomes, checkpointed external side effects, automatic series resume, common artifact recovery, and honest status reporting. Optional QA tools may return facts, transcripts, clips, evidence, and validation results without turning them into mandatory MCP lifecycle states.
