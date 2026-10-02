"""scripts/quality_audit_collect.py parses the ask-log format bot.py writes."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "quality_audit_collect",
    Path(__file__).resolve().parents[1] / "scripts" / "quality_audit_collect.py")
qac = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qac)

ENTRY = """
**Asker:** Ligma (`jynx_dsl`) in #💬-stonks-yapping-💬

**Route:** `LOCAL/BANTER` · ungrounded · 12.93s · guards: rank-evidence

**Q:** [MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]
"hard to crown anyone"

[Ligma's message to you]
Gimmie top 5, who has the crown

**A:**

→ **#1, BK** (bankerkyle), 230 race-edged lines

Data: room chat tags

**Tools called:**

| tool | args | status | result chars |
|---|---|---|---|
| lookup_user_profile | metric=racism | ok | 1635 |

<details>
<summary>📋 Full prompt</summary>

```text
WHO'S TALKING (background on people active in this conversation):
- **Ligma** (jynx_dsl, <@689991230477697103>; also called: mai) — _racism-rank #6/30_:
**Ligma (jynx_dsl) — 1255 msgs**
[YOU said earlier to this asker, re: 'who's the most gay in chat?']: hard to crown anyone
```
</details>
"""


def test_parses_a_real_shaped_entry():
    e = qac._parse_entry("2026-10-01 19:40:50", ENTRY)
    assert e["asker_username"] == "jynx_dsl"
    assert e["channel"] == "💬-stonks-yapping-💬"
    assert e["route"].startswith("`LOCAL/BANTER`")
    assert e["question"].endswith("Gimmie top 5, who has the crown")
    assert "230 race-edged lines" in e["answer"]
    assert "lookup_user_profile | metric=racism" in e["answer"]
    assert len(e["whos_talking"]) == 1 and "jynx_dsl" in e["whos_talking"][0]
    assert e["you_said_earlier"][0].startswith("[YOU said earlier")


def test_entry_headers_split_a_day_file():
    text = "# log\n\n## 2026-10-01 14:00:45 UTC\n" + ENTRY + "\n---\n\n## 2026-10-01 20:44:11 UTC\n" + ENTRY
    marks = list(qac._ENTRY_RE.finditer(text))
    assert [m.group(1) for m in marks] == ["2026-10-01 14:00:45", "2026-10-01 20:44:11"]


def test_timestamp_formats():
    t = qac._utc("2026-10-01 19:40:50 UTC")
    assert qac._iso(t) == "2026-10-01T19:40:50"
    assert qac._sp(t) == "2026-10-01 19:40:50"
    assert qac._utc("2026-10-01T19:00:02Z") == qac._utc("2026-10-01 19:00:02")
