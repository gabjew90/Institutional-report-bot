# HIGH-Priority PDF Ingestion Routine (Opus deep analysis)

> **Version-controlled source for the Claude.ai scheduled-routine prompt.**
> The cron-fire bootstrap fetches this file and executes it verbatim. Edit
> here, `git push`, and the next fire picks up the change.

**Routine surface assumptions:**
- Bash + Python 3 available
- File I/O via `/tmp/...` paths persists for the duration of the fire
- Anthropic's native PDF `Read` tool can open `.pdf` files directly

---

Parallel HIGH-priority deep analysis using GitHub as the message bus.
Railway commits raw PDFs to `ingest-pending/<pdf_file_id>.{pdf,json}` on
the bridge branch; this routine reads them, runs deep analysis, commits
results back; Railway's pull job ingests the results into `pdf_analyses`.

**Constants**
```
REPO: gabjew90/Institutional-report-bot
WORKING_BRANCH: claude/financial-pdf-discord-bot-mDpbk
BRIDGE_BRANCH: pulse-data
GH_TOKEN: ${GH_TOKEN}
```

## STEP 1 — Read the analysis system prompt

```bash
curl -sS -H "Authorization: token $GH_TOKEN" \
  https://raw.githubusercontent.com/gabjew90/Institutional-report-bot/claude/financial-pdf-discord-bot-mDpbk/ai_analysis/prompts.py \
  -o /tmp/prompts.py
python3 -c "
import re
src = open('/tmp/prompts.py').read()
# Grab ANALYSIS_SYSTEM_PROMPT (triple-quoted string)
m = re.search(r'ANALYSIS_SYSTEM_PROMPT\s*=\s*\"\"\"(.*?)\"\"\"', src, re.DOTALL)
if not m:
    raise SystemExit('ANALYSIS_SYSTEM_PROMPT not found')
prompt = m.group(1)
open('/tmp/analysis_system_prompt.txt', 'w').write(prompt)
print(f'analysis system prompt: {len(prompt)} chars, {prompt.count(chr(10))} lines')
"
```

The fetched prompt has all the schema rules (key_insights, market_movers,
theme_stances with evidence-anchoring, key_data_points, tension_points,
etc.), the LOW/MEDIUM/HIGH priority guidance, jargon rules, and the
anti-hallucination posture. Apply it to every PDF you process below.

## STEP 2 — List pending PDFs

```bash
mkdir -p /tmp/bridge
cd /tmp/bridge
# Fetch the listing of ingest-pending/ via the GitHub contents API
curl -sS -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/gabjew90/Institutional-report-bot/contents/ingest-pending?ref=pulse-data" \
  -o /tmp/bridge/listing.json

python3 << 'PYEOF'
import json
listing = json.load(open('/tmp/bridge/listing.json'))
if not isinstance(listing, list):
    print('listing not an array — likely empty bridge or auth issue')
    print(listing)
    raise SystemExit(0)
# Collect (pdf_file_id) where BOTH .pdf and .json exist (json sidecar = ready)
ids_with_pdf = set()
ids_with_json = set()
for item in listing:
    name = item.get('name', '')
    if name.endswith('.pdf'):
        try: ids_with_pdf.add(int(name[:-4]))
        except: pass
    elif name.endswith('.json'):
        try: ids_with_json.add(int(name[:-5]))
        except: pass
ready = sorted(ids_with_pdf & ids_with_json)
print(f'pending: {len(ready)} PDFs ready ({len(ids_with_pdf)} pdf files, {len(ids_with_json)} json sidecars)')
print('IDs to process:', ready[:20], ('...' if len(ready) > 20 else ''))
open('/tmp/bridge/ready_ids.json', 'w').write(json.dumps(ready))
PYEOF
```

If `ready_ids.json` is empty, jump to STEP 5 (commit & report).

## STEP 3 — Download each PDF + sidecar

```bash
python3 << 'PYEOF'
import json, base64, urllib.request, os
ids = json.load(open('/tmp/bridge/ready_ids.json'))
GH_TOKEN = os.environ['GH_TOKEN']
hdr = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}

def fetch_raw(path):
    url = f'https://raw.githubusercontent.com/gabjew90/Institutional-report-bot/pulse-data/{path}'
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

n_ok = 0
for pid in ids:
    try:
        pdf_bytes = fetch_raw(f'ingest-pending/{pid}.pdf')
        sidecar = fetch_raw(f'ingest-pending/{pid}.json').decode('utf-8')
        open(f'/tmp/bridge/{pid}.pdf', 'wb').write(pdf_bytes)
        open(f'/tmp/bridge/{pid}.json', 'w').write(sidecar)
        n_ok += 1
    except Exception as e:
        print(f'WARN: download failed for pdf_file_id={pid}: {e}')
print(f'downloaded: {n_ok}/{len(ids)} PDFs to /tmp/bridge/')
PYEOF
```

## STEP 4 — Analyze each PDF and emit result JSON

For EACH `pdf_file_id` in `/tmp/bridge/ready_ids.json` that downloaded successfully:

1. **Read the sidecar** at `/tmp/bridge/<id>.json` to get `file_name`,
   `dropbox_path`, `dropbox_modified_at`, `page_count`, `file_size_bytes`.
2. **Read the PDF** via the native `Read` tool: `Read("/tmp/bridge/<id>.pdf")`.
   Anthropic's PDF support converts pages to image+text under the hood.
3. **Apply** the system prompt rules from `/tmp/analysis_system_prompt.txt`.
   Treat this as the system prompt for analyzing THIS PDF specifically.
