"""Mechanical pre-EDIT pass on the DRAFT pulse.

Runs after DRAFT, before EDIT. Applies deterministic fixes that don't
need LLM judgment, leaving the EDIT sub-agent free to focus on judgment-
based work (cull, add-theme, RECAP rewrite, voice scrub).

Fixes applied:
  - Foreign cashtag scrub: $TSCO → "Tesco", $CNA → "Centrica", etc.
    Twitter/X cashtags resolve to US listings only; using $TSCO when the
    underlying is UK Tesco mis-points to US Tractor Supply.
  - Ticker normalization: $SPX → $SPY, $NDX → $QQQ, $RUT → $IWM.
    The live snapshot uses ETF tickers; draft references should match
    so AUDIT-injected prices line up with the cashtags around them.

Notes:
  - Foreign-listed names with legitimate US ADRs ($SHEL, $BP, $NVS, etc.)
    are NOT stripped — they resolve correctly to the ADR.
  - The placeholder check is informational only; EDIT injects [LIVE PRICE
    RECAP] downstream.

Usage:
    python3 scripts/pulse_stitch.py <input_md> <output_md>

Example (from the routine):
    python3 scripts/pulse_stitch.py /tmp/draft.md /tmp/stitched.md
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Foreign-listed cashtags that resolve to a different US-listed company on
# Twitter/X. Strip the $ and replace with the company name.
FOREIGN_CASHTAGS: dict[str, str] = {
    "TSCO": "Tesco",            # UK grocery (US $TSCO = Tractor Supply)
    "AD": "Ahold Delhaize",     # Dutch (no US $AD listing)
    "CNA": "Centrica",          # UK utility (US $CNA = CNA Financial)
    "BA": "BAE Systems",        # UK (US $BA = Boeing)
    "BT": "BT Group",           # UK
    "RR": "Rolls-Royce",        # UK
    "III": "3i Group",          # UK
    "IMB": "Imperial Brands",   # UK
    "CCL": "Carnival UK",       # UK share class
    "REP": "Repsol",            # Spain
    "ORA": "Orange",            # French telecom
    "VOD": "Vodafone UK",       # ADR exists ($VOD) but research often refs LSE
}

# When the live market snapshot uses an ETF as the broad-market reference,
# normalize the index ticker in the draft to match. Avoids draft saying
# "$SPX +0.8%" when the snapshot used "$SPY +0.8%" as the source.
#
# CARVE-OUT: when the index ticker is paired with an index-scale LEVEL
# in the same sentence (e.g., "$SPX 7,375 zone", "$NDX 29,000 Fibonacci
# level"), DO NOT swap. The level is index-scale, not ETF-scale —
# blindly swapping the ticker creates "$SPY 7,375" which reads as a
# wrong-scale typo to a reader who knows $SPY trades around $738.
# Observed 2026-06-08 pulse: slot #1 had "$SPY futures' 7,375 zone is
# the must-hold for the systematic complex" — the level was correctly
# the $SPX number but the ticker was wrongly swapped to $SPY.
TICKER_NORMALIZE: dict[str, str] = {
    "$SPX": "$SPY",
    "$NDX": "$QQQ",
    "$RUT": "$IWM",
}


def _has_index_scale_level_nearby(text: str, max_chars: int = 60) -> bool:
    """True when `text` contains a 4+ digit number (allowing commas)
    within `max_chars` from the start. Used to detect when an index
    ticker is paired with an index-scale level vs an ETF-scale price.

    4+ digits covers all current US index levels: $SPX (4-5 digit),
    $NDX (5 digit), $RUT (4 digit), $DJI (5 digit). ETF prices are
    always <1000 in 2026 so a 4+ digit pattern is a reliable index-
    level signal.
    """
    # Bound to same sentence to avoid catching unrelated numbers in
    # a later clause.
    sentence_end = re.search(r'[.!?]\s', text[:max_chars])
    window = text[: sentence_end.start() if sentence_end else max_chars]
    for m in re.finditer(r'\b[\d,]+\b', window):
        digits = m.group(0).replace(',', '')
        if digits.isdigit() and len(digits) >= 4:
            return True
    return False


def stitch(md: str) -> tuple[str, list[str], list[str]]:
    """Apply mechanical fixes to the DRAFT pulse.

    Returns (new_md, fixes, notes):
      - fixes: list of strings describing actual file modifications
      - notes: list of strings for informational context that did NOT
        modify the file (e.g., placeholder presence). Counted separately
        so the summary line doesn't overstate what stitch actually did.
    """
    new_md = md
    fixes: list[str] = []
    notes: list[str] = []

    # Foreign cashtag scrub
    for ticker, name in FOREIGN_CASHTAGS.items():
        pattern = r'\$' + re.escape(ticker) + r'\b'
        count = len(re.findall(pattern, new_md))
        if count:
            new_md = re.sub(pattern, name, new_md)
            fixes.append(f'foreign-cashtag: stripped ${ticker} ({count}x) -> {name}')

    # Index → ETF normalization (with index-level carve-out).
    # When the index ticker is paired with an index-scale level in the
    # same sentence (e.g., "$SPX 7,375 zone"), preserve the original
    # ticker — swapping creates a wrong-scale read ("$SPY 7,375" when
    # $SPY trades around $738).
    for old_t, new_t in TICKER_NORMALIZE.items():
        pattern = re.compile(re.escape(old_t) + r'\b')

        # Use a substitution function so each match is decided based on
        # what immediately follows it (look ahead to detect index-scale
        # level vs ETF-scale price). re.sub iterates left-to-right on
        # the ORIGINAL string, so all match.end() positions are stable.
        swapped = 0
        preserved = 0
        original = new_md

        def _decide(match, original=original, new_t=new_t):
            nonlocal swapped, preserved
            pos = match.end()
            if _has_index_scale_level_nearby(original[pos:]):
                preserved += 1
                return match.group(0)  # preserve index ticker
            swapped += 1
            return new_t

        new_md = pattern.sub(_decide, new_md)
        if swapped:
            fixes.append(f'ticker-normalize: {old_t} -> {new_t} ({swapped}x)')
        if preserved:
            fixes.append(
                f'preserved {old_t} ({preserved}x — paired with '
                f'index-scale level, swap would be wrong-scale)'
            )

    # Strip the internal "## _DRAFT NOTES" section if DRAFT appended one.
    # DRAFT writes this section (after WHAT TO WATCH) to record which
    # adjudicated themes it folded/dropped and why — it's for the QC
    # reviewer (who reads the pre-stitch /tmp/draft.md) and must NOT
    # ship in the published pulse. Matches the header through to EOF or
    # the next `## ` header (there shouldn't be one after it, but be safe).
    notes_match = re.search(
        r'\n+##\s+_DRAFT NOTES\b.*?(?=\n##\s|\Z)',
        new_md, re.DOTALL | re.IGNORECASE,
    )
    if notes_match:
        new_md = new_md[: notes_match.start()].rstrip() + '\n'
        fixes.append('stripped ## _DRAFT NOTES section (internal — kept in /tmp/draft.md for QC)')

    # Placeholder presence note — informational only, no file change.
    # Tracked separately from fixes so the summary line doesn't overstate.
    if '[LIVE PRICE RECAP]' in new_md:
        notes.append('[LIVE PRICE RECAP] placeholder present (EDIT will inject live data)')

    return new_md, fixes, notes


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: pulse_stitch.py <input_md> <output_md>", file=sys.stderr)
        return 2

    input_md = Path(sys.argv[1])
    output_md = Path(sys.argv[2])

    if not input_md.exists():
        print(f"input markdown not found: {input_md}", file=sys.stderr)
        return 1

    md = input_md.read_text(encoding='utf-8')
    new_md, fixes, notes = stitch(md)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(new_md, encoding='utf-8')

    if fixes:
        print(f"\nStitch pass: {len(fixes)} mechanical fix(es) applied")
        for line in fixes:
            print(f"  fix: {line}")
    else:
        print("\nStitch pass: no mechanical fixes needed (file unchanged)")
    for line in notes:
        print(f"  note: {line}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
