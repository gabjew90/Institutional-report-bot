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
    uv pip install -q --python .venv/bin/python -r requirements.txt -r requirements-dev.txt \
      || problem "pip install failed (network access to pypi.org?)"
  else
    .venv/bin/python -m pip install -q -r requirements.txt -r requirements-dev.txt \
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
# Memory lives on the memory repo's main branch. A repo attached to the
# session arrives on a throwaway claude/... branch with no upstream, and a
# memory committed there never reaches main or the owner's machine. Move
# to main when that loses nothing (HEAD already contained in origin/main),
# then fast-forward.
mem_on_main() {
  git -C "$1" fetch -q origin main 2>/dev/null || { problem "memory: cannot fetch origin/main"; return; }
  if [ "$(git -C "$1" rev-parse --abbrev-ref HEAD)" != main ]; then
    if git -C "$1" merge-base --is-ancestor HEAD origin/main \
       && [ -z "$(git -C "$1" status --porcelain)" ]; then
      git -C "$1" checkout -q -B main origin/main
    else
      problem "memory: checkout is on $(git -C "$1" rev-parse --abbrev-ref HEAD) with work not on main; merge it into main by hand"
      return
    fi
  fi
  git -C "$1" branch -q --set-upstream-to=origin/main main 2>/dev/null
  git -C "$1" merge -q --ff-only origin/main && echo "memory: on main, up to date"
}
if [ -d "$MEM_DIR/.git" ]; then
  mem_on_main "$MEM_DIR"
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
    mem_on_main "$SIBLING"
  elif git clone -q "https://github.com/$MEM_REPO.git" "$MEM_DIR" 2>/dev/null; then
    echo "memory: cloned to $MEM_DIR"
  elif [ -n "${GH_TOKEN:-}" ] && command -v gh >/dev/null 2>&1 \
       && gh repo clone "$MEM_REPO" "$MEM_DIR" -- -q 2>/dev/null; then
    echo "memory: cloned with GH_TOKEN"
  else
    problem "memory NOT loaded: attach $MEM_REPO to the session, or set GH_TOKEN (fine-grained, contents read/write on that repo only)"
  fi
fi

