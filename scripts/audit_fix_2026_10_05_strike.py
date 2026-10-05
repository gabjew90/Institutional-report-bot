r"""One-off: apply analyst_log.strike_check to stored option trades
(2026-10-05 audit, owner-approved). --dry lists, --apply writes.

Run on the worker (the shell needs the worker's library path for numpy):
  railway ssh "cd /app && LD_LIBRARY_PATH=$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^LD_LIBRARY_PATH=//p') /opt/venv/bin/python scripts/audit_fix_2026_10_05_strike.py --apply"

Also marks BK's 'Soxl 165c +100%' gain report (row 123770), stored as an
open, as a non-trade. Originals are saved to /data/audit-fixes first."""
import json
import logging
import os
import sqlite3
import sys

logging.disable(logging.WARNING)
sys.path.insert(0, "/app")
from analyst_log import strike_check as SC  # noqa: E402

APPLY = "--apply" in sys.argv
GAIN_REPORT_IDS = {123770}
NOTE = "audit 2026-10-05"

ro = sqlite3.connect("file:/data/reports.db?mode=ro", uri=True, timeout=5)
ro.row_factory = sqlite3.Row
rows = ro.execute(
    "SELECT * FROM analyst_trades WHERE is_trade = 1 AND contract_type IN ('call','put') "
    "AND posted_at >= '2026-09-05' ORDER BY posted_at").fetchall()
reject, move = [], []
for r in rows:
    x = {"is_trade_screenshot": True, "ticker": r["ticker"], "contract_type": r["contract_type"],
         "strike": r["strike"], "notes": ""}
    out = SC.apply(x, r["posted_at"])
    if r["id"] in GAIN_REPORT_IDS:
        reject.append((r, "a gain report ('+100%'), not an entry"))
    elif not out["is_trade_screenshot"]:
        reject.append((r, out["what_it_appears_to_be"]))
    elif out["ticker"] != r["ticker"]:
        move.append((r, out["ticker"]))
ro.close()

for r, why in reject:
    print("REJECT", r["id"], r["posted_at"][:10], r["author"], "|", r["ticker"], r["strike"],
          r["action"], "|", (r["caption"] or "")[:40].replace("\n", " "), "|", why)
for r, to in move:
    print("MOVE  ", r["id"], r["posted_at"][:10], r["author"], "|", r["ticker"], "->", to,
          r["strike"], "|", (r["caption"] or "")[:40].replace("\n", " "))
print(f"checked {len(rows)}: reject {len(reject)}, move {len(move)}")
if not APPLY:
    sys.exit(0)

os.makedirs("/data/audit-fixes", exist_ok=True)
backup = "/data/audit-fixes/2026-10-05-strike-check.json"
with open(backup, "w") as f:
    json.dump({"reject": [dict(r) | {"why": w} for r, w in reject],
               "move": [dict(r) | {"to": t} for r, t in move]}, f, default=str)

rw = sqlite3.connect("/data/reports.db", timeout=15)
with rw:
    for r, why in reject:
        g = json.loads(r["gemini_json"] or "{}")
        g["audit_original"] = {k: r[k] for k in ("ticker", "contract_type", "strike", "expiry",
                                                 "action", "gain_pct", "price")}
        g["is_trade_screenshot"] = False
        g["what_it_appears_to_be"] = f"{why} ({NOTE})"
        rw.execute("UPDATE analyst_trades SET is_trade = 0, ticker = NULL, contract_type = NULL, "
                   "strike = NULL, expiry = NULL, action = NULL, gain_pct = NULL, price = NULL, "
                   "gemini_json = ? WHERE id = ? AND is_trade = 1", (json.dumps(g), r["id"]))
    for r, to in move:
        g = json.loads(r["gemini_json"] or "{}")
        g["ticker"] = to
        g["notes"] = (str(g.get("notes") or "") + f"; index moved from {r['ticker']} by strike "
                      f"({NOTE})").lstrip("; ")
        rw.execute("UPDATE analyst_trades SET ticker = ?, gemini_json = ? WHERE id = ? AND ticker = ?",
                   (to, json.dumps(g), r["id"], r["ticker"]))
rw.close()
print(f"applied; originals in {backup}")
