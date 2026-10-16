"""Sprint 4 task 7: the skill packaging and its ordering contract (architecture §5.11)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from tests.conftest import ROOT


def frontmatter(path):
    text = path.read_text()
    assert text.startswith("---\n")
    head, _, body = text[4:].partition("\n---\n")
    return yaml.safe_load(head), body


def test_frontmatter_names_and_one_line_descriptions():
    for sub, name in (("ask", "agent-bi-ask"), ("enrich", "agent-bi-enrich")):
        meta, _ = frontmatter(ROOT / "skills" / sub / "SKILL.md")
        assert meta["name"] == name
        assert set(meta) == {"name", "description"} and "\n" not in meta["description"].strip()


def test_claude_skills_are_relative_symlinks_to_the_canonical_text():
    for link, target in (("agent-bi-ask", "../../skills/ask"), ("agent-bi-enrich", "../../skills/enrich")):
        path = ROOT / ".claude" / "skills" / link
        assert path.is_symlink() and os.readlink(path) == target
        assert (path / "SKILL.md").is_file()


def test_the_ask_skill_tries_the_question_path_before_composing():
    _, body = frontmatter(ROOT / "skills" / "ask" / "SKILL.md")
    assert body.index("--question") < body.index("Step 2") < body.index("--query-file out/question.json")
    assert "Step 1 - always first" in body and "E_NO_VERIFIED_MATCH" in body
    assert "E_UNKNOWN_FIELD" in body and "once" in body
    assert "must not write SQL" in " ".join(body.split())


def test_the_enrich_skill_forbids_sql_in_a_patch():
    _, body = frontmatter(ROOT / "skills" / "enrich" / "SKILL.md")
    assert "carries no SQL" in body and "propose_query" in body


def squash(t: str) -> str:
    """Line wrapping is not content."""
    return " ".join(t.split())


def _section(arch_path: Path, start_marker: str, end_marker: str) -> str:
    text = arch_path.read_text()
    start = text.index(start_marker)
    return text[start:text.index(end_marker, start)]


def _515_grammar(arch_path: Path = ROOT / "architecture.md") -> str:
    """The normative grammar of section 5.15 (rules 1-11), extracted at test time."""
    return _section(arch_path, "**Grammar (exact).**", "**Output**")


def _511_flow(arch_path: Path = ROOT / "architecture.md") -> str:
    return _section(arch_path, "### 5.11 ", "## 6. CLI")


def check_skill_delegates(
    skill_path: Path = ROOT / "skills" / "ask" / "SKILL.md",
    arch_path: Path = ROOT / "architecture.md",
) -> None:
    """Sprint 4 task 9 (D402/D419): the skill calls `agent-bi resolve`, branches on its exits as
    section 5.11 says, and carries neither the function-word list nor any grammar rule text of
    section 5.15. The 5.15 strings are extracted from architecture.md at call time."""
    import re

    skill = skill_path.read_text()
    flat = squash(skill)
    # it invokes the resolver, after the verified path, on the verbatim question
    assert "agent-bi resolve --model <m> --question" in flat
    assert "agent-bi ask --model <m> --question" in flat
    assert flat.index("agent-bi ask --model <m> --question") < flat.index("agent-bi resolve --model <m> --question")
    # the branches section 5.11 names: 0 -> ask out/resolution.json; 5 -> user picks, --choose; 2 -> drop
    # picks; 6 -> only now compose out/question.json (retry once on E_UNKNOWN_FIELD)
    for needle in (
        "**Exit 0, `RESOLVED`**", "agent-bi ask --model <m> --query-file out/resolution.json",
        "**Exit 5, `AMBIGUOUS`**", "--choose <id>=<their pick>", "**Never pick for the user.**",
        "`E_BAD_CHOICE`", "drop every pick for each id the error line names",
        "**Exit 6, `UNRESOLVED`**", "Only now may you compose", "out/question.json",
        "`E_UNKNOWN_FIELD`", "retry **once**",
    ):
        assert needle in flat, needle
    # the composing instruction comes only inside the exit-6 branch: nothing before it says to compose
    assert flat.index("Only now may you compose") < flat.index("out/question.json")
    assert flat.index("**Exit 6, `UNRESOLVED`**") < flat.index("out/question.json")
    # no function-word list: neither the exact list from 5.15 nor most of its words in a row
    grammar = _515_grammar(arch_path)
    words = re.search(r"\(closed\): `([^`]+)`", squash(grammar)).group(1).split()
    assert len(words) >= 20
    assert " ".join(words) not in flat and " ".join(words[:8]) not in flat
    # no grammar rule text: no run of 8 consecutive words of 5.15's grammar occurs in the skill
    toks = re.findall(r"[a-z0-9`_<>=/.-]+", squash(grammar).lower())
    skill_toks = " ".join(re.findall(r"[a-z0-9`_<>=/.-]+", flat.lower()))
    for i in range(len(toks) - 7):
        gram = " ".join(toks[i:i + 8])
        assert gram not in skill_toks, f"skill repeats 5.15 grammar text: {gram}"
    # the rule headings of 5.15 are not reproduced either
    for heading in re.findall(r"^\d+\. (\*\*[^*]+\*\*)", grammar, re.MULTILINE):
        assert heading not in skill, heading
    # and the flow the skill follows is the one 5.11 states: the same exit codes are named there
    flow = squash(_511_flow(arch_path))
    for exit_text in ("Exit 0 `RESOLVED`", "Exit 5 `AMBIGUOUS`", "Exit 6 `UNRESOLVED`", "`E_BAD_CHOICE`"):
        assert exit_text in flow


def test_skill_delegates_resolution():
    check_skill_delegates()
