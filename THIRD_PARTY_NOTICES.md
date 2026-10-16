# Third-party references

Agent BI's original source, documentation and synthetic demonstration fixtures are
licensed under Apache-2.0. Dependencies and vendor material retain their own licenses
and rights; this project's license does not relicense them.

- **Rill Developer** is a separate dashboard runtime, distributed by
  [Rill Data](https://github.com/rilldata/rill). The tested version is **v0.90.5**.
  Its code and license are maintained upstream; no Rill runtime is vendored here.
- **DuckDB, PyYAML and Pydantic** are installed as Python dependencies. Consult their
  upstream distributions for license notices.
- The indented `revenue_analysis` YAML example in
  `docs/pinned/snowflake-semantic-view-yaml-spec.md` is a functional interoperability
  test fixture from [Snowflake's semantic-view YAML reference](https://docs.snowflake.com/en/user-guide/views-semantic/semantic-view-yaml-spec),
  retrieved 2026-10-14. Attribution: Snowflake. That vendor example retains its original
  rights and is excluded from the Apache grant on Agent BI's original material.
  Other files in `docs/pinned/` provide upstream reference pointers rather than copies
  of vendor documentation.
- Vega-Lite schema URLs identify the chart specification format; the schema and vendor
  runtimes are maintained by their respective projects.

Rill and Snowflake names are used to identify interoperability. No affiliation or
endorsement is claimed.
