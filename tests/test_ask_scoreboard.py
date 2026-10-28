"""scripts/ask_scoreboard.py reads the ask-log format db.append_ask_interaction
writes (2026-10-28)."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ask_scoreboard as S  # noqa: E402

LOG = """# /ask interactions — 2026-10-27

## 2026-10-27 14:00:00 UTC

**Asker:** BK (`bankerkyle`) in #stonks

**Route:** `LOCAL/FACT` · ungrounded · 2.19s · guards: data-footer

**Q:** what are abe's open positions

**A:**

- LULU 105C 10-30 @1.07

Data: room trade ledger

**Tools called:**

| tool | args | status | result chars |
|---|---|---|---|
| lookup_trade_log | caller=abe, kind=open | ok | 304 |

<details>
<summary>🔧 Raw model output (before voice-lint / retry rewrites)</summary>

```text
- LULU 105C 10-30 @1.07
```
</details>

---

## 2026-10-27 15:00:00 UTC

**Asker:** Z in #stonks

**Route:** `WEB/FACT` · grounded ✅ (3 sources) · shape: ticker_opinion · retry-calls: 1 · 8.5s · guards: business-line, name-check:Wth, figure-provenance:stripped:1

**Q:** thoughts on MU

**A:**

Micron's memory unit drives it.

<details>
<summary>🔧 Raw model output (before voice-lint / retry rewrites)</summary>

```text
MU looks great; buy it.
```
</details>

---

## 2026-10-27 16:00:00 UTC

**Asker:** Z in #stonks

**Route:** `LOCAL/BANTER` · ungrounded · 3.0s · guards: —

**Q:** LMAOOOO

**A:**

ok — sure

<details>
<summary>🔧 Raw model output (before voice-lint / retry rewrites)</summary>

```text
ok, sure
```
</details>

---
"""


def _entries(tmp_path):
    (tmp_path / "2026-10-27.md").write_text(LOG, encoding="utf-8")
    return S.load(str(tmp_path), datetime(2026, 10, 27, tzinfo=timezone.utc))


def test_parses_route_line_tools_and_logged_shape(tmp_path):
    a, b, c = _entries(tmp_path)
    assert a["route"] == "LOCAL" and a["kind"] == "FACT" and a["latency_s"] == 2.19
    assert a["tools"] == ["lookup_trade_log"] and a["checks"] == ["data-footer"]
    assert b["shape"] == "ticker_opinion" and not b["shape_inferred"]
    assert b["grounded"] and b["retry_calls"] == 1 and b["retried"]
    assert c["checks"] == [] and c["kind"] == "BANTER"


def test_a_footer_or_punctuation_cleanup_is_not_a_change(tmp_path):
    a, b, c = _entries(tmp_path)
    assert not a["answer_changed"] and not a["punctuation_only"]
    assert b["answer_changed"]
    assert not c["answer_changed"] and c["punctuation_only"]


def test_unlogged_shape_is_inferred_and_the_catch_all_splits_by_kind(tmp_path):
    a, _, c = _entries(tmp_path)
    assert a["shape_inferred"] and c["shape_inferred"]
    assert S.group(c) == "unknown/banter"
    s = S.summarize([a, _, c])
    assert s["asks"] == 3 and s["changed"] == 1 and s["punctuation_only"] == 1
    assert s["no_check"] == 1
    assert all(e["question"] != "LMAOOOO" for e in s["unknown_examples"])


def test_check_names_drop_counts_and_trigger_words():
    assert S._base_check("name-check:Wth") == "name-check"
    assert S._base_check("figure-provenance:stripped:1") == "figure-provenance:stripped"
    assert S._base_check("validate:macro-unsourced") == "validate:macro-unsourced"


def test_render_has_both_tables(tmp_path):
    out = S.render(S.summarize(_entries(tmp_path)))
    assert "## By shape" in out and "## By check" in out and "| business-line | 1 |" in out
