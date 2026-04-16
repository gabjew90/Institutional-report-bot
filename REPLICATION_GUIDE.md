# Replication Guide: Claude Code + GitHub + Railway

End-to-end blueprint for setting up a new project with the same architecture as the Institutional Research Bot:

- **Laptop (VS Code + Claude Code extension)** for development
- **Mobile Claude app** for remote control when you're away from the laptop
- **GitHub** as the single source of truth for code
- **Railway** for always-on production deployment that auto-redeploys on every `git push`

Result: you can dictate changes to Claude from your phone, which drives your laptop, which pushes to GitHub, which triggers a Railway redeploy — all hands-free after initial setup.

---

## One-Time Setup (do this once per laptop)

### Required tools

| Tool | Install | Purpose |
|---|---|---|
| VS Code | [code.visualstudio.com](https://code.visualstudio.com) | Editor |
| Claude Code VS Code extension | VS Code Extensions panel → search "Claude Code" | Lets Claude run commands + edit files on your laptop |
| Git | [git-scm.com](https://git-scm.com/) | Version control |
| GitHub account | [github.com](https://github.com) | Code hosting |
| GitHub CLI (optional but recommended) | `winget install GitHub.cli` (Win) / `brew install gh` (Mac) | Create repos from terminal |
| Node.js 18+ | [nodejs.org](https://nodejs.org) | Required for Railway CLI |
| Railway CLI | `npm install -g @railway/cli` | Deploy + log access |
| Railway account | [railway.app](https://railway.app) — sign in with GitHub | Hosting |

### One-time auth (run in a terminal)

```bash
gh auth login                  # GitHub CLI auth (use HTTPS + browser)
git config --global user.name "Your Name"
git config --global user.email "your@email.com"
railway login                  # Browser auth for Railway CLI
```

After this, `git push`, `gh`, and `railway` commands work headless forever. Claude can run them on your behalf without re-authing.

### Link mobile Claude to laptop

In the Claude mobile app: **Settings → Linked devices → Link laptop**. Follow the pairing flow. Once linked, messages you type on the phone get executed by the Claude Code session running on your laptop.

---

## Per-Project Setup (~10-15 minutes)

### Step 1: Initialize the project locally

```bash
mkdir my-new-project && cd my-new-project
git init -b main
# ... add your code ...
```

Create these files at the root of every project:

**`.gitignore`** (minimum):
```
.env
data/
*.db
__pycache__/
.venv/
.claude/
```

**`.env.example`** (template that gets committed — never commit actual `.env`):
```
# Service keys
API_KEY_NAME=
OTHER_KEY=

# Deployment paths (use absolute /data/... if using a Railway volume)
DB_PATH=/data/app.db
```

**`CLAUDE.md`** — short overview that new Claude sessions read first. Cover:
- What the project does in 2 paragraphs
- Architecture diagram (ASCII is fine)
- Key design decisions (so future sessions don't undo them)
- Env vars list
- How to access production state (Railway CLI commands)

**`Procfile`** (Railway uses this to know how to start the app):
```
worker: python main.py
```

Or use `railway.toml` for more control:
```toml
[build]
builder = "nixpacks"

[deploy]
startCommand = "python main.py"
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 5
```

**`nixpacks.toml`** (if you need system packages, e.g., poppler for PDFs):
```toml
[phases.setup]
aptPkgs = ["poppler-utils"]
```

### Step 2: Push to GitHub

```bash
git add .
git commit -m "Initial commit"
gh repo create my-new-project --public --source=. --push
```

(Or create the repo manually on github.com and `git remote add origin ... && git push -u origin main`.)

### Step 3: Deploy to Railway

1. Go to [railway.app/new](https://railway.app/new) → **Deploy from GitHub repo**
2. Authorize Railway to access your GitHub account (one-time)
3. Pick the repo you just pushed
4. Railway auto-starts a deploy. It'll fail on the first run if env vars aren't set — expected.
5. Click the **service card** → **Variables** tab → paste your env vars (or use **Raw Editor**)
6. If you need persistent storage, click **Settings → Volumes → + New Volume**. Mount path `/data` is a common convention.
7. If using a volume, update env vars to use absolute paths: `DB_PATH=/data/app.db`, not `data/app.db`. Relative paths write to ephemeral container storage and get wiped on every redeploy.
8. Click **Deploy** to apply. The next push to the GitHub repo triggers auto-redeploys.

### Step 4: Link the project directory to Railway CLI

Still in your terminal:

```bash
cd my-new-project
railway link
# Follow prompts: select workspace → project → environment → service
```

This writes `.railway/config.json` locally so `railway logs`, `railway ssh`, `railway variable set` all work without specifying the project each time.

### Step 5: Open the project in Claude Code

```bash
code my-new-project
```

In VS Code, open Claude Code. The first message should point it at `CLAUDE.md` so it has context:

> Please read `CLAUDE.md` first. It has the architecture and design decisions for this project. Confirm you understand, then I'll tell you what to work on.

---

## Daily Workflow

### Making a change

1. Tell Claude what you want (from phone or laptop)
2. Claude edits files
3. You approve the change (or Claude can commit directly if auto-approved)
4. `git push` triggers Railway redeploy
5. ~1-2 min later the new version is live

### Checking production

| You want to see | Claude runs |
|---|---|
| Recent logs | `railway logs --deployment \| tail -100` |
| Error patterns | `railway logs --deployment 2>&1 \| grep -iE "ERROR\|Traceback"` |
| Env vars | `railway variables --service <name>` |
| Set env var | `railway variable set --service <name> "KEY=value"` (triggers redeploy) |
| Query prod DB | `railway ssh "python -c 'import sqlite3; ...'"` |
| Redeploy manually | `railway redeploy` |

From mobile Claude (no shell access), Claude asks you to run these in your laptop terminal and paste the output. Pre-written commands in CLAUDE.md make this smooth.

---

## Gotchas I Learned the Hard Way

### 1. Claude Code auto-creates a branch per session
Branches like `claude/my-feature-xyz` appear automatically. **If Railway watches `main` but Claude pushes to its own branch, your changes don't deploy.** Fix: have Claude checkout `main` at the start of each session, or configure Railway to watch the session branch, or merge to `main` before expecting a deploy.

### 2. Shared env vars ≠ Service env vars (on Railway)
Variables set at the project "Shared" level are NOT auto-exposed to services. Set them on the specific service's Variables tab.

### 3. Relative paths get wiped on every redeploy
Railway containers have ephemeral filesystems. Volumes persist; everything else doesn't. Always use absolute paths (`/data/app.db`) in env vars and code when the file needs to survive redeploys.

### 4. Discord bot tokens can only have ONE active connection
Running two Railway services with the same bot token = they disconnect each other in a loop. For multiple bots, create multiple Discord applications.

### 5. GitHub push needs auth cached
First `git push` pops a browser window. After that, Git Credential Manager caches the token and `git push` works silently — critical for Claude running headlessly.

### 6. Claude might be on a different branch than you think
Always `git status` at session start. If Claude is on a weird branch, pushes won't go where you expect.

### 7. Railway CLI refuses non-interactive login
`railway login` requires a browser popup. You must run it yourself at least once per machine. After that, Claude can use the cached token.

---

## File Templates to Copy for Every New Project

These live in any language / runtime. Adapt as needed.

**`.env.example`** — document every env var
**`.gitignore`** — see above
**`CLAUDE.md`** — project context (architecture, env vars, access patterns)
**`Procfile`** — Railway start command
**`nixpacks.toml`** (optional) — system packages
**`railway.toml`** (optional) — deploy config

---

## What You Get After Setup

- **Code on GitHub**: single source of truth, version controlled, reviewable
- **Production on Railway**: always-on, auto-redeploys on every push, ~$5/month for a worker
- **Claude on laptop (VS Code)**: can edit code, run commands, deploy, inspect production
- **Claude on mobile**: relays through laptop — you can tell Claude to "check production logs and fix the bug" while walking the dog
- **Rollback path**: `git revert` + push → Railway rolls back automatically

Total time to reach "production-deployed with mobile remote control" on a greenfield project: ~30 minutes including all account signups.
