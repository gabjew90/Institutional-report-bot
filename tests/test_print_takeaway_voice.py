"""The takeaway's hard voice ban is applied to what ships (2026-09-30 review)."""
from report import print_takeaway as T


def test_em_dash_and_semicolon_are_rewritten_in_the_shipped_bullet():
    ev = T.evidence_text([{"label": "Core PCE m/m", "actual": "+0.2%", "consensus": "+0.3%"}], [], [])
    out = T.guard([
        {"label": "Dash — test", "text": "Core PCE rose 0.2% — under the 0.3% call; the Fed can wait."},
    ], ev)
    assert out == ["• **Dash , test:** Core PCE rose 0.2% , under the 0.3% call, the Fed can wait."]
    assert all("—" not in o and ";" not in o for o in out)


def test_very_is_not_a_banned_word():
    ev = T.evidence_text([{"label": "Core PCE m/m", "actual": "+0.2%"}], [], [])
    assert T.guard([{"label": "Quiet", "text": "A very quiet 0.2% print."}], ev)
