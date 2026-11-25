#!/usr/bin/env bash
# One Omnipulse reader: read ONE document, verify its cards, re-ask once,
# commit. Called by scripts/pilot_read.py, several at a time.
#
#   pilot_read_one.sh ID TEXT MODEL TIER DATE     exit 0 read, 3 failed, 4 failed and retired
#   pilot_read_one.sh --ops TOTAL FAILED          write the run's ops record and commit it
#
# This is the body of the loop that lived in .github/workflows/pilot-readers.yml
# until 2026-11-25, moved here so documents can be read in parallel. The
# reading steps and their order are unchanged: known labels, the reader prompt
# (docs/superpowers/routines/pilot/reader.md, untouched), finalize, anchor
# verify, one re-ask, final verify. What changed, and why:
#
#   - Each document has its own temp folder. The loop used fixed /tmp names.
#   - The reader runs with read-only tools (Read, Grep, Glob), no MCP servers
#     and no skills, from the pilot-data checkout. It used to run from the
#     repository root with every tool allowed (bypassPermissions) on bank PDF
#     text, beside a token that can push to any branch, and loaded this
#     repository's CLAUDE.md (about 31,000 tokens) on every one of its 20 to
#     50 turns. pilot-data has no CLAUDE.md and no settings.
#   - Every git command and every write to a file git already tracks happens
#     under one lock, because a rebase refuses a tree someone is writing to.
#
# PILOT_DRY=1 writes nothing to the checkout and commits nothing (speed test).
set -u

ROOT="pilot-data/${PILOT_ROOT:-pilot}"
LOCK="${RUNNER_TEMP:-/tmp}/pilot-readers-git.lock"
DRY="${PILOT_DRY:-0}"
READER_PROMPT="docs/superpowers/routines/pilot/reader.md"

locked () { ( flock 9; "$@" ) 9>"$LOCK"; }

persist () {  # $1 = commit message suffix
  [ "$DRY" = "1" ] && return 0
  for d in cards ops read-failures; do
    [ -d "$ROOT/$d" ] && git -C pilot-data add "${PILOT_ROOT:-pilot}/$d/" 2>/dev/null
  done
  if git -C pilot-data diff --cached --quiet; then return 0; fi
  git -C pilot-data commit -q -m "pilot: reader cards ($1)" || return 0
  for attempt in 1 2 3; do
    git -C pilot-data pull --rebase --quiet origin pilot-data || true
    if git -C pilot-data push -q origin pilot-data; then return 0; fi
    sleep 3
  done
  echo "::warning::could not push after $1; the next commit retries"
}

if [ "${1:-}" = "--ops" ]; then
  ops () {
    python scripts/pilot_ops_record.py --root "$ROOT" --total "$1" --failed "$2" || true
    persist "final $1 doc(s)"
  }
  [ "$DRY" = "1" ] || locked ops "$2" "$3"
  exit 0
fi

ID="$1"; TEXT="$2"; MODEL="$3"; TIER="$4"; DATE="$5"
WORK="$(mktemp -d "${RUNNER_TEMP:-/tmp}/reader-$ID-XXXXXX")"
if [ "$DRY" = "1" ]; then
  OUT="$WORK/cards.json"
else
  OUT="$ROOT/cards/$DATE/$ID.json"
  mkdir -p "$(dirname "$OUT")"
fi

# Every failure path records an attempt. After MAX_READ_ATTEMPTS
# (pilot_config) the document leaves the unread list (2026-09-05).
fail () {  # reason
  echo "::warning::$1 for $ID"
  [ "$DRY" = "1" ] && exit 3
  note () {
    out=$(python scripts/pilot_read_failure.py --root "$ROOT" --id "$ID" --date "$DATE" --reason "$1") || true
    echo "$out"
    persist "$ID failed"
    case "$out" in *given_up=True*) return 4;; esac
    return 3
  }
  locked note "$1"
  exit $?
}

reader () {  # prompt file -> $WORK/reader_out.txt
  # From the pilot-data checkout: the source path in the prompt is relative
  # to it, reads inside the working folder need no permission, and nothing
  # of the code repository is loaded.
  ( cd pilot-data && claude -p --model "$MODEL" \
      --tools "Read,Grep,Glob" --strict-mcp-config --disable-slash-commands \
      --no-session-persistence --max-turns "$MAXT" \
      < "$1" > "$WORK/reader_out.txt" 2>"$WORK/reader_err.txt" ) || true
}

