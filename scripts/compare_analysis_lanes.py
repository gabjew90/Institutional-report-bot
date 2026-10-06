"""Compare the Claude analysis lane with Gemini for the same documents
(spec docs/superpowers/specs/2026-10-06-retire-gemini-pdf-pipeline-
design.md, phase 1 shadow check). Read-only.

Run on the worker, after copying the Claude records there, or anywhere
both inputs are available:

  python scripts/compare_analysis_lanes.py <claude_dir> <gemini_json>

<claude_dir>: pilot/analyses/ from the pilot-data branch.
<gemini_json>: {pdf_file_id: analysis_json} for the same ids, from the
latest pdf_analyses row per document.

Prints, per field, how many items each lane found, both lanes' anchor
match rates, and agreement on source, report type and tickers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FIELDS = ("key_insights", "market_movers", "trade_ideas", "earnings_insights",
          "macro_indicators", "key_data_points", "entities_mentioned",
          "risk_factors", "conference_sessions", "charts_described")


def _tickers(a: dict) -> set[str]:
    return {(e.get("ticker") or "").upper() for e in a.get("entities_mentioned") or []
            if isinstance(e, dict) and e.get("ticker")}


def compare(claude: dict[int, dict], gemini: dict[int, dict]) -> dict:
    ids = sorted(set(claude) & set(gemini))
    out = {"documents": len(ids), "fields": {}, "anchor": {}, "agree": {}}
    for f in FIELDS:
        c = sum(len(claude[i].get(f) or []) for i in ids)
        g = sum(len(gemini[i].get(f) or []) for i in ids)
        out["fields"][f] = {"claude": c, "gemini": g}
    for name, lane in (("claude", claude), ("gemini", gemini)):
        m = sum((lane[i].get("anchor_check") or {}).get("matched", 0) for i in ids)
        t = sum((lane[i].get("anchor_check") or {}).get("matched", 0)
                + (lane[i].get("anchor_check") or {}).get("missed", 0) for i in ids)
        out["anchor"][name] = {"matched": m, "checked": t, "rate": round(m / t, 3) if t else None}
    out["agree"]["source"] = sum(
        (claude[i].get("source") or "").lower()[:6] == (gemini[i].get("source") or "").lower()[:6]
        for i in ids)
    out["agree"]["report_type"] = sum(claude[i].get("report_type") == gemini[i].get("report_type")
                                      for i in ids)
    jac = []
    for i in ids:
        a, b = _tickers(claude[i]), _tickers(gemini[i])
        if a or b:
            jac.append(len(a & b) / len(a | b))
    out["agree"]["ticker_overlap_avg"] = round(sum(jac) / len(jac), 3) if jac else None
    return out


def load_claude(root: Path) -> dict[int, dict]:
    recs = {}
    for f in root.rglob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            recs[int(d["pdf_file_id"])] = d["analysis"]
        except (ValueError, KeyError):
            continue
    return recs


def main(argv: list[str]) -> int:
    claude = load_claude(Path(argv[0]))
    gemini = {int(k): (json.loads(v) if isinstance(v, str) else v)
              for k, v in json.loads(Path(argv[1]).read_text(encoding="utf-8")).items()}
    print(json.dumps(compare(claude, gemini), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
