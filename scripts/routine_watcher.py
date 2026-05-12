"""Routine + bridge observability watcher.

Unified QC observability. Polls a small set of signal sources every 45s and
emits one stdout line per new event:

  PROGRESS:        step=<name> (latest event in pulse-output/progress/<ts>.json)
  QC_REVIEW:       <filename>  (new file in pulse-output/qc-reviews/ — the unified
                                quality artifact: routine review on success,
                                routine-failure marker on routine abort, or
                                <ts>.delivery.md sidecar on bridge delivery failure)
  POSTED:          <filename>  (new file in pulse-output/archive/ — pulse
                                successfully delivered to Discord and archived)

Designed to be invoked under the Monitor tool — each emitted line becomes a
chat notification. Persistent (does NOT exit on first event); user can call
TaskStop when done.

The "everything is QC" model: any event that affects the quality assessment of
a pulse — content, process, or delivery — lands in `pulse-output/qc-reviews/`
as either `<ts>.md` (routine's view) or `<ts>.delivery.md` (bridge's view).

Authenticates via env GH_TOKEN (passed by the Monitor invocation).
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

REPO = "gabjew90/Institutional-report-bot"
BRANCH = "pulse-data"
POLL_INTERVAL_SECONDS = 45

DIRS_TO_WATCH = {
    # name → emit prefix
    # qc-reviews holds:
    #   <ts>.md          — routine QC review (success) OR routine-failure marker
    #                      (STEP 2/6/7 failures relabel as QC reviews with
    #                      Status: FAILED at <stage>)
    #   <ts>.delivery.md — bridge delivery failure sidecar
    "pulse-output/qc-reviews": "QC_REVIEW",
    # archive holds successful pulse markdown after Discord delivery
    "pulse-output/archive": "POSTED",
}


def _auth_headers() -> dict[str, str]:
    token = (os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        sys.stderr.write("watcher: missing GH_TOKEN env var — running unauthenticated (will hit rate limits fast)\n")
        return {"Accept": "application/vnd.github+json"}
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


def _list_dir(path: str) -> set[str]:
    """Return the set of filenames in a GitHub directory. Empty on 404 or error."""
    url = f"https://api.github.com/repos/{REPO}/contents/{path}?ref={BRANCH}"
    try:
        req = urllib.request.Request(url, headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        if not isinstance(data, list):
            return set()
        return {item.get("name", "") for item in data if isinstance(item, dict) and item.get("name")}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return set()
        sys.stderr.write(f"watcher: list_dir({path}) HTTP {e.code}\n")
        return set()
    except Exception as e:
        sys.stderr.write(f"watcher: list_dir({path}) error: {e}\n")
        return set()


def _latest_progress_event() -> tuple[str, str] | None:
    """Return (ts_filename, last_event_step) for the newest progress file,
    or None if no progress files exist.

    Uses the GitHub Contents API (not raw.githubusercontent.com) because
    raw URLs have a 5-min HTTP cache that causes stale reads — the
    watcher would bounce between progress states ("STEP_5_LINT_DONE"
    flickering back to "STEP_3_5_DONE" because the raw cache returned an
    older version of the file). Contents API returns fresh data on every
    call.
    """
    files = _list_dir("pulse-output/progress")
    if not files:
        return None
    latest = sorted(files)[-1]  # ts-named, lexical sort = chronological
    url = f"https://api.github.com/repos/{REPO}/contents/pulse-output/progress/{latest}?ref={BRANCH}"
    try:
        req = urllib.request.Request(url, headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            file_meta = json.load(resp)
        # Contents API returns base64-encoded content; decode it.
        import base64
        raw_content = base64.b64decode(file_meta.get("content", "")).decode("utf-8")
        data = json.loads(raw_content)
        events = data.get("events") or []
        if not events:
            return latest, "(file empty)"
        last = events[-1]
        step = last.get("step") or "(unnamed)"
        return latest, step
    except Exception as e:
        sys.stderr.write(f"watcher: latest_progress error: {e}\n")
        return None


def _list_dir_retry(path: str, attempts: int = 4) -> set[str]:
    """_list_dir with retry. A transient GitHub API hiccup that returns
    an empty set for a directory that actually has files is the cause of
    the watcher's recurring "re-emit all existing files as new" noise:
    if the BASELINE snapshot comes back empty, the next successful poll
    sees every file as new. Retry with backoff so the baseline is built
    on a real listing, not a hiccup.
    """
    for i in range(attempts):
        s = _list_dir(path)
        if s:
            return s
        if i < attempts - 1:
            time.sleep(2 * (i + 1))
    return set()  # genuinely empty (or persistently failing — accept it)


def main() -> None:
    # Snapshot baseline so we only emit on NEW files, not pre-existing ones.
    # Use the retrying variant — a transient empty listing here is what
    # causes the "re-emit everything" noise on the next poll.
    baseline: dict[str, set[str]] = {p: _list_dir_retry(p) for p in DIRS_TO_WATCH}
    last_progress: tuple[str, str] | None = None

    sys.stdout.write("watcher: armed; baseline file counts:\n")
    for p, files in baseline.items():
        sys.stdout.write(f"  {p}: {len(files)}\n")
    sys.stdout.flush()

    while True:
        for path, prefix in DIRS_TO_WATCH.items():
            current = _list_dir(path)
            # Guard against transient empty/short listings: if `current`
            # is empty but the baseline had files, that's almost certainly
            # an API hiccup, not 24 files getting deleted. Skip this cycle
            # for this dir rather than treating the next poll's files as
            # all-new. Real new files are additive — they'll show up next
            # cycle once the listing recovers.
            if not current and baseline[path]:
                continue
            new_files = sorted(current - baseline[path])
            for fname in new_files:
                sys.stdout.write(f"{prefix}: {fname}\n")
                sys.stdout.flush()
            # Merge rather than replace, so a brief partial listing doesn't
            # "forget" files and re-emit them when the full listing returns.
            baseline[path] |= current

        progress = _latest_progress_event()
        if progress is not None and progress != last_progress:
            ts_file, step = progress
            sys.stdout.write(f"PROGRESS: step={step} (file={ts_file})\n")
            sys.stdout.flush()
            last_progress = progress

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
