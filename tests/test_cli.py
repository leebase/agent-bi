from __future__ import annotations

import pytest

from agent_bi.main import build_parser, main

# Every architecture §6 command.
COMMANDS = [
    "scan", "validate", "compile-rill", "compile-sql", "verify", "parity", "ask", "save",
    "canvas fork", "enrich", "diff", "contract", "resolve", "init", "snowflake-load",
]

UNIMPLEMENTED = {
    "parity": ["--model", "m"],
    "snowflake-load": ["--model", "m"],
}


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for command in COMMANDS:
        assert command in out, command


def test_command_set_is_exactly_section_6():
    parser = build_parser()
    registered = next(a for a in parser._actions if a.dest == "command").choices
    assert set(registered) == {c.split()[0] for c in COMMANDS}


@pytest.mark.parametrize("command", sorted(UNIMPLEMENTED))
def test_unimplemented_commands_exit_2_with_e_not_implemented(command, capsys):
    rc = main([*command.split(), *UNIMPLEMENTED[command]])
    assert rc == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert err[-1].startswith("E_NOT_IMPLEMENTED: ")
