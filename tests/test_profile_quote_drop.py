"""A quote the member never wrote does not ship in a profile (2026-10-10
audit: G's "Stop losses are for homosexuals..." was invented)."""
import subprocess
import sys

_CODE = r'''
import sys, json
sys.path.insert(0, '.')
from scripts import backfill_user_profiles as B
p = ("**Voice.**\n"
     "- \"Nigga I'm fighting for my life over here\" — [taking a hit]\n"
     "- \"Stop losses are for homosexuals. For a straight man there's only liquidation\" — [risk]\n"
     "- Claims to live well — \"i love strippers\" + [the classic 'I'm living the life' posturing]\n")
new, dropped = B._drop_unverified_quote_lines(p, [
    "Stop losses are for homosexuals. For a straight man there's only liquidation",
    "I'm living the life"])
print(json.dumps({"new": new, "dropped": dropped}))
'''


def test_an_unverified_double_quote_drops_its_bullet():
    out = subprocess.run([sys.executable, "-c", _CODE], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    import json
    res = json.loads(out.strip().splitlines()[-1])
    assert "Stop losses" not in res["new"] and len(res["dropped"]) == 1
    assert "fighting for my life" in res["new"]
    # a single-quoted phrase is the writer's commentary and keeps its line
    assert "i love strippers" in res["new"]
