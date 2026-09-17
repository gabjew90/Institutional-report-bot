"""The Fed chair lives in world_context.py and nowhere else.

2026-09-16: a /ask answer named a "Powell press conference" three
months after the transition. world_context was built to hold the fact,
but the /ask tool docs still spelled the predecessor's name and the
/ask context never carried the chair at all. A source line that names
the predecessor must also name the chair (a filter that must pass both
is fine); anything else reads it from world_context."""
import pathlib
import re

import world_context as wc

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCOPE = ("discord_bot", "report", "github_bridge", "scheduler")


def _source_lines():
    for d in SCOPE:
        for p in (ROOT / d).rglob("*.py"):
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                yield p.relative_to(ROOT), n, line


def test_no_bare_predecessor_name_outside_world_context():
    pred = re.compile(rf"\b{wc.PREDECESSOR_NAME}\b")
    chair = re.compile(rf"\b{wc.FED_CHAIR}\b")
    bad = [f"{p}:{n}: {line.strip()}" for p, n, line in _source_lines()
           if pred.search(line) and not chair.search(line)
           and not line.lstrip().startswith("#")]
    assert not bad, "\n".join(bad)


def test_ask_tool_docs_name_the_current_chair():
    from discord_bot.tool_docs import TOOL_DOCS
    from discord_bot import ask_tools as T
    assert wc.FED_CHAIR in TOOL_DOCS["lookup_economic_calendar"]
    import inspect
    src = inspect.getsource(T)
    assert "_wc.FED_CHAIR" in src and "Powell" not in src.replace("PREDECESSOR", "")


def test_ask_runtime_header_carries_the_chair():
    from discord_bot import bot as B
    text = B._build_runtime_system_instruction()
    tail = text.split("CURRENT TIME (UTC)", 1)[1]
    assert f"FED CHAIR:             {wc.FED_CHAIR_FULL}" in tail
    assert wc.PREDECESSOR_ROLE_NOW in tail
