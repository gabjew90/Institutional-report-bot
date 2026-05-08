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
TICKER_NORMALIZE: dict[str, str] = {
    "$SPX": "$SPY",
    "$NDX": "$QQQ",
    "$RUT": "$IWM",
}


def stitch(md: str) -> tuple[str, list[str]]:
    """Apply mechanical fixes to the DRAFT pulse. Returns (new_md, log_lines)."""
    new_md = md
    log: list[str] = []

    # Foreign cashtag scrub
    for ticker, name in FOREIGN_CASHTAGS.items():
        pattern = r'\$' + re.escape(ticker) + r'\b'
        count = len(re.findall(pattern, new_md))
        if count:
            new_md = re.sub(pattern, name, new_md)
            log.append(f'foreign-cashtag: stripped ${ticker} ({count}x) -> {name}')

    # Index → ETF normalization
    for old_t, new_t in TICKER_NORMALIZE.items():
        pattern = re.escape(old_t) + r'\b'
        count = len(re.findall(pattern, new_md))
        if count:
            new_md = re.sub(pattern, new_t, new_md)
            log.append(f'ticker-normalize: {old_t} -> {new_t} ({count}x)')

    # Placeholder presence check (informational — EDIT injects live RECAP)
    if '[LIVE PRICE RECAP]' in new_md:
        log.append('note: [LIVE PRICE RECAP] placeholder present (EDIT will inject live data)')

    return new_md, log


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
    new_md, log = stitch(md)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(new_md, encoding='utf-8')

    print(f"\nStitch pass: {len(log)} mechanical fix(es)")
    for line in log:
        print(f"  {line}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
