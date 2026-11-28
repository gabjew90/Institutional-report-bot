"""Triage by Claude (2026-11-28, owner: "triage to Claude").

One triage call: the production triage prompt (ai_analysis/prompts.py
TRIAGE_SYSTEM_PROMPT and TRIAGE_USER_PROMPT, the same first 32,000
characters) through the Claude CLI the way the analysis lane calls it, with
no tools, no project context and a working directory outside the checkout.
The lane's run uses `prompt_for` and `call` for every note the worker hands
over before triage (scripts/pilot_analyze.py).

Triage decides which notes are analysed and which the Omnipulse readers see,
so it was measured before it was switched: Claude's label beside the one
Gemini gave the same note. `bundle` is that measurement, kept because the
triage prompt is sensitive to small edits (moving one hint line changed 8% of
priorities on 2026-11-27) and any change to it should be measured the same
way first.

  bundle   triage every note of a bundle file, once per run named in --runs,
           and write one JSON line per call

A bundle is a gzip file of JSON lines, one note each: id, file_name, folder,
preview (what Gemini's triage saw) and Gemini's stored answer.
scripts/triage_bundle.py builds one on the worker, read-only, and
scripts/triage_compare.py reads the results. The workflow input
`triage_bundle` of pilot-analysis.yml runs this on a GitHub runner.

Standard library plus this repo only, like scripts/pilot_analyze.py.
"""
from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PREVIEW_CHARS = 32000       # ai_analysis/analyzer.py triage_pdf
PRIORITIES = ("high", "medium", "low")
SYSTEM_PROMPT = ("You classify one research document. Follow the instructions in the message "
                 "and answer with the JSON object only.")
CLI_FLAGS = ("--tools", "", "--strict-mcp-config", "--disable-slash-commands",
             "--no-session-persistence", "--system-prompt", SYSTEM_PROMPT,
             "--max-turns", "3", "--output-format", "json")
LIMIT_MARKERS = ("hit your", "usage limit", "rate limit")


def prompt_for(file_name: str, folder_path: str, text: str) -> str:
    """The message Claude gets: Gemini's system instruction, then Gemini's
    user prompt, unchanged."""
    from ai_analysis.prompts import TRIAGE_SYSTEM_PROMPT, TRIAGE_USER_PROMPT
    user = TRIAGE_USER_PROMPT.format(file_name=file_name, folder_path=folder_path,
                                     text_preview=(text or "")[:PREVIEW_CHARS])
    return (TRIAGE_SYSTEM_PROMPT + "\n\n---\n\n" + user
            + "\n\n---\n\nAnswer with the JSON object only: no prose before or after it, no tool use.")


def read_answer(answer: str) -> dict:
    """Claude's answer as the triage fields. `priority` is empty when the
    answer names none of the three, so a caller can tell a refusal or a
    malformed answer from a decision."""
    from ai_analysis.analysis_build import _parse_json_response
    try:
        d = _parse_json_response(answer or "")
    except Exception:
        d = {}
    if isinstance(d, list):
        d = d[0] if d else {}
    if not isinstance(d, dict):
        d = {}
    pr = str(d.get("priority") or "").strip().lower()
    tickers = d.get("key_tickers")
    return {"priority": pr if pr in PRIORITIES else "",
            "report_type": str(d.get("report_type") or "other"),
            "source": str(d.get("source") or ""),
            "key_tickers": tickers if isinstance(tickers, list) else [],
            "summary": str(d.get("summary") or "")}


