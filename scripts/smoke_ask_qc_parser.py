"""Smoke test for ask_qc.parser.parse_ask_log.

Covers:
  - per-interaction split on '## YYYY-MM-DD HH:MM:SS UTC' headers
  - asker label / username / channel extraction
  - question + answer body extraction (multiline)
  - <details> block extraction (when present)
  - <details> block = None for legacy entries (no block)
  - malformed blocks silently skipped (graceful, not raised)
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Inline fixture so this smoke runs without external files. Mirrors
# the real format observed in .tmp_ask_log_today.md.
SAMPLE_LOG = """# /ask interactions - 2026-06-01

## 2026-06-01 14:30:00 UTC

**Asker:** kloh (`kloh.`) in #stonks-yapping

**Q:** what's TSLA at right now

**A:**

- **$TSLA $310** as of 14:30 ET, up **+1.2%** on session

- Holding **$305** support - clean breakout target **$315**

<details>
<summary>Prompt sent to Gemini (40k char cap)</summary>

WHO'S TALKING:
kloh - Personality: chill, posts charts...

USER QUESTION: what's TSLA at right now
</details>

---

## 2026-06-01 18:45:12 UTC

**Asker:** BK (`bankerkyle`) in #stonks-yapping

**Q:** who's the worst trader?

**A:**

Looking at the bottom of the leaderboard, that'd be theorb_18574 - sitting at the very bottom of the trader rankings.

---

## 2026-06-01 20:00:00 UTC

**Asker:** 2pale in #stonks-yapping

**Q:** how about now

**A:**

- still flat

---
"""


def test_parser_splits_on_timestamp_headers():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    assert len(interactions) == 3, (
        f"expected 3 interactions, got {len(interactions)}"
    )
    _ok(f"parse_ask_log: 3 interactions parsed out of 3-block sample")


def test_parser_extracts_asker_fields():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    bk = interactions[1]
    assert bk.asker_label.startswith("BK"), bk.asker_label
    assert bk.asker_username == "bankerkyle", bk.asker_username
    assert "stonks-yapping" in bk.channel, bk.channel
    _ok("parser extracts asker_label, asker_username, channel")


def test_parser_extracts_no_username_when_label_is_plain():
    """2pale's row has no backtick-username - asker_username should be None."""
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    twopale = interactions[2]
    assert twopale.asker_label.startswith("2pale"), twopale.asker_label
    assert twopale.asker_username is None, twopale.asker_username
    _ok("parser leaves asker_username None when label has no `username` form")


def test_parser_extracts_question_and_answer_multiline():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    kloh = interactions[0]
    assert "TSLA" in kloh.question, kloh.question
    assert "$310" in kloh.answer, kloh.answer
    assert "breakout target" in kloh.answer, kloh.answer
    _ok("parser captures multiline question + multiline answer body")


def test_parser_extracts_details_block_when_present():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    kloh = interactions[0]
    assert kloh.prompt_block is not None, "expected <details> block extracted"
    assert "WHO'S TALKING" in kloh.prompt_block, kloh.prompt_block[:200]
    _ok("parser extracts <details> body into prompt_block")


def test_parser_prompt_block_none_when_absent():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    bk = interactions[1]
    assert bk.prompt_block is None, (
        f"expected prompt_block=None for legacy entry, got "
        f"{bk.prompt_block[:100] if bk.prompt_block else bk.prompt_block!r}"
    )
    _ok("parser leaves prompt_block=None for entries without <details>")


def test_parser_skips_malformed_blocks():
    """A block with no clear timestamp header should be silently dropped,
    other interactions in the file should still parse."""
    from ask_qc.parser import parse_ask_log
    corrupted = SAMPLE_LOG.replace("## 2026-06-01 14:30:00 UTC", "## garbage")
    interactions = parse_ask_log(corrupted)
    assert len(interactions) == 2, (
        f"expected 2 interactions (1 dropped), got {len(interactions)}"
    )
    _ok("parser silently drops malformed blocks, continues on the rest")


def test_parser_handles_empty_input():
    from ask_qc.parser import parse_ask_log
    assert parse_ask_log("") == []
    assert parse_ask_log("# header only, no interactions\n") == []
    _ok("parser returns [] on empty / header-only input")


if __name__ == "__main__":
    print("=== ask_qc.parser smoke ===")
    test_parser_splits_on_timestamp_headers()
    test_parser_extracts_asker_fields()
    test_parser_extracts_no_username_when_label_is_plain()
    test_parser_extracts_question_and_answer_multiline()
    test_parser_extracts_details_block_when_present()
    test_parser_prompt_block_none_when_absent()
    test_parser_skips_malformed_blocks()
    test_parser_handles_empty_input()
    print("\nALL ASK-QC PARSER SMOKE TESTS PASS")
