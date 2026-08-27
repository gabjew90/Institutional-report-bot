"""The triage CLI the headless ask-QC judge runs. Thin wrapper, but it
is the deterministic half of FAIL classification — if it breaks, every
nightly triage silently becomes 'regex-able' (the CLI-returned-nothing
bucket), which mislabels validator misses."""
import json
import subprocess
import sys

REPO = __file__.rsplit("tests", 1)[0]


def _run(text, tools=""):
    p = subprocess.run(
        [sys.executable, "scripts/validate_answer.py", "--tools", tools],
        input=text, capture_output=True, text=True, encoding="utf-8",
        cwd=REPO,
    )
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_known_violation_is_reported():
    got = _run("RSI (14) is sitting neutral around 58, indicating "
               "neither overbought nor oversold")
    rules = {v["rule"] for v in got["violations"]}
    assert "self-generated-ta" in rules


def test_clean_answer_is_empty():
    got = _run("-> AVGO 450C closed +71.2%. the log keeps percentages, "
               "not sizes.")
    assert got["violations"] == []


def test_tool_call_exempts_tool_gated_class():
    """unforced-price is clean when the price tool fired — the CLI must
    pass tools through or every tool-grounded answer looks unforced."""
    text = "$NVDA is currently trading at $126.46"
    with_tool = _run(text, tools="lookup_market_price")
    without = _run(text)
    assert not any(v["rule"] == "unforced-price"
                   for v in with_tool["violations"])
    assert any(v["rule"] == "unforced-price"
               for v in without["violations"])


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
