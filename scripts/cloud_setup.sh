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
#
# It always exits 0 and ends with a SETUP SUMMARY: a cloud environment's
# setup script that exits non-zero stops the session from starting at all
# (2026-10-01, "cannot open scripts/cloud_setup.sh", exit 2), and a session
# that starts with one piece missing is more useful than no session.
PROBLEMS=""
problem() { echo "!! $1" >&2; PROBLEMS="$PROBLEMS
- $1"; }

# The environment's setup script runs from the home directory, not the
# checkout; find the checkout first.
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  for d in "$HOME/Institutional-report-bot" /home/user/Institutional-report-bot \
           "$(dirname "$0")/.."; do
    [ -f "$d/scripts/cloud_setup.sh" ] && cd "$d" && break
  done
fi
if [ ! -f "$(git rev-parse --show-toplevel 2>/dev/null)/scripts/cloud_setup.sh" ]; then
  # Carrying on here would build .venv and clone memory into the wrong
  # place; report and stop (still exit 0 so the session starts).
  echo "== SETUP SUMMARY"
  echo "checkout not found from $(pwd): run 'sh scripts/cloud_setup.sh' from inside Institutional-report-bot"
  exit 0
fi
cd "$(git rev-parse --show-toplevel)"
echo "repo: $(pwd)"

echo "== python 3.12"
if [ ! -x .venv/bin/python ]; then
  if ! command -v python3.12 >/dev/null 2>&1 && ! command -v uv >/dev/null 2>&1; then
    # uv fetches a 3.12 build when the image has none
    curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh >/dev/null 2>&1
    export PATH="$HOME/.local/bin:$PATH"
  fi
  if command -v python3.12 >/dev/null 2>&1; then
    python3.12 -m venv .venv
  elif command -v uv >/dev/null 2>&1; then
    uv venv -q --python 3.12 .venv
  fi
fi
if [ -x .venv/bin/python ]; then
  .venv/bin/python --version
  if command -v uv >/dev/null 2>&1; then
    uv pip install -q --python .venv/bin/python -r requirements.txt pytest \
      || problem "pip install failed (network access to pypi.org?)"
  else
    .venv/bin/python -m pip install -q -r requirements.txt pytest \
      || problem "pip install failed (network access to pypi.org?)"
  fi
else
  problem "no Python 3.12 (neither python3.12 nor uv could be found or installed)"
fi

echo "== push gate"
if [ -f .githooks/pre-push ]; then
  chmod +x .githooks/pre-push
  git config core.hooksPath .githooks && echo "pre-push hook: .githooks/pre-push"
else
  problem "not in the Institutional-report-bot checkout (no .githooks/pre-push)"
fi

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
    problem "memory NOT loaded: attach $MEM_REPO to the session, or set GH_TOKEN (fine-grained, contents read/write on that repo only)"
  fi
fi

echo "== railway"
if [ -n "${RAILWAY_API_TOKEN:-}${RAILWAY_TOKEN:-}" ]; then
  if ! command -v railway >/dev/null 2>&1; then
    # global first; a non-root sandbox falls back to a user prefix
    npm install -g @railway/cli >/dev/null 2>&1 \
      || { npm install -g --prefix "$HOME/.local" @railway/cli >/dev/null 2>&1 \
           && export PATH="$HOME/.local/bin:$PATH"; }
  fi
  # The session does not inherit this script's PATH: a CLI that landed in
  # ~/.local/bin goes onto a directory the session already searches.
  if [ -x "$HOME/.local/bin/railway" ]; then
    for bin in /usr/local/bin "$HOME/bin"; do
      if [ -d "$bin" ] && [ -w "$bin" ]; then
        ln -sf "$HOME/.local/bin/railway" "$bin/railway" && break
      fi
    done
  fi
  if ! command -v railway >/dev/null 2>&1; then
    problem "railway: could not install the CLI (no npm, or npm failed); production commands unavailable"
  else
    if [ -n "${RAILWAY_API_TOKEN:-}" ]; then
      # an account token needs the project linked; a project token is
      # already scoped to one project and environment
      railway link --project marvelous-dream --environment production --service worker \
        || problem "railway: link failed; run 'railway link' by hand (the CLI may want the project ID)"
    fi
    railway status || problem "railway: token present but status failed; check the token, and that RAILWAY_TOKEN is not also set"
  fi
else
  problem "no RAILWAY_API_TOKEN: production commands (logs, ssh, harness) unavailable"
fi

echo
echo "== SETUP SUMMARY"
if [ -z "$PROBLEMS" ]; then
  echo "all set: python 3.12, push gate, memory, railway. Tests: .venv/bin/python -m pytest tests -q"
  echo "railway: $(command -v railway) (use the full path if the session says 'command not found')"
else
  echo "set up with problems:$PROBLEMS"
fi
exit 0
