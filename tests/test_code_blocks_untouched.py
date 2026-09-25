"""Prose filters leave fenced code alone (2026-09-24).

The Pelosi portfolio answer ran code execution and echoed its matplotlib
code into the reply. The citation stripper read
`weights = [21.42, 18.04, 12.54, ...]` as a Gemini marker like
`[1.2.8, 1.2.9]` and deleted it, so the room got `weights =` with
nothing after it. The repetition detector read `fontsize=10,
fontweight='bold'` repeating down the code as a token loop and spent a
retry and a strip on it.
"""
from discord_bot import bot as B

CODE = (
    "```python\n"
    "weights = [21.42, 18.04, 12.54, 11.63, 9.06, 7.91, 6.32, 6.21]\n"
    "top = weights[0]\n"
    "for bar in bars:\n"
    "    ax.text(w, y, f'{w:.2f}%', va='center', ha='left', fontsize=10, fontweight='bold')\n"
    "ax.set_xlabel('Portfolio Allocation (%)', fontsize=11, fontweight='bold')\n"
    "ax.set_title('Portfolio Weightings', fontsize=14, fontweight='bold')\n"
    "```\n"
)
PROSE = "→ **NVDA (21.42%)** leads the book [1] and AVGO sits at 7.91% [cite: 1.2.8, 1.2.9]"


def test_citation_markers_go_but_the_code_list_stays():
    out = B._strip_citation_markers(CODE + PROSE)
    assert "weights = [21.42, 18.04, 12.54" in out
    assert "weights[0]" in out
    assert "[1]" not in out and "[cite:" not in out
    assert "leads the book and AVGO" in out


def test_code_is_not_a_repetition_glitch():
    assert not B._has_repetition_glitch(CODE + PROSE)
    assert B._repetition_glitch_sentences(CODE + PROSE) == []


def test_a_real_loop_after_code_still_trips():
    loop = ("compounding risk and volatility decay risks of volatility decay "
            "and volatility decay")
    assert B._has_repetition_glitch(CODE + loop)


def test_an_unclosed_fence_is_still_protected():
    out = B._strip_citation_markers("see [1]\n```python\nx = [1, 2, 3]\n")
    assert "x = [1, 2, 3]" in out and "see [1]" not in out


def test_text_without_code_behaves_as_before():
    assert B._strip_citation_markers("a [1] b [2.3]") == "a b"


def test_a_text_wrapped_prose_answer_is_still_prose():
    """Gemini wraps plain answers in ```text. That is not code: the
    markers inside must still go and a loop inside must still trip."""
    wrapped = "```text\n→ **CPI** prints at 8:30 [1] and core follows [cite: 1.2]\n```"
    out = B._strip_citation_markers(wrapped)
    assert "[1]" not in out and "[cite:" not in out
    loop = ("```text\ncompounding risk and volatility decay risks of "
            "volatility decay and volatility decay\n```")
    assert B._has_repetition_glitch(loop)
    bare = "```\nsee [1] here\n```"
    assert "[1]" not in B._strip_citation_markers(bare)


def test_code_is_hidden_when_a_chart_is_attached():
    """Owner, 2026-09-24: 'yes hide code'. The chart is the deliverable."""
    import inspect
    src = inspect.getsource(B._ask_10_log_and_render)
    assert "if _code_images and" in src and "_without_code(answer)" in src
    ans = CODE + "\n" + PROSE
    assert B._without_code(ans).strip().startswith("→ **NVDA")


def test_spaced_ranges_read_as_to():
    """2026-09-24: 'September 29 – October 2' shipped as
    'September 29 , October 2'."""
    C = lambda s: B._clean_voice_violations(s)[0]
    assert C("Atlanta (September 29 – October 2)") == "Atlanta (September 29 to October 2)"
    assert C("open 9:30 – 10:00 AM") == "open 9:30 to 10:00 AM"
    assert C("closed at 147 — up 3%") == "closed at 147, up 3%"
    assert C("62–65% odds") == "62–65% odds"
    assert C("the tape — as usual — ripped") == "the tape, as usual, ripped"


def test_the_voice_cleaner_leaves_code_alone():
    code = "```python\nx = 1; y = 2\n# 5 – 10 range\n```\nthe tape — as usual — ripped"
    out = B._clean_voice_violations(code)[0]
    assert "x = 1; y = 2" in out and "# 5 – 10 range" in out
    assert "the tape, as usual, ripped" in out


def test_the_voice_scan_sees_text_wrapped_prose():
    _, hits = B._clean_voice_violations("```text\nthe tape — as usual — ripped\n```")
    assert hits, "a ```text-wrapped answer is prose and its em-dash must register"
    _, hits = B._clean_voice_violations("```python\nx = 1; y = 2\n```")
    assert not hits, "code is not scanned"


def test_the_alias_refresh_job_survives_a_busy_boot():
    import inspect
    from scheduler import jobs
    src = inspect.getsource(jobs.setup_scheduler)
    i = src.index('id="member_aliases_refresh"')
    assert "misfire_grace_time" in src[i:i + 500]
