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

echo "== railway"
# Nothing below may stop the script: Python and the push gate above are
# what a session needs first; production access is reported, not required.
set +e
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