# A cloud session can start one level up (/home/user, 2026-10-02) with this
# repo as a subfolder. The repo's .claude/settings.json hooks then do not
# load, and memory is read from the parent's project folder. Give the
# parent the same hooks (absolute paths, so they work from there) and point
# its memory folder at the same checkout. Skipped when the parent is $HOME,
# where .claude/settings.json would be the user-level file.
PARENT="$(dirname "$(pwd)")"
if [ "$PARENT" != "$HOME" ] && [ -e "$MEM_DIR" ]; then
  PARENT_MEM="$HOME/.claude/projects/$(printf %s "$PARENT" | sed 's#[^A-Za-z0-9]#-#g')/memory"
  if [ -d "$PARENT_MEM" ] && [ ! -L "$PARENT_MEM" ]; then
    mv "$PARENT_MEM" "$PARENT_MEM.local-$(date +%s)"
  fi
  if [ ! -e "$PARENT_MEM" ]; then
    mkdir -p "$(dirname "$PARENT_MEM")" \
      && ln -sfn "$(readlink -f "$MEM_DIR")" "$PARENT_MEM" \
      && echo "memory: also linked for sessions started in $PARENT"
  fi
  # (re)write it when absent or ours, so a stale copy is refreshed; a file
  # someone else made is left alone
  if [ -w "$PARENT" ] && { [ ! -e "$PARENT/.claude/settings.json" ] \
       || grep -q "session_sync.py" "$PARENT/.claude/settings.json"; }; then
    SYNC="$(pwd)/scripts/session_sync.py"
    mkdir -p "$PARENT/.claude" && printf '%s\n' \
      '{"hooks": {' \
      "  \"SessionStart\": [{\"matcher\": \"startup|resume\", \"hooks\": [{\"type\": \"command\", \"command\": \"python3 $SYNC start || exit 0\", \"timeout\": 90}]}]," \
      "  \"Stop\": [{\"hooks\": [{\"type\": \"command\", \"command\": \"python3 $SYNC stop || exit 0\", \"timeout\": 90}]}]" \
      '}}' > "$PARENT/.claude/settings.json" \
      && echo "hooks: sync hooks also set for sessions started in $PARENT"
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
  elif [ -n "${RAILWAY_API_TOKEN:-}" ] && ! railway whoami >/dev/null 2>&1; then
    # Check the token before linking, so a bad paste reads as one problem
    # about the token. Describe its shape, never its value: an account
    # token is a 36-character UUID, and a paste that kept quotes, a space
    # or a newline is rejected as Unauthorized.
    shape="${#RAILWAY_API_TOKEN} chars"
    case "$RAILWAY_API_TOKEN" in *[!0-9a-fA-F-]*) shape="$shape, contains characters a UUID token does not (quotes, spaces, newline?)" ;; esac
    problem "railway: token rejected ($shape; a valid account token is 36 chars). Re-paste it in the environment settings, value only, no quotes"
  else
    if [ -n "${RAILWAY_API_TOKEN:-}" ]; then
      # an account token needs the project linked; a project token is
      # already scoped to one project and environment
      railway link --project marvelous-dream --environment production --service worker \
        || problem "railway: the token works but linking marvelous-dream/production/worker failed; run 'railway link' by hand"
    fi
    railway status || problem "railway: status failed after linking"
    # `railway ssh` (logs and DB probes on the worker, the /ask harness)
    # authenticates with an SSH key registered on the Railway account, and
    # a cloud container starts with none. The dedicated cloud key lives in
    # the environment settings as one base64 line, the private key file
    # encoded, and is written here each session. Never printed.
    if [ -n "${RAILWAY_SSH_KEY_B64:-}" ] && ! command -v ssh-keygen >/dev/null 2>&1 \
       && command -v apt-get >/dev/null 2>&1; then
      # the cloud image ships without ssh (2026-10-02)
      SUDO=""; [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1 && SUDO=sudo
      { $SUDO apt-get update -qq && DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq openssh-client; } \
        >/dev/null 2>&1 && echo "railway ssh: installed openssh-client"
    fi
    if [ -n "${RAILWAY_SSH_KEY_B64:-}" ] && ! command -v ssh-keygen >/dev/null 2>&1; then
      problem "railway ssh: the image has no ssh-keygen (openssh-client), so the cloud key cannot be installed"
    elif [ -n "${RAILWAY_SSH_KEY_B64:-}" ]; then
      mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
      NEW="$HOME/.ssh/.railway_cloud_key.tmp"
      ( umask 077; printf %s "$RAILWAY_SSH_KEY_B64" | tr -d " \r\n\"'" | base64 -d > "$NEW" 2>/dev/null )
      if ! ssh-keygen -y -f "$NEW" >/dev/null 2>&1; then
        rm -f "$NEW"
        problem "railway ssh: RAILWAY_SSH_KEY_B64 is set but does not decode to a valid private key; re-paste it"
      else
        # The default name, because `railway ssh` hands off to ssh, which
        # offers only default-named keys unless given -i. A different key
        # already there is left alone (a re-run finds its own key: fine).
        KEY="$HOME/.ssh/id_ed25519"
        if [ -s "$KEY" ] && ! cmp -s "$KEY" "$NEW"; then
          KEY="$HOME/.ssh/railway_cloud_ed25519"
          problem "railway ssh: another ~/.ssh/id_ed25519 exists; the cloud key is at $KEY, pass '-i $KEY' to railway ssh"
        fi
        mv -f "$NEW" "$KEY" && chmod 600 "$KEY" && ssh-keygen -y -f "$KEY" > "$KEY.pub" \
          && echo "railway ssh: cloud key installed at $KEY"
        # A key on disk is useless unless the Railway account knows it
        # (never registered, or revoked since).
        FP="$(ssh-keygen -lf "$KEY.pub" 2>/dev/null | awk '{print $2}')"
        if [ -n "$FP" ] && ! railway ssh keys list 2>/dev/null | grep -qF "$FP"; then
          problem "railway ssh: the cloud key ($FP) is not registered on the Railway account; register its public half or ssh is refused"
        fi
        # The environment's network policy can block port 22, and then
        # `railway ssh` hangs instead of failing (2026-10-02). Probe it with
        # a short timeout. accept-new also records the host key, so the
        # first real connection does not stop at a prompt.
        out="$(ssh -o ConnectTimeout=6 -o BatchMode=yes -o StrictHostKeyChecking=accept-new ssh.railway.com exit 2>&1)"
        case "$out" in
          *"timed out"*|*"unreachable"*|*"Could not resolve"*)
            problem "railway ssh: cannot reach ssh.railway.com port 22 (cloud network policy). Set the environment's Network access to Full, or allow ssh.railway.com" ;;
        esac
      fi
    else
      problem "railway ssh unavailable (no RAILWAY_SSH_KEY_B64): logs and DB probes on the worker will not run"
    fi
  fi
else
  problem "no RAILWAY_API_TOKEN: production commands (logs, ssh, harness) unavailable"
fi

echo "== session sync"
# The start hook can miss the session it was meant for: the cloud loads
# settings a few seconds before this script writes the parent folder's
# hooks (2026-10-02). Run the start sync here too, so moving off a
# throwaway branch and pulling memory never depend on hook timing.
SYNC_PY=.venv/bin/python; [ -x "$SYNC_PY" ] || SYNC_PY=python3
"$SYNC_PY" scripts/session_sync.py start || true

echo
echo "== SETUP SUMMARY"
if [ -z "$PROBLEMS" ]; then
  echo "all set: python 3.12, push gate, memory, railway. Tests: .venv/bin/python -m pytest tests -q"
  echo "railway: $(command -v railway) (use the full path if the session says 'command not found')"
else
  echo "set up with problems:$PROBLEMS"
fi
exit 0
