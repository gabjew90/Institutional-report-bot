"""Claude analysis lane (spec docs/superpowers/specs/2026-10-06-retire-gemini-
pdf-pipeline-design.md, phase 1). Runs on a GitHub runner from
.github/workflows/pilot-analysis.yml, beside the frozen pilot readers.

For each HIGH document the pilot publishes (pilot/source-text/<date>/
<id>.meta.json), Claude runs the SAME deep-analysis prompt Gemini runs in
production (ai_analysis/prompts.py ANALYSIS_SYSTEM_PROMPT and
ANALYSIS_USER_PROMPT_TEXT_ONLY), and the answer goes through the SAME
post-processing (ai_analysis/analysis_build.build_analysis: field
mapping, anchor check, conference sessions). The result is a complete
pdf_analyses-shaped record at pilot/analyses/<date>/<id>.json, so every
consumer of the Gemini rows can read it unchanged.

Standard library plus this repo only: the runner does not install the
bot's requirements.

  pending   list documents with no analysis yet (TSV: id, text, meta, date)
  prompt    write the prompt file for one document
  finalize  parse Claude's answer, build the analysis, write it
  fail      count a failed attempt; a document stops after MAX_ATTEMPTS
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_ATTEMPTS = 3
# The readers stay pinned to claude-sonnet-5 (pilot freeze); this lane is
# new and not part of the freeze, so it uses the current Sonnet.
MODEL = "claude-sonnet-5-5"
ANALYSES = "analyses"
FAILURES = "analysis-failures"


def _failures(root: Path, date: str, doc_id: str) -> Path:
    return root / FAILURES / date / f"{doc_id}.json"


def pending(root: Path, days: int, limit: int) -> list[tuple[str, str, str, str]]:
    today = dt.date.today()
    out = []
    for back in range(days):
        date = (today - dt.timedelta(days=back)).isoformat()
        for meta in sorted((root / "source-text" / date).glob("*.meta.json")):
            doc_id = meta.name.split(".")[0]
            if (root / ANALYSES / date / f"{doc_id}.json").exists():
                continue
            f = _failures(root, date, doc_id)
            if f.exists() and json.loads(f.read_text()).get("attempts", 0) >= MAX_ATTEMPTS:
                continue
            m = json.loads(meta.read_text(encoding="utf-8"))
            text = m.get("text_path") or ""
            # text_path is relative to the pilot-data checkout
            text_abs = root.parent / text if text else None
            if not text_abs or not text_abs.exists():
                continue
            out.append((doc_id, str(text_abs), str(meta), date))
            if len(out) >= limit:
                return out
    return out


def write_prompt(text_path: str, meta_path: str, out: str) -> None:
    from ai_analysis.prompts import ANALYSIS_SYSTEM_PROMPT, ANALYSIS_USER_PROMPT_TEXT_ONLY
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    text = Path(text_path).read_text(encoding="utf-8", errors="replace")
    user = ANALYSIS_USER_PROMPT_TEXT_ONLY.format(
        file_name=meta.get("file_name") or "", total_pages="unknown", text_content=text)
    Path(out).write_text(
        ANALYSIS_SYSTEM_PROMPT
        + "\n\n---\n\n" + user
        + "\n\n---\n\nAnswer with the JSON object only: no prose before or after it, "
          "no tool use. The full document text is above.",
        encoding="utf-8")


LIMIT_MARKERS = ("hit your", "usage limit", "rate limit")


def _cli_result(raw: str) -> tuple[str, dict]:
    """(answer text, usage) from `claude -p --output-format json`, whose
    stdout is {"result": ..., "usage": {...}}; plain text passes through."""
    try:
        d = json.loads(raw)
    except ValueError:
        return raw, {}
    if isinstance(d, dict) and "result" in d:
        return str(d.get("result") or ""), d.get("usage") or {}
    return raw, {}


def is_limit(raw: str) -> bool:
    """True when the CLI answered with a usage or rate limit: not a
    failure of the document, so it must not count as an attempt."""
    text, _ = _cli_result(raw)
    low = (text or raw).lower()
    return any(m in low for m in LIMIT_MARKERS) and "{" not in low[:200]


def finalize(raw: str, text_path: str, meta_path: str, out: str, model: str) -> dict:
    from ai_analysis.analysis_build import _parse_json_response, build_analysis
    meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
    answer, usage = _cli_result(Path(raw).read_text(encoding="utf-8", errors="replace"))
    data = _parse_json_response(answer)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict) or not data.get("key_insights"):
        raise ValueError(f"no analysis JSON in the answer: {answer[:160]!r}")
    text = Path(text_path).read_text(encoding="utf-8", errors="replace")
    analysis = build_analysis(
        data, pdf_file_id=int(meta["pdf_file_id"]), file_name=meta.get("file_name") or "",
        priority=meta.get("priority") or "high", text_content=text,
        total_pages=int(meta.get("total_pages") or 0),
        input_tokens=int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0))
    # A banned publication (The Market Ear) keeps its name as the source,
    # as Gemini's rows do: the TRADE BOARD drops calls by that name, and
    # Claude naming the underlying author ("Goldman Sachs", "ZeroHedge")
    # would let a TME repost through as a desk call. Claude's attribution
    # is kept beside it.
    from ai_analysis.analysis_build import canonical_source
    from ai_analysis.voice_rules import BANNED_PUBLICATION_NAMES
    claude_source = analysis.source
    folder_source = canonical_source(meta.get("source") or "")
    if folder_source in {canonical_source(b) for b in BANNED_PUBLICATION_NAMES}:
        analysis.source = folder_source
    record = {
        "pdf_file_id": int(meta["pdf_file_id"]),
        "claude_source": claude_source,
        "model": model,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "analysis": asdict(analysis),
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record


def fail(root: Path, date: str, doc_id: str, reason: str) -> int:
    f = _failures(root, date, doc_id)
    rec = json.loads(f.read_text()) if f.exists() else {"attempts": 0, "history": []}
    rec["attempts"] += 1
    rec["history"].append({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                           "reason": reason[:200]})
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return rec["attempts"]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending")
    p.add_argument("--root", required=True)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("prompt")
    p.add_argument("--text", required=True)
    p.add_argument("--meta", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("finalize")
    for a in ("--raw", "--text", "--meta", "--out"):
        p.add_argument(a, required=True)
    p.add_argument("--model", default=MODEL)
    p = sub.add_parser("limit")
    p.add_argument("--raw", required=True)
    p = sub.add_parser("fail")
    for a in ("--root", "--date", "--id", "--reason"):
        p.add_argument(a, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "pending":
        for row in pending(Path(a.root), a.days, a.limit):
            print("\t".join(row))
    elif a.cmd == "prompt":
        write_prompt(a.text, a.meta, a.out)
    elif a.cmd == "finalize":
        try:
            rec = finalize(a.raw, a.text, a.meta, a.out, a.model)
        except Exception as e:
            print(f"finalize failed: {e}", file=sys.stderr)
            return 1
        ac = rec["analysis"].get("anchor_check") or {}
        print(f"ok {rec['pdf_file_id']}: {len(rec['analysis'].get('key_insights') or [])} insights, "
              f"anchors {ac.get('matched')}/{ac.get('total')}")
    elif a.cmd == "limit":
        return 0 if is_limit(Path(a.raw).read_text(encoding="utf-8", errors="replace")) else 1
    elif a.cmd == "fail":
        n = fail(Path(a.root), a.date, a.id, a.reason)
        print(f"attempts={n} given_up={n >= MAX_ATTEMPTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