# The running label list (spec section 4 amendment 2026-09-10): every label
# already in the ledger window, rebuilt per document so earlier reads of this
# run are in it. With several readers at once, a document does not see the
# labels of the ones being read beside it.
python scripts/pilot_known_labels.py --root "$ROOT" --date "$DATE" --out "$WORK/known_labels.txt" \
  || printf '(unavailable this run)\n' > "$WORK/known_labels.txt"

{
  cat "$READER_PROMPT"
  printf '\n\nSOURCE_TEXT_PATH=%s\n' "${TEXT#pilot-data/}"
  printf '\nKNOWN_LABELS (already in this ledger window, most-used first; reuse verbatim when the subject matches at pulse-theme grain):\n'
  cat "$WORK/known_labels.txt"
} > "$WORK/reader_prompt.txt"

# Turn budget scales with the document (history in the workflow file's
# git log, 2026-09-02 to 09-07: byte and line thresholds, floor of 20).
BYTES=$(wc -c < "$TEXT" 2>/dev/null || echo 0)
LINES=$(wc -l < "$TEXT" 2>/dev/null || echo 0)
MAXT=20
if [ "$BYTES" -gt 50000 ] || [ "$LINES" -gt 1500 ]; then MAXT=30; fi
if [ "$BYTES" -gt 150000 ] || [ "$LINES" -gt 3500 ]; then MAXT=40; fi
if [ "$BYTES" -gt 300000 ]; then MAXT=50; fi

reader "$WORK/reader_prompt.txt"

if ! python scripts/pilot_finalize_read.py \
    --raw "$WORK/reader_out.txt" --out "$OUT" \
    --source "$TEXT" --tier "$TIER" --model "$MODEL" \
    --prompt "$READER_PROMPT"; then
  # stdout too: the CLI reports usage limits, auth and max-turns there (2026-09-04).
  echo "reader stdout head:"; head -c 300 "$WORK/reader_out.txt" || true; echo
  tail -3 "$WORK/reader_err.txt" || true
  fail "reader: $(head -c 120 "$WORK/reader_out.txt" | tr '\n' ' ')"
fi

# Pass 1: verify anchors, collect failures for ONE re-ask, in-session
# (spec section 3: a deferred re-ask is a card lost for hours).
if ! python scripts/pilot_verify_cards.py "$OUT" "$TEXT" --reask-out "$WORK/reask.json"; then
  fail "anchor verify pass 1"
fi
NEEDS=$(python -c "import json,sys;print(json.load(open(sys.argv[1]))['verify']['needs_reask'])" "$OUT" 2>/dev/null || echo Error)
if [ "$NEEDS" = "Error" ]; then
  fail "verify block unreadable"
fi
if [ "$NEEDS" = "True" ]; then
  {
    cat "$WORK/reader_prompt.txt"
    printf '\n[RE-ASK] failed_cards had anchors NOT found in the source; label_contract, when present, names a topic or macro_key contract breach:\n'
    cat "$WORK/reask.json"
    printf '\nRe-emit the COMPLETE artifact. For each failed card, either copy its anchor character-for-character from the source or omit that card. Apply the label_contract instruction if there is one. Change nothing else.\n'
  } > "$WORK/reask_prompt.txt"
  reader "$WORK/reask_prompt.txt"
  python scripts/pilot_finalize_read.py \
    --raw "$WORK/reader_out.txt" --out "$OUT" \
    --source "$TEXT" --tier "$TIER" --model "$MODEL" \
    --prompt "$READER_PROMPT" || true
fi
# Pass 2: drop whatever still fails, and count the drops.
if ! python scripts/pilot_verify_cards.py "$OUT" "$TEXT" --final; then
  fail "anchor verify final"
fi
if [ "$DRY" = "1" ]; then
  python -c "import json,sys;d=json.load(open(sys.argv[1]));print('dry run', sys.argv[2], 'cards:', len(d.get('cards') or []), '| verify:', {k: v for k, v in (d.get('verify') or {}).items() if isinstance(v, (int, bool))})" "$OUT" "$ID" || true
  exit 0
fi
locked persist "$ID"
exit 0
