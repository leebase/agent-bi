"""``agent-bi`` command line (architecture §6, §9).

Sprint 1 implements ``scan`` and ``validate``; Sprint 2 adds ``compile-sql``, ``verify`` and
``contract``; Sprint 3 adds ``compile-rill``, ``canvas fork``, ``init`` and ``verify --engine
rill``; Sprint 4R adds ``resolve`` (§5.15); every other §6 command is registered and exits 2
with ``E_NOT_IMPLEMENTED``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from agent_bi.config import CONFIG_FILENAME, load_config
from agent_bi.engines import open_engine
from agent_bi.errors import AgentBiError, validation_error
from agent_bi.version import __version__


def _project_dir(args: argparse.Namespace) -> Path:
    return Path(args.config).resolve().parent


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def cmd_scan(args: argparse.Namespace) -> int:
    from agent_bi.semantic.scan import scan

    config = load_config(args.config)
    engine = open_engine(args.engine, config)
    try:
        model_path, sidecar_path = scan(
            engine,
            args.relation,
            args.model,
            _project_dir(args) / "semantic",
            refresh=args.refresh,
        )
    finally:
        engine.close()
    for written in (model_path, sidecar_path):
        print(f"wrote {_display(written)}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from agent_bi.explore.components import component_warnings
    from agent_bi.semantic.validate import validate

    validate(args.model, _project_dir(args) / "semantic")
    try:
        rill_dir = load_config(args.config).rill.project_dir
    except AgentBiError:  # validate needs no config; the components live under rill/ by default
        rill_dir = "rill"
    for warning in component_warnings(_project_dir(args), rill_dir, args.model):
        print(warning)  # a warning: exit stays 0
    print(f"OK: {args.model}")
    return 0


def cmd_compile_rill(args: argparse.Namespace) -> int:
    from agent_bi.compile_rill import compile_rill

    published, manifest = compile_rill(args.model, load_config(args.config), _project_dir(args))
    for path in published:
        print(f"wrote {path}")
    print(f"manifest {manifest}")
    return 0


def cmd_canvas_fork(args: argparse.Namespace) -> int:
    from agent_bi.canvas import fork_canvas

    rel = fork_canvas(args.model, args.name, load_config(args.config), _project_dir(args))
    print(f"wrote {rel}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from agent_bi.initialize import run_init

    result = run_init(
        Path(args.config), sqlite_arg=args.sqlite, table=args.table, model_name=args.model,
        rill_model=args.rill_model,
    )
    print(f"attach: {'reused' if result.reused_attach else 'added'} alias {result.alias!r} for {result.source}")
    print(f"scan: {'created' if result.created else 'refreshed'} semantic/{args.model or args.rill_model}.yaml")
    for path in result.published:
        print(f"wrote {path}")
    print(f"manifest {result.manifest}")
    print(f"Next: rill start {result.rill_dir} --no-open")
    return 0


def cmd_compile_sql(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from agent_bi.query.compile import compile as compile_query
    from agent_bi.query.compile import executed_sql
    from agent_bi.query.contract import parse_query
    from agent_bi.query.files import load_query_file
    from agent_bi.semantic.model import load_model_dict
    from agent_bi.semantic.scan import paths_for
    from agent_bi.semantic.validate import validate

    semantic_dir = _project_dir(args) / "semantic"
    validate(args.model, semantic_dir)
    model = load_model_dict(str(paths_for(semantic_dir, args.model)[0]))
    query = parse_query(load_query_file(args.query_file), args.query_file)
    relation_map = None
    if args.engine == "duckdb":
        relation_map = load_config(args.config).engines.duckdb.relation_map
    compiled = compile_query(query, model, args.engine, relation_map=relation_map)
    sql = compiled.compiled_sql
    if args.executed:
        as_of = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if args.engine == "snowflake" else None
        sql = executed_sql(compiled, args.engine, as_of)
    print(sql)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from agent_bi.verify.known_answers import verify

    result = verify(
        args.model, args.engine, load_config(args.config), _project_dir(args),
        coverage=args.coverage,
    )
    for line in result.lines:
        print(line)
    if result.error is not None:
        print(str(result.error), file=sys.stderr)
    return result.exit_code


def cmd_status(args: argparse.Namespace) -> int:
    """Read-only model report: the Definition and Serving lines (§4.8.4). Reads receipts
    from ``out/serving/<model>/`` and never calls Rill."""
    from agent_bi.rill_models import current_models_digest
    from agent_bi.semantic.model import load_model_dict
    from agent_bi.semantic.provenance import load_sidecar_dict
    from agent_bi.semantic.scan import paths_for
    from agent_bi.semantic.validate import validate
    from agent_bi.verify import serving

    config = load_config(args.config)
    project_dir = _project_dir(args)
    semantic_dir = project_dir / "semantic"
    validate(args.model, semantic_dir)
    model_path, sidecar_path = paths_for(semantic_dir, args.model)
    model = load_model_dict(str(model_path))
    sidecar = load_sidecar_dict(sidecar_path)
    models_digest = current_models_digest(
        config.base_dir / config.rill.project_dir, model, config.engines.duckdb.rill_models
    )
    definition_line, serving_lines = serving.model_report(
        project_dir, args.model, model, sidecar, models_digest
    )
    print(f"Definition: {definition_line}")
    print(f"Serving:    {serving_lines[0]}")
    for line in serving_lines[1:]:
        print(f"            {line}")
    return 0


def cmd_contract(args: argparse.Namespace) -> int:
    from agent_bi.query.contract_doc import contract_doc

    if not args.print_:
        raise validation_error("QUERY_INVALID", "`contract` needs --print")
    sys.stdout.write(contract_doc())
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from agent_bi.explore.ask import run_ask

    answer = run_ask(
        args.model, load_config(args.config), _project_dir(args),
        query_file=args.query_file, question=args.question, engine_name=args.engine,
        mark=args.mark, theme=args.theme,
    )
    if args.json:
        print(json.dumps(answer.envelope, indent=2, ensure_ascii=False))
    else:
        print(answer.text)
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    from agent_bi.errors import EXIT_AMBIGUOUS, EXIT_UNRESOLVED, AgentBiError
    from agent_bi.explore.resolve import run_resolve

    doc, text = run_resolve(args.model, args.question, args.choose, _project_dir(args))
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BaseException:  # D417: a failed stdout write leaves no result artifact from this call
        (_project_dir(args) / "out" / "resolution.json").unlink(missing_ok=True)
        raise
    if doc["status"] == "AMBIGUOUS":
        ids = ", ".join(a["id"] for a in doc["ambiguities"])
        raise AgentBiError(
            "RESOLVE_AMBIGUOUS",
            f"more than one valid binding for: {ids}; re-run with --choose <id>=<value>",
            EXIT_AMBIGUOUS,
        )
    if doc["status"] == "UNRESOLVED":
        why = "; ".join(f"{u['word']!r}: {u['reason']}" if u["word"] else u["reason"] for u in doc["unresolved"])
        raise AgentBiError(
            "RESOLVE_UNRESOLVED", f"outside the closed grammar ({why})", EXIT_UNRESOLVED
        )
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    if args.apply is not None:
        from agent_bi.semantic.patch import apply_patch

        for path in apply_patch(
            args.model, args.apply, args.allow_confirmed, _project_dir(args)
        ):
            print(f"wrote {path}")
        return 0
    from agent_bi.semantic.enrich import accept

    print(accept(args.model, args.accept, args.by, _project_dir(args)))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from agent_bi.semantic.diff import run_diff

    result = run_diff(args.model, args.engine, load_config(args.config), _project_dir(args))
    print("\n".join(result.lines))
    return result.exit_code


def cmd_save(args: argparse.Namespace) -> int:
    from agent_bi.explore.save import run_save

    published = run_save(
        args.from_, args.name, load_config(args.config), _project_dir(args),
        question=args.question, force=args.force, verify=args.verify, by=args.by,
        expected_sql=args.expected_sql, add_to_canvas=args.add_to_canvas,
    )
    for path in published:
        print(f"wrote {path}")
    return 0


def _not_implemented(name: str) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        raise validation_error("NOT_IMPLEMENTED", f"`{name}` is not implemented yet")

    return run


def _add(sub, name: str, help_: str, func, configure=None):
    p = sub.add_parser(name, help=help_, description=help_)
    if configure:
        configure(p)
    p.set_defaults(func=func)
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bi",
        description="Table-to-dashboard-and-chat tool in which no LLM produces a number.",
    )
    parser.add_argument("--version", action="version", version=f"agent-bi {__version__}")
    parser.add_argument(
        "--config", default=CONFIG_FILENAME, help="project config (default: ./agent-bi.yaml)"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    engines = ("duckdb", "snowflake")
    ni = _not_implemented

    def scan_args(p):
        p.add_argument("--engine", choices=engines, required=True)
        p.add_argument("--relation", required=True, help="DATABASE.SCHEMA.NAME")
        p.add_argument("--model", required=True)
        p.add_argument("--refresh", action="store_true", help="merge into an existing model")

    def model_arg(p):
        p.add_argument("--model", required=True)

    def compile_sql_args(p):
        model_arg(p)
        p.add_argument("--query-file", required=True)
        p.add_argument("--engine", choices=(*engines, "rill"), required=True)
        p.add_argument("--executed", action="store_true")

    def verify_args(p):
        model_arg(p)
        p.add_argument("--engine", choices=(*engines, "rill"), default="duckdb")
        p.add_argument("--coverage", action="store_true")

    def ask_args(p):
        model_arg(p)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--query-file")
        g.add_argument("--question")
        p.add_argument("--engine", choices=engines)
        p.add_argument("--mark")
        p.add_argument("--theme")
        p.add_argument("--json", action="store_true")

    def save_args(p):
        p.add_argument("--from", dest="from_", required=True)
        p.add_argument("--name", required=True)
        p.add_argument("--question")
        p.add_argument("--force", action="store_true")
        p.add_argument("--verify", action="store_true")
        p.add_argument("--by")
        p.add_argument("--expected-sql")
        p.add_argument("--add-to-canvas")

    def resolve_args(p):
        model_arg(p)
        p.add_argument("--question", required=True)
        p.add_argument("--choose", action="append", default=[], metavar="ID=VALUE")

    def enrich_args(p):
        model_arg(p)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--apply")
        g.add_argument("--accept")
        p.add_argument("--allow-confirmed", action="append", default=[])
        p.add_argument("--by")

    def diff_args(p):
        model_arg(p)
        p.add_argument("--engine", choices=engines, required=True)

    def init_args(p):
        form = p.add_mutually_exclusive_group(required=True)
        form.add_argument("--sqlite")
        form.add_argument("--rill-model", dest="rill_model")
        p.add_argument("--table")
        p.add_argument("--model")

    _add(sub, "scan", "Discover a relation and write the semantic model + sidecar", cmd_scan, scan_args)
    _add(sub, "validate", "Check the subset, grammars and sidecar consistency", cmd_validate, model_arg)
    _add(sub, "compile-rill", "Write the Rill project files", cmd_compile_rill, model_arg)
    _add(sub, "compile-sql", "Print the compiled SQL for a SemanticQuery", cmd_compile_sql, compile_sql_args)
    _add(sub, "verify", "Run the known-answer checks", cmd_verify, verify_args)
    _add(sub, "status", "Show the model's Definition and Serving lines (read-only)", cmd_status, model_arg)
    _add(sub, "parity", "Compare the four verification routes", ni("parity"), model_arg)
    _add(sub, "ask", "Answer a SemanticQuery or a question", cmd_ask, ask_args)
    _add(sub, "save", "Promote an answer into a saved query and chart", cmd_save, save_args)

    canvas = sub.add_parser(
        "canvas",
        help="`canvas fork`: copy the default canvas to an operator-owned canvas",
        description="Rill canvas operations",
    )
    canvas_sub = canvas.add_subparsers(dest="canvas_command", metavar="<subcommand>", required=True)

    def fork_args(p):
        model_arg(p)
        p.add_argument("--name", required=True)

    _add(canvas_sub, "fork", "Copy the default canvas to an operator-owned canvas", cmd_canvas_fork, fork_args)
    _add(sub, "enrich", "Apply an enrichment patch or accept an entry", cmd_enrich, enrich_args)
    _add(sub, "diff", "Report drift between the catalog and the model", cmd_diff, diff_args)
    _add(
        sub, "contract", "Print the SemanticQuery contract", cmd_contract,
        lambda p: p.add_argument("--print", dest="print_", action="store_true"),
    )
    _add(
        sub, "resolve", "Deterministically map a question onto semantic objects, or refuse",
        cmd_resolve, resolve_args,
    )
    _add(sub, "init", "Scan, validate and compile a Rill project from a SQLite table or a registered Rill model", cmd_init, init_args)
    _add(sub, "snowflake-load", "Create the Semantic View in Snowflake", ni("snowflake-load"), model_arg)
    return parser


def _drop_resolution_after_usage_error(argv: Sequence[str]) -> None:
    """D417: an invalid argument to ``resolve`` is a non-successful invocation, so it removes any
    existing ``out/resolution.json`` (next to the ``--config`` project, else the default one) like
    every other failure. The location is found by a tolerant scan, so a dangling ``--config``
    (no value) does not stop it: that token is ignored and the default config path applies."""
    config, command, i = CONFIG_FILENAME, None, 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--config":
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                config = argv[i + 1]
                i += 1
        elif tok.startswith("--config="):
            config = tok.split("=", 1)[1] or config
        elif not tok.startswith("-") and command is None:
            command = tok
        i += 1
    if command == "resolve":
        (Path(config).resolve().parent / "out" / "resolution.json").unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(raw)
    except SystemExit as exc:
        if exc.code not in (0, None):  # --help / --version exit 0 and resolve nothing
            _drop_resolution_after_usage_error(raw)
        raise
    try:
        return args.func(args)
    except AgentBiError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
