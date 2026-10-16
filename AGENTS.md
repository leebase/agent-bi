# Agent BI contributor guide

Read README.md, CONTRIBUTING.md and the relevant contracts in architecture.md before
editing. Version 0.1.0 supports the M1 scope documented in docs/user-guide.md; later
interfaces described in architecture.md are not implemented merely because they exist
in CLI help. Do not implement roadmap work without an explicit assignment.

Preserve the accepted resolver grammar in architecture.md §5.15 and its frozen cases.
Never rewrite the contract to make a failing test pass. Report ambiguity to the owner.

Use temporary projects for mutations. Never accept committed example definitions under
an agent identity, fabricate known answers, call an LLM from src/agent_bi, or silently
discard unsupported input. Report checks with their actual exit codes.

Run pytest -q and ruff check src tests for code changes; review the final delta and
reproduce documented examples for user-facing changes. Scope review to the assigned
work. Do not publish, deploy or push without the user's authorization.
