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

Triage (2026-11-28, owner: "triage to Claude"). With TRIAGE_BACKEND=
claude_lane the worker hands a note over before any Gemini call, as one file
(lane/notes/<date>/<id>.json: the text and what the triage prompt needs), and
its entry on the list carries "triage": true. `run` triages it first with the
production triage prompt (scripts/pilot_triage.py) and writes lane/triage/
<date>/<id>.json, which the worker reads for the priority. A LOW note is done
there. A HIGH note is also copied to source-text/ in the layout the frozen
Omnipulse readers know, and HIGH and MEDIUM notes are then analysed as
before. Order inside a run: analyses of notes known to be HIGH, then triage
(a note not triaged yet may be HIGH), then the MEDIUM analyses.

Standard library plus this repo only: the runner does not install the
bot's requirements.

  pending   list what the worker waits for (TSV: id, text, meta, date; a note
            waiting for triage has "triage" in a fifth column)
  run       triage and analyse them, several at a time, committing each record
  prompt    write the prompt file for one document
  finalize  parse Claude's answer, build the analysis, write it
  fail      count a failed attempt; a document stops after MAX_ATTEMPTS
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
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
# Notes handed over before triage, and the triage the run wrote for each.
NOTES = "lane/notes"
TRIAGE = "lane/triage"
# Measured 2026-11-28 on 359 notes against Gemini's stored triage (NOTES.md):
# Sonnet analysed the same number of notes and two blind reviewers preferred
# its label on 29 of the 54 disputed notes they agreed on. Haiku gave another
# label on a second run for 11% of notes.
TRIAGE_MODEL = "claude-sonnet-5-5"


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


def _note_file(root: Path, date: str, doc_id: str) -> Path:
    return root / NOTES / date / f"{doc_id}.json"


def _triage_file(root: Path, date: str, doc_id: str) -> Path:
    return root / TRIAGE / date / f"{doc_id}.json"


