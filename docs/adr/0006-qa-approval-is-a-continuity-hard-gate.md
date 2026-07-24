---
status: superseded by ADR-0007
---

# QA approval is a continuity hard gate

This decision incorrectly forced `podcast_series` to stop for human approval between episodes. ADR-0007 supersedes it: continuity remains an idempotent, recoverable tool operation, while the skill or host may optionally place QA gates around it.
