"""Gemini call ledger: `gemini_calls` rows and the per-feature spend view.
Written by ai_analysis.usage_ledger; read by /status.
"""
from __future__ import annotations

import db as _db  # noqa: E402


def record_gemini_call(*, caller: str, model: str, input_tokens: int = 0,
                       output_tokens: int = 0, thinking_tokens: int = 0,
                       cached_tokens: int = 0, ref: str | None = None) -> int:
    conn = _db.get_connection()
    cur = conn.execute(
        """INSERT INTO gemini_calls
           (caller, model, input_tokens, output_tokens, thinking_tokens,
            cached_tokens, ref)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (caller, model, int(input_tokens or 0), int(output_tokens or 0),
         int(thinking_tokens or 0), int(cached_tokens or 0), ref),
    )
    conn.commit()
    return int(cur.lastrowid)


def gemini_spend(days: int = 7) -> list[dict]:
    """Per caller over the last `days`: calls, tokens and estimated USD
    (priced per model row, see usage_ledger.PRICES_PER_M). Sorted by USD,
    largest first."""
    from ai_analysis.usage_ledger import cost_usd
    conn = _db.get_connection()
    rows = conn.execute(
        """SELECT caller, model, COUNT(*) AS calls,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens
           FROM gemini_calls
           WHERE called_at >= datetime('now', ?)
           GROUP BY caller, model""",
        (f"-{int(days)} days",),
    ).fetchall()
    by_caller: dict[str, dict] = {}
    for r in rows:
        d = by_caller.setdefault(r["caller"], {
            "caller": r["caller"], "calls": 0, "input_tokens": 0,
            "output_tokens": 0, "usd": 0.0, "models": set()})
        d["calls"] += r["calls"]
        d["input_tokens"] += r["input_tokens"]
        d["output_tokens"] += r["output_tokens"]
        d["usd"] += cost_usd(r["model"], r["input_tokens"], r["output_tokens"])
        d["models"].add(r["model"])
    out = []
    for d in by_caller.values():
        d["models"] = sorted(d["models"])
        out.append(d)
    out.sort(key=lambda d: -d["usd"])
    return out