def _read_json(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _listed(root: Path, now: dt.datetime):
    """The worker's entries a run may still work on: (entry, id, date)."""
    for e in load_waiting(root):
        doc_id, date = str(e.get("id") or ""), str(e.get("date") or "")
        # Both become path segments: digits and an ISO date, nothing else.
        if not doc_id.isdigit() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            continue
        deadline = _deadline(e)
        if deadline is not None and (deadline - now).total_seconds() < MIN_LEFT_S:
            continue
        f = _failures(root, date, doc_id)
        if f.exists() and json.loads(f.read_text()).get("attempts", 0) >= MAX_ATTEMPTS:
            continue
        yield e, doc_id, date


def untriaged(root: Path, now: dt.datetime | None = None) -> list[tuple[str, str, str]]:
    """Notes the worker handed over before triage that have none yet, in the
    worker's order: (id, the note's file, date)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for e, doc_id, date in _listed(root, now):
        if not e.get("triage") or _triage_file(root, date, doc_id).exists():
            continue
        note = _note_file(root, date, doc_id)
        if note.exists():       # else not in this checkout yet; the next refresh has it
            out.append((doc_id, str(note), date))
    return out


def note_source(note: dict, triage: dict) -> str:
    """The source a triaged note is filed under: the one the triage read,
    unless the note sits in a banned publication's Dropbox folder. That
    folder's name then stands, whatever author the text credits, so a repost
    never reaches the TRADE BOARD as a desk's own call (see finalize)."""
    from ai_analysis.analysis_build import canonical_source
    from ai_analysis.voice_rules import BANNED_PUBLICATION_NAMES
    banned = {canonical_source(b).lower() for b in BANNED_PUBLICATION_NAMES}
    folder = (note.get("folder_path") or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if folder and canonical_source(folder).lower() in banned:
        return canonical_source(folder)
    return canonical_source(str(triage.get("source") or ""))


# A triage record is written once and never changed, and the list is read
# again for every document a run takes: each record is read once per process.
_TRIAGED: dict[tuple[str, str, str], str] = {}
_SCRATCH: Path | None = None


def _triaged_priority(root: Path, date: str, doc_id: str) -> str:
    """The priority the run's triage gave a note, "" when it has none yet."""
    key = (str(root), date, doc_id)
    if key not in _TRIAGED:
        record = _read_json(_triage_file(root, date, doc_id))
        if record is None:
            return ""
        _TRIAGED[key] = str((record.get("triage") or {}).get("priority") or "").lower()
    return _TRIAGED[key]


def _materialize(root: Path, date: str, doc_id: str) -> tuple[str, str] | None:
    """A triaged note as the (text file, meta file) pair the analysis reads,
    written once, outside the checkout, in a folder of this process. None
    when the note or its triage is not in this checkout yet."""
    global _SCRATCH
    if _SCRATCH is None:
        _SCRATCH = Path(tempfile.mkdtemp(prefix="lane-notes-"))
    d = _SCRATCH / hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12] / date
    text, meta = d / f"{doc_id}.txt", d / f"{doc_id}.meta.json"
    if not meta.exists():
        record = _read_json(_triage_file(root, date, doc_id))
        note = _read_json(_note_file(root, date, doc_id)) if record else None
        if not note:
            return None
        d.mkdir(parents=True, exist_ok=True)
        text.write_text(note.get("text") or "", encoding="utf-8")
        meta.write_text(json.dumps({
            "pdf_file_id": int(doc_id), "file_name": note.get("file_name") or "",
            "source": record.get("source") or "",
            "priority": (record.get("triage") or {}).get("priority"),
            "total_pages": int(note.get("total_pages") or 0),
            "note_date": note.get("note_date"),
        }), encoding="utf-8")
    return str(text), str(meta)


def _analysis_rows(root: Path, now: dt.datetime) -> list[tuple[str, tuple[str, str, str, str]]]:
    """(priority, (id, text, meta, date)) for every listed note that can be
    analysed now and has no analysis, HIGH first, else in the worker's order."""
    out = []
    for e, doc_id, date in _listed(root, now):
        if (root / ANALYSES / date / f"{doc_id}.json").exists():
            continue
        priority = str(e.get("priority") or "").lower()
        meta = next((m for m in (root / d / date / f"{doc_id}.meta.json" for d in SOURCE_DIRS)
                     if m.exists()), None)
        if meta is not None:
            m = json.loads(meta.read_text(encoding="utf-8"))
            text = m.get("text_path") or ""
            # text_path is relative to the pilot-data checkout
            text_abs = root.parent / text if text else None
            if not text_abs or not text_abs.exists():
                continue
            row = (doc_id, str(text_abs), str(meta), date)
            priority = priority if priority in ("high", "medium") else str(m.get("priority") or "").lower()
        else:
            # handed over before triage: analysed once the triage says HIGH or MEDIUM
            priority = _triaged_priority(root, date, doc_id)
            if priority not in ("high", "medium"):
                continue    # no triage yet, or LOW
            paths = _materialize(root, date, doc_id)
            if paths is None:
                continue    # the text is not in this checkout yet
            row = (doc_id, *paths, date)
        out.append((priority, row))
    out.sort(key=lambda pr: pr[0] != "high")       # stable: the worker's order otherwise
    return out


def pending(root: Path, limit: int = 25,
            now: dt.datetime | None = None) -> list[tuple[str, str, str, str]]:
    """Documents the worker is waiting for that can be analysed now and have
    no analysis yet (HIGH first, nearest deadline first)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return [row for _, row in _analysis_rows(root, now)][:limit]


def write_triage(root: Path, date: str, doc_id: str, note: dict, res: dict, model: str) -> dict:
    """The triage record the worker reads, and for a HIGH note its copy in
    the Omnipulse readers' folder."""
    triage = {k: res.get(k) for k in ("priority", "report_type", "source", "key_tickers", "summary")}
    record = {
        "pdf_file_id": int(doc_id),
        "model": model,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": note_source(note, triage),
        "triage": triage,
        "usage": {"input_tokens": int(res.get("in") or 0) + int(res.get("cache_read") or 0)
                  + int(res.get("cache_new") or 0), "output_tokens": int(res.get("out") or 0)},
    }
    if triage["priority"] == "high":
        publish_for_readers(root, date, doc_id, note, record)
    f = _triage_file(root, date, doc_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record


def publish_for_readers(root: Path, date: str, doc_id: str, note: dict, record: dict) -> bool:
    """Copy a note triaged HIGH into source-text/<date>/, text and meta as
    github_bridge/pilot_publish.publish_high_document writes them. The worker
    cannot do it at hand-off any more: the priority is decided here."""
    from github_bridge.pilot_publish import _slug, title_from_file_name
    folder = root / SOURCE_DIRS[0] / date
    meta = folder / f"{doc_id}.meta.json"
    if meta.exists():
        return False
    title = title_from_file_name(note.get("file_name") or "")
    stem = f"{doc_id}__{_slug(title or note.get('file_name') or '')}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}.txt").write_text(note.get("text") or "", encoding="utf-8")
    meta.write_text(json.dumps({
        "pdf_file_id": int(doc_id),
        "file_name": note.get("file_name") or "",
        "source": record.get("source") or "",
        "title": title,
        "priority": "high",
        "published_at": None,
        "text_path": f"{root.name}/{SOURCE_DIRS[0]}/{date}/{stem}.txt",
        "truncated": bool(note.get("truncated")),
        "total_pages": int(note.get("total_pages") or 0),
        "note_date": note.get("note_date"),
    }, indent=1), encoding="utf-8")
    return True


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


def stage_untriaged(root: Path, dest: Path, n: int) -> int:
    """The rehearsal's input: the n newest HIGH notes of the branch written
    under `dest` as notes handed over before triage, with a list that waits
    for them. `dest` is outside the checkout, so a run on it commits nothing
    and the readers' real folder is not touched. The Dropbox folder is not
    kept in a published note's meta, so the triage sees an empty folder path."""
    rows = recent_high(root, n)
    deadline = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    waiting = []
    for doc_id, text, meta, date in rows:
        m = json.loads(Path(meta).read_text(encoding="utf-8"))
        f = _note_file(dest, date, doc_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({
            "pdf_file_id": int(doc_id), "file_name": m.get("file_name") or "", "folder_path": "",
            "total_pages": int(m.get("total_pages") or 0), "note_date": m.get("note_date"),
            "truncated": bool(m.get("truncated")),
            "text": Path(text).read_text(encoding="utf-8", errors="replace")}), encoding="utf-8")
        waiting.append({"id": int(doc_id), "date": date, "priority": "untriaged", "triage": True,
                        "deadline": deadline})
    (dest / WAITING).parent.mkdir(parents=True, exist_ok=True)
    (dest / WAITING).write_text(json.dumps({"waiting": waiting}), encoding="utf-8")
    return len(rows)


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


def _triage_call(prompt: str, model: str, cwd: str) -> dict:
    from scripts.pilot_triage import call
    return call(prompt, model, cwd)


def run(root: Path, *, workers: int = 4, budget_s: float = 1500, model: str = MODEL,
        ask=claude_cli, repo: Path | None = None, docs: list | None = None,
        out_root: Path | None = None, say=print, triage=_triage_call) -> dict:
    """Triage and analyse what the worker waits for, `workers` at a time,
    until the list is empty, the time budget is spent or Claude answers with
    a usage limit.

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
    claimed: set[tuple[str, str]] = set()
    stats = {"analyzed": 0, "triaged": 0, "failed": 0, "limit": False, "seconds": [],
             "input_tokens": []}
    last_sync = [0.0]
    dest_root = out_root or root
    fixed = list(docs) if docs is not None else None

    def claim():
        with lock:
            if stop.is_set() or time.monotonic() - started > budget_s:
                return None
            if fixed is not None:
                return ("analysis", fixed.pop(0)) if fixed else None
            if repo is not None and time.monotonic() - last_sync[0] >= REFRESH_S:
                _git(repo, "pull", "--rebase", "--quiet", "origin", PILOT_BRANCH)
                last_sync[0] = time.monotonic()
            now = dt.datetime.now(dt.timezone.utc)
            rows = _analysis_rows(root, now)
            # Known HIGH analyses, then triage (a note not triaged yet may be
            # HIGH), then the rest.
            todo = ([("analysis", row) for pri, row in rows if pri == "high"]
                    + [("triage", row) for row in untriaged(root, now)]
                    + [("analysis", row) for pri, row in rows if pri != "high"])
            for kind, row in todo:
                if (kind, row[0]) not in claimed:
                    claimed.add((kind, row[0]))
                    return kind, row
            return None

    def persist(label: str) -> None:
        if repo is None or out_root is not None:
            return
        rel = root.relative_to(repo).as_posix()
        # One path per call: git stages nothing at all when any path given
        # to one `git add` does not exist, and the failures folder may not.
        for folder in (ANALYSES, FAILURES, TRIAGE, SOURCE_DIRS[0]):
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
            got = claim()
            if got is None:
                return
            kind, row = got
            if kind == "triage":
                if not triage_one(row, tmp):
                    return
                continue
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
                    claimed.discard(("analysis", doc_id))
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

    def triage_one(row, tmp: Path) -> bool:
        """Triage one note. False stops this worker (a usage limit)."""
        doc_id, note_path, date = row
        t0 = time.monotonic()
        note = _read_json(Path(note_path)) or {}
        try:
            from scripts.pilot_triage import prompt_for
            res = triage(prompt_for(note.get("file_name") or "", note.get("folder_path") or "",
                                    note.get("text") or ""), TRIAGE_MODEL, str(tmp))
        except Exception as e:
            res = {"err": f"triage did not run: {e}"}
        if res.get("limit"):
            stop.set()
            with lock:
                stats["limit"] = True
                claimed.discard(("triage", doc_id))
            say("::warning::Claude usage limit reached; stopping, nothing counted as failed")
            return False
        ok = bool(res.get("priority")) and not res.get("err")
        with lock:
            if ok:
                try:
                    write_triage(dest_root, date, doc_id, note, res, TRIAGE_MODEL)
                except Exception as e:      # a write that failed is an attempt, not a dead worker
                    ok, res = False, {"err": f"triage not written: {e}"}
            if ok:
                stats["triaged"] += 1
            else:
                fail(dest_root, date, doc_id, "triage: " + " ".join(str(res.get("err") or "")[:120].split()))
                stats["failed"] += 1
            persist(f"triage {doc_id}")
        say(f"{doc_id}: triage {res.get('priority') if ok else 'FAILED ' + str(res.get('err'))[:120]} "
            f"in {time.monotonic() - t0:.0f}s")
        return True

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
    p.add_argument("--dry-run-triage", type=int, default=0,
                   help="triage and analyse this many recent HIGH notes as if they had been "
                        "handed over before triage, and commit nothing")
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
        for row in untriaged(Path(a.root))[:a.limit]:
            print("\t".join((row[0], row[1], "", row[2], "triage")))
    elif a.cmd == "run":
        root = Path(a.root)
        if a.dry_run_triage > 0:
            staged = Path(tempfile.mkdtemp(prefix="lane-dry-triage-")) / root.name
            n = stage_untriaged(root, staged, a.dry_run_triage)
            print(f"rehearsal: {n} note(s) staged for triage outside the checkout")
            st = run(staged, workers=a.workers, budget_s=a.budget_minutes * 60)
            for f in sorted((staged / TRIAGE).glob("*/*.json")):
                r = json.loads(f.read_text(encoding="utf-8"))
                print(f"  {r['pdf_file_id']}: {r['triage']['priority']} ({r['source']})")
            print(f"  copied for the readers: {len(list((staged / SOURCE_DIRS[0]).glob('*/*.meta.json')))}")
        elif a.dry_run > 0:
            docs = recent_high(root, a.dry_run)
            st = run(root, workers=a.workers, budget_s=a.budget_minutes * 60, docs=docs,
                     out_root=Path(tempfile.mkdtemp(prefix="lane-dry-")))
        else:
            st = run(root, workers=a.workers, budget_s=a.budget_minutes * 60,
                     repo=Path(a.repo) if a.repo else None)
        secs = sorted(st.pop("seconds"))
        toks = sorted(st.pop("input_tokens"))
        done = st["analyzed"] + st["failed"]
        print(f"triaged {st['triaged']}, analyzed {st['analyzed']} document(s), {st['failed']} failed, "
              f"{a.workers} at a time, {st['wall_seconds']}s wall"
              + (f", per document median {secs[len(secs) // 2]}s max {secs[-1]}s" if secs else "")
              + (f", {done * 3600 // max(st['wall_seconds'], 1)} an hour" if done else "")
              + (f", input tokens per document median {toks[len(toks) // 2]:,}" if toks else "")
              + (", stopped by a usage limit" if st["limit"] else ""))
        if done and not st["analyzed"] and not st["triaged"] and not st["limit"]:
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
