"""Keep the owner's local and cloud Claude Code sessions in sync (2026-10-01).

Wired as two hooks in .claude/settings.json, so both sides run it:

  start  (SessionStart) pulls the code repo and the memory repo. A checkout
         on a throwaway claude/... branch (how a cloud session attaches a
         repo) is moved to the branch that matters, deploy branch for code
         and main for memory, but only when that loses nothing: a clean
         tree whose HEAD is already contained in the target. Anything else
         is reported, never forced. Unpushed commits are reported too.
  stop   (Stop, after every reply) commits and pushes changed memory files,
         so a memory written on one side reaches the other without anyone
         remembering. A rejected push is rebased once and retried. A
         conflict is aborted and reported.

The code repo is never committed or pushed from here: code goes out
through the push gate when the work is done. Every path exits 0, because
a hook that fails can stall the session it exists to help. Lines printed
by `start` reach the session as context.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEPLOY_BRANCH = "claude/financial-pdf-discord-bot-mDpbk"  # Railway deploys this
MEMORY_BRANCH = "main"


def git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"


def project_dir() -> Path:
    # The repo this script lives in, not CLAUDE_PROJECT_DIR: a cloud session
    # can start one level up (/home/user) with the repo as a subfolder.
    return Path(__file__).resolve().parents[1]


def memory_dir(project: Path) -> Path:
    # Claude Code names a project's folder by replacing every character that
    # is not a letter or digit with '-'.
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
    return Path.home() / ".claude" / "projects" / slug / "memory"


def sync_to(repo: Path, target: str, label: str) -> list[str]:
    """Fetch, move onto `target` when that loses nothing, fast-forward.
    Returns report lines; an empty list means up to date and quiet."""
    if not (repo / ".git").exists():
        return [f"{label}: {repo} is not a git checkout; not synced"]
    rc, out = git(repo, "fetch", "-q", "origin", target, timeout=60)
    if rc:
        return [f"{label}: fetch of origin/{target} failed ({out.splitlines()[-1] if out else 'no output'})"]
    notes = []
    _, branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _, dirty = git(repo, "status", "--porcelain")
    if branch != target:
        contained = git(repo, "merge-base", "--is-ancestor", "HEAD", f"origin/{target}")[0] == 0
        # `checkout -B` resets a local target branch: refuse when that
        # branch holds commits origin does not have.
        local_exists = git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{target}")[0] == 0
        if local_exists and git(repo, "merge-base", "--is-ancestor", target, f"origin/{target}")[0]:
            return [f"{label}: local {target} has commits not on origin and HEAD is on {branch}; left as is"]
        if contained and not dirty:
            rc, out = git(repo, "checkout", "-q", "-B", target, f"origin/{target}")
            if rc:
                return [f"{label}: could not switch from {branch} to {target}: {out}"]
            git(repo, "branch", "-q", f"--set-upstream-to=origin/{target}", target)
            notes.append(f"{label}: moved from {branch} to {target}")
        else:
            return [f"{label}: on {branch}, which has work not on {target}; left as is, merge it by hand"]
    if dirty:
        notes.append(f"{label}: uncommitted changes present; pull skipped")
        return notes
    rc, out = git(repo, "merge", "-q", "--ff-only", f"origin/{target}")
    if rc:
        notes.append(f"{label}: local {target} and origin/{target} have diverged; not merged")
    _, ahead = git(repo, "rev-list", "--count", f"origin/{target}..HEAD")
    if ahead.isdigit() and int(ahead):
        notes.append(f"{label}: {ahead} local commit(s) not pushed to origin/{target}")
    return notes


def start() -> None:
    project = project_dir()
    lines = sync_to(project, DEPLOY_BRANCH, "code") + sync_to(memory_dir(project), MEMORY_BRANCH, "memory")
    if Path(os.getcwd()).resolve() != project:
        # Started outside the repo, so its CLAUDE.md (binding rules) is not
        # loaded yet. This line reaches the session as context.
        lines.append(f"this session started in {os.getcwd()}, not the repo. "
                     f"Work in {project} and read {project / 'CLAUDE.md'} before anything else.")
    if lines:
        print("session sync:\n" + "\n".join(f"- {l}" for l in lines))


def stop() -> None:
    mem = memory_dir(project_dir())
    if not (mem / ".git").exists():
        return
    _, changed = git(mem, "status", "--porcelain")
    if not changed:
        return
    git(mem, "add", "-A")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rc, out = git(mem, "commit", "-q", "-m", f"memory: session update {stamp}")
    if rc:
        print(f"memory sync: commit failed: {out}", file=sys.stderr)
        return
    if git(mem, "push", "-q", "origin", f"HEAD:{MEMORY_BRANCH}", timeout=60)[0] == 0:
        return
    # The other side pushed first: replay this commit on top, once.
    if git(mem, "pull", "-q", "--rebase", "origin", MEMORY_BRANCH, timeout=60)[0]:
        git(mem, "rebase", "--abort")
        print("memory sync: conflict with the other session's memory; committed locally, not pushed. Merge by hand.", file=sys.stderr)
        return
    rc, out = git(mem, "push", "-q", "origin", f"HEAD:{MEMORY_BRANCH}", timeout=60)
    if rc:
        print(f"memory sync: push failed: {out}", file=sys.stderr)


if __name__ == "__main__":
    try:
        {"start": start, "stop": stop}[sys.argv[1]]()
    except Exception as e:  # never fail the session
        print(f"session sync error: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(0)