def call(prompt: str, model: str, cwd: str, timeout_s: int = 300) -> dict:
    """One triage call. Returns the fields, the usage and the seconds it took,
    {"limit": True} on a usage limit, or {"err": ...}."""
    t0 = time.monotonic()
    try:
        p = subprocess.run(["claude", "-p", "--model", model, *CLI_FLAGS],
                           input=prompt.encode("utf-8"), capture_output=True,
                           timeout=timeout_s, cwd=cwd)
        raw = p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return {"err": "timeout", "secs": round(time.monotonic() - t0, 1)}
    except OSError as e:
        return {"err": f"claude CLI did not start: {e}"}
    secs = round(time.monotonic() - t0, 1)
    try:
        d = json.loads(raw)
    except ValueError:
        d = None
    if not isinstance(d, dict) or "result" not in d:
        low = raw.lower()
        if any(m in low for m in LIMIT_MARKERS):
            return {"limit": True, "err": raw[:160]}
        return {"err": "no CLI result: " + " ".join(raw[:160].split()), "secs": secs}
    text = str(d.get("result") or "")
    low = text.lower()
    if any(m in low for m in LIMIT_MARKERS) and "{" not in low[:200]:
        return {"limit": True, "err": text[:160]}
    u = d.get("usage") or {}
    out = read_answer(text)
    res = {**out, "summary": out["summary"][:300],
           "in": int(u.get("input_tokens") or 0),
           "cache_read": int(u.get("cache_read_input_tokens") or 0),
           "cache_new": int(u.get("cache_creation_input_tokens") or 0),
           "out": int(u.get("output_tokens") or 0),
           "secs": secs, "api_ms": d.get("duration_api_ms"), "turns": d.get("num_turns")}
    if not out["priority"]:
        res["err"] = "no priority in the answer: " + " ".join(text[:160].split())
    return res


def parse_runs(spec: str) -> list[tuple[str, str, int]]:
    """"haiku=claude-haiku-5-5,haiku2=claude-haiku-5-5:120" -> (label, model,
    how many of the bundle's first notes; 0 = all)."""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        label, _, rest = part.partition("=")
        model, _, n = rest.partition(":")
        out.append((label.strip(), model.strip(), int(n) if n.strip() else 0))
    return out


def run_bundle(bundle: str, out_path: str, runs: list[tuple[str, str, int]], *,
               workers: int = 6, budget_s: float = 2100, limit: int = 0,
               ask=call, say=print) -> dict:
    with gzip.open(bundle, "rt", encoding="utf-8") as fh:
        notes = [json.loads(line) for line in fh if line.strip()]
    if limit:
        notes = notes[:limit]
    jobs = [(label, model, n) for label, model, k in runs for n in (notes[:k] if k else notes)]
    say(f"notes {len(notes)}, calls {len(jobs)}, {workers} at a time")
    started = time.monotonic()
    lock, stop = threading.Lock(), threading.Event()
    stats = {"done": 0, "errors": 0, "limit": False, "left": 0}
    fh = open(out_path, "a", encoding="utf-8")

    def work() -> None:
        cwd = tempfile.mkdtemp(prefix="triage-")
        while not stop.is_set():
            with lock:
                if not jobs or time.monotonic() - started > budget_s:
                    return
                label, model, note = jobs.pop(0)
            res = ask(prompt_for(note.get("file_name") or "", note.get("folder") or "",
                                 note.get("preview") or ""), model, cwd)
            if res.get("limit"):
                stop.set()
                with lock:
                    stats["limit"] = True
                say("::warning::Claude usage limit reached; stopping")
                return
            with lock:
                fh.write(json.dumps({"id": note.get("id"), "run": label, "model": model, **res},
                                    ensure_ascii=False) + "\n")
                fh.flush()
                stats["done"] += 1
                stats["errors"] += bool(res.get("err"))
                if stats["done"] % 25 == 0 or res.get("err"):
                    say(f"{stats['done']}: {note.get('id')} {label} "
                        f"{res.get('priority') or res.get('err')} {res.get('secs')}s")

    threads = [threading.Thread(target=work) for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    fh.close()
    stats["left"] = len(jobs)
    stats["wall_seconds"] = round(time.monotonic() - started)
    return stats


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bundle")
    p.add_argument("--bundle", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--runs", required=True)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--budget-minutes", type=float, default=35)
    p.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    if a.cmd == "bundle":
        st = run_bundle(a.bundle, a.out, parse_runs(a.runs), workers=a.workers,
                        budget_s=a.budget_minutes * 60, limit=a.limit)
        print(f"calls done {st['done']}, errors {st['errors']}, not reached {st['left']}, "
              f"{st['wall_seconds']}s wall" + (", stopped by a usage limit" if st["limit"] else ""))
        if st["done"] and st["errors"] == st["done"]:
            print("::error::every call failed")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
