"""Claude analysis lane (spec docs/superpowers/specs/2026-10-06-retire-gemini-
pdf-pipeline-design.md, phase 1). Runs on a GitHub runner from
.github/workflows/pilot-analysis.yml, beside the frozen pilot readers.

For each HIGH document the pilot publishes (pilot/source-text/<date>/
<id>.meta.json), and each MEDIUM document the worker hands to the lane
(pilot/source-text-all/<date>/<id>.meta.json marked "lane", 2026-10-27),
Claude runs the SAME deep-analysis prompt Gemini runs in
production (ai_analysis/prompts.py ANALYSIS_SYSTEM_PROMPT and
ANALYSIS_USER_PROMPT_TEXT_ONLY), and the answer goes through the SAME
post-processing (ai_analysis/analysis_build.build_analysis: field
mapping, anchor check, conference sessions). The result is a complete
pdf_analyses-shaped record at pilot/analyses/<date>/<id>.json, so every
consumer of the Gemini rows can read it unchanged.

Which documents: only the ones the worker is waiting for (2026-11-23). The
worker publishes pilot/lane/waiting.json (github_bridge/claude_lane.py), each
note with the time the worker stops waiting and runs Gemini instead. Before
that this script analysed every note of the last seven days, oldest id first,
25 a run and one at a time, and of 960 analyses in nine days the worker could
use 83: the rest arrived after it had given up. Now a note off the list, or
too close to its deadline, is not analysed, and `run` works several notes at
once so a day's research, which arrives within an hour or two, is done inside
the worker's two-hour wait.

Standard library plus this repo only: the runner does not install the
bot's requirements.

  pending   list the documents the worker waits for (TSV: id, text, meta, date)
  run       analyse them, several at a time, committing each record
  prompt    write the prompt file for one document
  finalize  parse Claude's answer, build the analysis, write it
  fail      count a failed attempt; a document stops after MAX_ATTEMPTS
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_ATTEMPTS = 3
# The readers stay pinned to claude-sonnet-5 (pilot freeze); this lane is
# new and not part of the freeze, so it uses the current Sonnet.
MODEL = "claude-sonnet-5-5"
ANALYSES = "analyses"
FAILURES = "analysis-failures"
# The worker's list of notes it is waiting for, relative to the pilot root.
WAITING = "lane/waiting.json"
# A note with less than this left before the worker gives up is not started:
# an analysis takes about a minute and the worker reads results every five.
MIN_LEFT_S = 240
SOURCE_DIRS = ("source-text", "source-text-all")


def _failures(root: Path, date: str, doc_id: str) -> Path:
    return root / FAILURES / date / f"{doc_id}.json"


def load_waiting(root: Path) -> list[dict]:
    """The worker's list, in the order it wants them. Missing or unreadable
    means nothing to do: the worker's timeout then runs Gemini."""
    try:
        d = json.loads((root / WAITING).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = d.get("waiting") if isinstance(d, dict) else None
    return [e for e in (rows or []) if isinstance(e, dict)]


def _deadline(e: dict) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(str(e.get("deadline")), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def pending(root: Path, limit: int = 25,
            now: dt.datetime | None = None) -> list[tuple[str, str, str, str]]:
    """Documents the worker is waiting for that have no analysis yet, in the
    worker's order (HIGH first, nearest deadline first)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for e in load_waiting(root):
        doc_id, date = str(e.get("id") or ""), str(e.get("date") or "")
        # Both become path segments: digits and an ISO date, nothing else.
        if not doc_id.isdigit() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        deadline = _deadline(e)
        if deadline is not None and (deadline - now).total_seconds() < MIN_LEFT_S:
            continue
        if (root / ANALYSES / date / f"{doc_id}.json").exists():
            continue
        f = _failures(root, date, doc_id)
        if f.exists() and json.loads(f.read_text()).get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        meta = next((m for m in (root / d / date / f"{doc_id}.meta.json" for d in SOURCE_DIRS)
                     if m.exists()), None)
        if meta is None:
            continue    # the text is not in this checkout yet; the next refresh has it
        m = json.loads(meta.read_text(encoding="utf-8"))
        text = m.get("text_path") or ""
        # text_path is relative to the pilot-data checkout
        text_abs = root.parent / text if text else None
        if not text_abs or not text_abs.exists():
            continue
        out.append((doc_id, str(text_abs), str(meta), date))
        if len(out) >= limit:
            break
    return out


def recent_high(root: Path, n: int) -> list[tuple[str, str, str, str]]:
    """The n newest HIGH notes on the branch, list or no list: the input of a
    dry run, which measures speed and commits nothing."""
    out = []
    for folder in sorted((root / SOURCE_DIRS[0]).glob("20*"), reverse=True):
        for meta in sorted(folder.glob("*.meta.json"), reverse=True):
            m = json.loads(meta.read_text(encoding="utf-8"))
            text_abs = root.parent / (m.get("text_path") or "")
            if m.get("text_path") and text_abs.exists():
                out.append((meta.name.split(".")[0], str(text_abs), str(meta), folder.name))
                if len(out) >= n:
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


REFRESH_S = 20
PILOT_BRANCH = "pilot-data"


# The analysis call has no tools and no project context (2026-11-23).
#
# Until this date it ran from the repository checkout with every tool allowed
# (--permission-mode bypassPermissions) on text taken from bank PDFs, beside a
# token that can push to any branch, the deploying one included. The answer is
# one JSON object. It needs no tool.
#
# Running from the checkout also loaded this repository's CLAUDE.md (about
# 31,000 tokens), its hooks and Claude Code's own coding system prompt and
# tool definitions into every call: the 40 records of 2026-11-22 used a median
# 83,505 input tokens for notes Gemini reads in 16,705 with the same prompt.
# Now: no tools, no MCP servers, no skills, a one-line system prompt in place
# of the coding one, and a working directory outside the checkout, so nothing
# of this repository is read.
LANE_SYSTEM_PROMPT = ("You analyse one research document. Follow the instructions in the message "
                      "and answer with the JSON object only.")
LANE_CLI_FLAGS = ("--tools", "", "--strict-mcp-config", "--disable-slash-commands",
                  "--no-session-persistence", "--system-prompt", LANE_SYSTEM_PROMPT,
                  "--max-turns", "3", "--output-format", "json")


def claude_cli(prompt_path: Path, model: str, timeout_s: int = 900) -> str:
    """One analysis: the prompt on standard input, the CLI's JSON on standard
    output. A timeout or a crash returns what there is, which then fails
    finalize and counts as one attempt."""
    try:
        with open(prompt_path, "rb") as fin:
            p = subprocess.run(
                ["claude", "-p", "--model", model, *LANE_CLI_FLAGS],
                stdin=fin, capture_output=True, timeout=timeout_s,
                # the worker's own temp folder, never the repository
                cwd=str(Path(prompt_path).parent))
        return p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return (e.stdout or b"").decode("utf-8", "replace")
    except OSError as e:
        return f"claude CLI did not start: {e}"


def _git(repo: Path, *args: str) -> int:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).returncode


def run(root: Path, *, workers: int = 4, budget_s: float = 1500, model: str = MODEL,
        ask=claude_cli, repo: Path | None = None, docs: list | None = None,
        out_root: Path | None = None, say=print) -> dict:
    """Analyse what the worker waits for, `workers` at a time, until the list
    is empty, the time budget is spent or Claude answers with a usage limit.

    `repo` is the pilot-data checkout to commit and push each record from;
    None commits nothing. The checkout is refreshed between documents, so a
    note the worker hands over mid-run is picked up and one it gave up on is
    dropped. `docs` and `out_root` are the dry run: a fixed list, records
    written outside the checkout.

    Every write into the checkout and every git command happens under one
    lock, because a rebase refuses a tree another thread is writing to."""
    started = time.monotonic()
    lock = threading.Lock()
    stop = threading.Event()
    claimed: set[str] = set()
    stats = {"analyzed": 0, "failed": 0, "limit": False, "seconds": [], "input_tokens": []}
    last_sync = [0.0]
    dest_root = out_root or root
    fixed = list(docs) if docs is not None else None

    def claim():
        with lock:
            if stop.is_set() or time.monotonic() - started > budget_s:
                return None
            if fixed is not None:
                return fixed.pop(0) if fixed else None
            if repo is not None and time.monotonic() - last_sync[0] >= REFRESH_S:
                _git(repo, "pull", "--rebase", "--quiet", "origin", PILOT_BRANCH)
                last_sync[0] = time.monotonic()
            for row in pending(root, limit=10 ** 6):
                if row[0] not in claimed:
                    claimed.add(row[0])
                    return row
            return None

    def persist(label: str) -> None:
        if repo is None or out_root is not None:
            return
        rel = root.relative_to(repo).as_posix()
        # One path per call: git stages nothing at all when any path given
        # to one `git add` does not exist, and the failures folder may not.
        for folder in (ANALYSES, FAILURES):
            if (root / folder).exists():
                _git(repo, "add", f"{rel}/{folder}/")
        if _git(repo, "diff", "--cached", "--quiet") == 0:
            return
        if _git(repo, "commit", "-q", "-m", f"pilot: analysis ({label})") != 0:
            return
        for _ in range(3):
            _git(repo, "pull", "--rebase", "--quiet", "origin", PILOT_BRANCH)
            if _git(repo, "push", "-q", "origin", PILOT_BRANCH) == 0:
                last_sync[0] = time.monotonic()
                return
            time.sleep(3)
        say(f"::warning::could not push after {label}; the next commit retries")

    def work(n: int) -> None:
        tmp = Path(tempfile.mkdtemp(prefix=f"lane{n}-"))
        while True:
            row = claim()
            if row is None:
                return
            doc_id, text, meta, date = row
            t0 = time.monotonic()
            prompt, raw, made = tmp / "prompt.txt", tmp / "raw.txt", tmp / f"{doc_id}.json"
            try:
                write_prompt(text, meta, str(prompt))
                answer = ask(prompt, model)
            except Exception as e:
                answer = f"analysis did not run: {e}"
            if is_limit(answer):
                # Not this document's fault, and the rest of the run would hit it too.
                stop.set()
                with lock:
                    stats["limit"] = True
                    claimed.discard(doc_id)
                say("::warning::Claude usage limit reached; stopping, nothing counted as failed")
                return
            raw.write_text(answer, encoding="utf-8")
            used = 0
            try:
                rec = finalize(str(raw), text, meta, str(made), model)
                used = int((rec.get("analysis") or {}).get("input_tokens") or 0)
                ok, why = True, ""
            except Exception as e:
                ok, why = False, str(e)
            took = time.monotonic() - t0
            with lock:
                if ok:
                    dest = dest_root / ANALYSES / date / f"{doc_id}.json"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(made), str(dest))
                    stats["analyzed"] += 1
                    stats["input_tokens"].append(used)
                else:
                    fail(dest_root, date, doc_id, " ".join(answer[:120].split()))
                    stats["failed"] += 1
                stats["seconds"].append(round(took))
                persist(doc_id)
            say(f"{doc_id}: {'ok' if ok else 'FAILED ' + why[:120]} in {took:.0f}s"
                + (f", {used:,} input tokens" if used else ""))

    threads = [threading.Thread(target=work, args=(i,), name=f"lane-{i}") for i in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats["wall_seconds"] = round(time.monotonic() - started)
    return stats


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending")
    p.add_argument("--root", required=True)
    p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("run")
    p.add_argument("--root", required=True)
    p.add_argument("--repo", default="", help="the pilot-data checkout to commit from")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--budget-minutes", type=float, default=25)
    p.add_argument("--dry-run", type=int, default=0,
                   help="analyse this many recent HIGH notes and commit nothing")
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
        for row in pending(Path(a.root), a.limit):
            print("\t".join(row))
    elif a.cmd == "run":
        root = Path(a.root)
        if a.dry_run > 0:
            docs = recent_high(root, a.dry_run)
            st = run(root, workers=a.workers, budget_s=a.budget_minutes * 60, docs=docs,
                     out_root=Path(tempfile.mkdtemp(prefix="lane-dry-")))
        else:
            st = run(root, workers=a.workers, budget_s=a.budget_minutes * 60,
                     repo=Path(a.repo) if a.repo else None)
        secs = sorted(st.pop("seconds"))
        toks = sorted(st.pop("input_tokens"))
        done = st["analyzed"] + st["failed"]
        print(f"analyzed {st['analyzed']} document(s), {st['failed']} failed, "
              f"{a.workers} at a time, {st['wall_seconds']}s wall"
              + (f", per document median {secs[len(secs) // 2]}s max {secs[-1]}s" if secs else "")
              + (f", {done * 3600 // max(st['wall_seconds'], 1)} an hour" if done else "")
              + (f", input tokens per document median {toks[len(toks) // 2]:,}" if toks else "")
              + (", stopped by a usage limit" if st["limit"] else ""))
        if done and not st["analyzed"] and not st["limit"]:
            print("::error::every analysis failed this run")
            return 1
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
