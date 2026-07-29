"""Smoke: un-echoable inline artifacts are stripped from the model turn.

2026-07-29, second failure of "analyze trades opened by analysts
relative to qqq" (the NaN fix cleared the first):

  400 INVALID_ARGUMENT: Unsupported MIME type: application/octet-stream

With code execution enabled, the model's turn can carry inline_data
artifacts the sandbox produced (a saved .csv/.npy comes back as
application/octet-stream). The tool loop echoes the whole turn back
into `contents` for the next round, and the API rejects the request —
surfacing as "Something about that question broke the model."

Only inline parts the API accepts (image/*, application/pdf) may be
echoed; text / executable_code / code_execution_result / function_call
must always survive because the loop depends on them.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _part(**kw):
    base = dict(text=None, executable_code=None, code_execution_result=None,
                function_call=None, inline_data=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _inline(mime):
    return _part(inline_data=SimpleNamespace(mime_type=mime, data=b"x"))


def test_octet_stream_dropped():
    from discord_bot.bot import _safe_echo_parts
    parts = [_part(text="hi"), _inline("application/octet-stream")]
    out = _safe_echo_parts(parts)
    assert len(out) == 1 and out[0].text == "hi", out
    _ok("application/octet-stream inline artifact dropped")


def test_supported_inline_kept():
    from discord_bot.bot import _safe_echo_parts
    parts = [_inline("image/png"), _inline("application/pdf")]
    assert len(_safe_echo_parts(parts)) == 2
    _ok("image/* and application/pdf inline parts survive")


def test_loop_parts_always_survive():
    from discord_bot.bot import _safe_echo_parts
    parts = [
        _part(text="reasoning"),
        _part(executable_code=SimpleNamespace(code="print(1)")),
        _part(code_execution_result=SimpleNamespace(output="1")),
        _part(function_call=SimpleNamespace(name="query_data", args={})),
        _inline("application/octet-stream"),
    ]
    out = _safe_echo_parts(parts)
    assert len(out) == 4, f"loop-critical parts must all survive: {out}"
    assert any(getattr(p, "function_call", None) for p in out), (
        "function_call MUST survive — the tool loop depends on it"
    )
    _ok("text/code/result/function_call all survive the scrub")


def test_wired_into_echo():
    import inspect
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_safe_echo_parts(response_parts)" in src, (
        "the model-turn echo must go through the scrub"
    )
    _ok("scrub wired into the tool-loop echo")


def test_empty_and_none_safe():
    from discord_bot.bot import _safe_echo_parts
    assert _safe_echo_parts([]) == []
    assert _safe_echo_parts(None) == []
    _ok("empty / None input handled")


if __name__ == "__main__":
    print("=== echo-parts scrub smoke ===")
    test_octet_stream_dropped()
    test_supported_inline_kept()
    test_loop_parts_always_survive()
    test_wired_into_echo()
    test_empty_and_none_safe()
    print("\nALL ECHO PARTS SMOKE TESTS PASS")
