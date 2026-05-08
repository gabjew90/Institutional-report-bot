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
    """
    files = _list_dir("pulse-output/progress")
    if not files:
        return None
    latest = sorted(files)[-1]  # ts-named, lexical sort = chronological
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/pulse-output/progress/{latest}"
    try:
        req = urllib.request.Request(url, headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
        events = data.get("events") or []
        if not events:
            return latest, "(file empty)"
        last = events[-1]
        step = last.get("step") or "(unnamed)"
        return latest, step
    except Exception as e:
        sys.stderr.write(f"watcher: latest_progress error: {e}\n")
        return None


def main() -> None:
    # Snapshot baseline so we only emit on NEW files, not pre-existing ones.
    baseline: dict[str, set[str]] = {p: _list_dir(p) for p in DIRS_TO_WATCH}
    last_progress: tuple[str, str] | None = None

    sys.stdout.write("watcher: armed; baseline file counts:\n")
    for p, files in baseline.items():
        sys.stdout.write(f"  {p}: {len(files)}\n")
    sys.stdout.flush()

    while True:
        for path, prefix in DIRS_TO_WATCH.items():
            current = _list_dir(path)
            new_files = sorted(current - baseline[path])
            for fname in new_files:
                sys.stdout.write(f"{prefix}: {fname}\n")
                sys.stdout.flush()
            baseline[path] = current

        progress = _latest_progress_event()
        if progress is not None and progress != last_progress:
            ts_file, step = progress
            sys.stdout.write(f"PROGRESS: step={step} (file={ts_file})\n")
            sys.stdout.flush()
            last_progress = progress

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
