"""Each short line reaches the profile writer with what it answered
(2026-10-26 audit M1). Run in a subprocess: importing the profile script
rebinds stdout, which breaks pytest's capture (see test_profile_quote_drop)."""
import json
import subprocess
import sys

_CODE = r'''
import sys, json
sys.path.insert(0, '.')
from scripts import backfill_user_profiles as B
prev = [("2026-10-20T14:00:00", 2, "Grand", "I fathered a child with a goth girl"),
        ("2026-10-20T14:01:00", 3, "re4l", "lmao no way"),
        ("2026-10-20T14:01:30", 4, "zebra", "grand is cooked")]
own = [("2026-10-20T14:00:00", 2, "Grand", "x"), ("2026-10-20T14:01:00", 1, "kloh", "mine")]
old = [("2026-10-20T13:00:00", 2, "Grand", "x")]
block = B._format_messages_block([
    {"timestamp": "2026-10-20T14:02:00", "channel_name": "stonks", "content": "bro same",
     "before": [("Grand", "I fathered a child with a goth girl")]},
    {"timestamp": "2026-10-20T14:03:00", "channel_name": "stonks", "content": "lol",
     "reply_to": "Grand", "reply_text": "my wife runs my phone"}])
print(json.dumps({
    "ctx": B._context_before(prev, 1, "2026-10-20T14:02:00", "bro same"),
    "after_own": B._context_before(own, 1, "2026-10-20T14:02:00", "ok"),
    "too_old": B._context_before(old, 1, "2026-10-20T14:02:00", "ok"),
    "long": B._context_before(prev, 1, "2026-10-20T14:02:00", "y" * 200),
    "block": block}))
'''


def _run():
    out = subprocess.run([sys.executable, "-c", _CODE], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_context_lines():
    r = _run()
    assert r["ctx"] == [["re4l", "lmao no way"], ["zebra", "grand is cooked"]]
    assert r["after_own"] == [] and r["too_old"] == [] and r["long"] == []
    assert ('bro same [said just before, by others: Grand: "I fathered a child with a goth girl"]'
            in r["block"])
    assert '(replying to Grand: "my wife runs my phone") lol' in r["block"]
