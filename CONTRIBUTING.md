# Contributing

Use Python 3.12+ and a source checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
ruff check src tests
python -m build
```

Tests use synthetic local fixtures; the unit suite does not require a Snowflake account
or running Rill. Rill integration is checked separately with the README workflow.
Do not overwrite the committed fixtures or accept definitions on the committed example
models during tests; use a scratch checkout or temporary project.

Preserve these contracts:

- The CLI calls no LLM; every answer value is computed by an engine.
- Agents propose SemanticQuery JSON, never SQL or expected numbers.
- Known-answer verification uses an independent computation route.
- Definitions become accepted only through attributed human action.
- Resolver ambiguity is settled by user choices; unsupported qualifiers must survive
  as unresolved input.
- Refused transactions must not partially modify project content.

`architecture.md` contains detailed rules, including a frozen resolver contract read
directly by tests. A test failure is evidence, not permission to silently change that
contract or its frozen cases. Propose a contract amendment explicitly if one is needed.
Include a reproducible example and relevant checks with a pull request.

The public repository begins with a source snapshot of the accepted M1 code. Internal
engineering run logs and private commit ancestry are not part of this distribution.
The included source, tests, skills, synthetic data and normative resolver rules retain
their accepted bytes. Public documentation and packaging metadata were added for release.

Contributions are under Apache-2.0 unless explicitly stated otherwise. See LICENSE and
THIRD_PARTY_NOTICES.md.
