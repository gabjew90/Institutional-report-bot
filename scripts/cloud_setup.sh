#!/bin/sh
# Set up a Claude Code cloud session (or any fresh Linux clone) to work on
# this repo the way the owner's local machine does (2026-10-01).
#
#   sh scripts/cloud_setup.sh
#
# Use it as the cloud environment's setup script, or run it at the start of
# a session. Safe to run twice. It:
#   1. builds .venv on Python 3.12 (the worker's version; the push gate
#      refuses any other) and installs the pinned requirements,
#   2. points git at the versioned push gate in .githooks/,
#   3. installs and links the Railway CLI when a Railway token is in the
#      environment (RAILWAY_API_TOKEN for an account token, RAILWAY_TOKEN
#      for a project token), so `railway ssh` / `railway logs` and the /ask
#      live harness work. Without a token it says so and skips.
# It never writes a secret to disk.
set -e
cd "$(git rev-parse --show-toplevel)"

echo "== python 3.12"
if [ ! -x .venv/bin/python ]; then
  if command -v python3.12 >/dev/null 2>&1; then
    python3.12 -m venv .venv
  elif command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
  else
    echo "no python3.12 and no uv: install one, then rerun" >&2
    exit 1
  fi
fi
.venv/bin/python --version
if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python -q -r requirements.txt pytest
else
  .venv/bin/python -m pip install -q -r requirements.txt pytest
fi

echo "== push gate"
chmod +x .githooks/pre-push
git config core.hooksPath .githooks
echo "pre-push hook: .githooks/pre-push"

# Nothing below may stop the script: Python and the push gate above are
# what a session needs first; memory and production access are reported,
# not required.
set +e

echo "== memory"
# Session memory lives in the PRIVATE repo gabjew90/institutional-report-bot-memory
# (notes about room members stay out of this public repo). Claude Code reads
# memory from ~/.claude/projects/<working dir with / as ->/memory; that path
# becomes a checkout of the private repo, so a memory written here is a
# commit away from every other session.
MEM_REPO="gabjew90/institutional-report-bot-memory"
# Claude Code names a project's directory by replacing every character that
# is not a letter or digit with '-' (C:\Users\gabje\Institutional-report-bot
# -> C--Users-gabje-Institutional-report-bot).
MEM_DIR="$HOME/.claude/projects/$(pwd | sed 's#[^A-Za-z0-9]#-#g')/memory"
if [ -d "$MEM_DIR/.git" ]; then
  git -C "$MEM_DIR" pull -q --ff-only && echo "memory: up to date at $MEM_DIR"
else
  mkdir -p "$(dirname "$MEM_DIR")"
  if [ -d "$MEM_DIR" ] && [ ! -L "$MEM_DIR" ]; then
    # a plain directory Claude Code made before setup ran: keep its files
    # beside the checkout rather than under it
    mv "$MEM_DIR" "$MEM_DIR.local-$(date +%s)" && echo "memory: moved an existing local memory dir aside"
  fi
  SIBLING="$(dirname "$(pwd)")/institutional-report-bot-memory"
  if [ -d "$SIBLING/.git" ]; then                 # attached to the session as a second repo
    ln -sfn "$SIBLING" "$MEM_DIR" && echo "memory: linked $SIBLING"
  elif git clone -q "https://github.com/$MEM_REPO.git" "$MEM_DIR" 2>/dev/null; then
    echo "memory: cloned to $MEM_DIR"
  elif [ -n "${GH_TOKEN:-}" ] && command -v gh >/dev/null 2>&1 \
       && gh repo clone "$MEM_REPO" "$MEM_DIR" -- -q 2>/dev/null; then
    echo "memory: cloned with GH_TOKEN"
  else
    echo "memory: NOT loaded. Attach $MEM_REPO to the session or set GH_TOKEN (read/write on that repo only)" >&2
  fi
fi

echo "== railway"
if [ -n "${RAILWAY_API_TOKEN:-}${RAILWAY_TOKEN:-}" ]; then
  if ! command -v railway >/dev/null 2>&1; then
    # global first; a non-root sandbox falls back to a user prefix
    npm install -g @railway/cli >/dev/null 2>&1 \
      || { npm install --prefix "$HOME/.local" @railway/cli >/dev/null 2>&1 \
           && export PATH="$HOME/.local/bin:$PATH"; }
  fi
  if ! command -v railway >/dev/null 2>&1; then
    echo "railway: could not install the CLI (no npm, or npm failed); production commands unavailable" >&2
  else
    if [ -n "${RAILWAY_API_TOKEN:-}" ]; then
      # an account token needs the project linked; a project token is
      # already scoped to one project and environment
      railway link --project marvelous-dream --environment production --service worker \
        || echo "railway: link failed; run 'railway link' by hand (the CLI may want the project ID)" >&2
    fi
    railway status || echo "railway: token present but status failed; check the token's scope" >&2
  fi
else
  echo "no RAILWAY_API_TOKEN / RAILWAY_TOKEN: production commands (logs, ssh, harness) unavailable"
fi

echo "== done. Tests: .venv/bin/python -m pytest tests -q"