4. **Emit a single JSON object** matching the `PdfAnalysis` shape (see
   `ai_analysis/models.py:PdfAnalysis`). Required fields: `pdf_file_id`,
   `file_name`, `source`, `title`, `report_type`, `priority`, `key_insights`
   (list). Strongly recommended (populate when present in the document):
   `market_movers`, `sector_views`, `earnings_insights`, `macro_indicators`,
   `crypto_views`, `trade_ideas`, `risk_factors`, `vol_and_positioning`,
   `geopolitical`, `cross_bank_references`, `entities_mentioned`,
   `key_data_points`, `tension_points`, `theme_stances`, `charts_described`.
5. **Set `pdf_file_id` to match the integer in the filename** (the pull
   job validates this). Set `total_pages` to the sidecar's `page_count`.
   Leave `pages_analyzed` = `total_pages` (Anthropic's PDF Read consumes
   all pages). Token counts are best-effort estimates; if uncertain leave 0.
6. **Write the result** to `/tmp/bridge/result_<id>.json`. Keep one result
   object per file.

Apply the anti-hallucination rules in the system prompt rigorously. In
particular:
- `theme_stances` empty list is correct and common
- `evidence` field must be VERBATIM from the PDF (≤15 words)
- `vs_consensus` empty unless explicit consensus language in the report
- Do not fabricate price targets or numeric data points
- LOW-priority pieces (calendar wrappers, foreign FX dailies) — surface
  in `priority` field even though they were sent here as HIGH; the pull
  job won't reclassify but downstream synthesis filters by `priority`
  anyway.

If a PDF fails to parse, is corrupt, or you cannot produce a valid result
for any reason, write `/tmp/bridge/failed_<id>.json` instead, containing
`{"pdf_file_id": <id>, "reason": "<short string>"}`. The Railway pull
job routes those to the Gemini fallback path.

Process PDFs sequentially. Don't try to parallelize within one routine
fire — the per-fire token budget is the constraint, not wall time.

## STEP 5 — Commit results to the bridge branch

Use the GitHub Contents API to upload each result file. **Commit results
files BEFORE deleting input files** (defensive: if commit fails midway,
no input is lost).

```bash
python3 << 'PYEOF'
import json, base64, urllib.request, urllib.error, os, glob, time

GH_TOKEN = os.environ['GH_TOKEN']
REPO = 'gabjew90/Institutional-report-bot'
BRANCH = 'pulse-data'
hdr_get = {'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'}
hdr_put = {**hdr_get, 'Content-Type': 'application/json'}

def get_sha(path):
    """Returns existing sha for a path, or None if not present."""
    url = f'https://api.github.com/repos/{REPO}/contents/{path}?ref={BRANCH}'
    req = urllib.request.Request(url, headers=hdr_get)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get('sha')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise

def put_file(path, content_bytes, message):
    body = {
        'message': message,
        'content': base64.b64encode(content_bytes).decode('ascii'),
        'branch': BRANCH,
    }
    sha = get_sha(path)
    if sha:
        body['sha'] = sha
    req = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/contents/{path}',
        data=json.dumps(body).encode(), headers=hdr_put, method='PUT',
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

n_ok = 0
n_failed = 0

# Successful results → ingest-complete/
for path in sorted(glob.glob('/tmp/bridge/result_*.json')):
    pid = path.rsplit('result_', 1)[1].rsplit('.json', 1)[0]
    try:
        content = open(path, 'rb').read()
        # Validate JSON before pushing
        payload = json.loads(content.decode('utf-8'))
        if int(payload.get('pdf_file_id', 0)) != int(pid):
            print(f'WARN: result_{pid}.json has pdf_file_id={payload.get("pdf_file_id")} — fixing')
            payload['pdf_file_id'] = int(pid)
            content = json.dumps(payload, indent=1).encode('utf-8')
        put_file(f'ingest-complete/{pid}.json', content,
                 f'Routine: deep analysis result for PDF {pid}')
        n_ok += 1
        time.sleep(0.3)  # be polite to the API
    except Exception as e:
        print(f'ERROR: commit failed for result_{pid}: {e}')

# Hard failures → ingest-failed/
for path in sorted(glob.glob('/tmp/bridge/failed_*.json')):
    pid = path.rsplit('failed_', 1)[1].rsplit('.json', 1)[0]
    try:
        content = open(path, 'rb').read()
        put_file(f'ingest-failed/{pid}.json', content,
                 f'Routine: hard failure for PDF {pid}')
        n_failed += 1
        time.sleep(0.3)
    except Exception as e:
        print(f'ERROR: failure-marker commit failed for {pid}: {e}')

print(f'committed: {n_ok} results, {n_failed} hard failures')
PYEOF
```

The Railway pull job (every 2 min) will pick up `ingest-complete/*.json`,
INSERT into `pdf_analyses`, and prune both the result file and the
matching `ingest-pending/<id>.{pdf,json}` pair. We do NOT delete the
pending files here — Railway is the sole pruner.

## STEP 6 — Report

Output a single line:

```
Bridge ingestion complete: <N_OK> results committed, <N_FAILED> hard failures, <N_NOT_DOWNLOADED> not downloaded.
```

Where:
- `N_OK` = count of `result_*.json` successfully pushed to `ingest-complete/`
- `N_FAILED` = count of `failed_*.json` pushed to `ingest-failed/`
- `N_NOT_DOWNLOADED` = `len(ready_ids) - len(downloaded files)` from STEP 3

Do NOT add commentary beyond that line.
