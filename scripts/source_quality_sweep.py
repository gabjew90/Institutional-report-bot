#!/usr/bin/env python3
"""Run the source-quality classifier over every recorded answer's
Sources footer — review session 4's mandated look at the
false-positive surface BEFORE the check ever touches production
behavior.

The production check is warn-only and fires when EVERY cited domain
falls outside the sane list (discord_bot.bot._SANE_SOURCE_DOMAINS).
This sweep applies the same rule to the citations embedded in the
recorded corpus (docs/ask-*.json answers carry their footers, since
the harness scores what the user sees) and prints each flag with its
domains and question, for reading — not pass/fail.

USAGE
=====
    python scripts/source_quality_sweep.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from discord_bot.bot import _domain_is_sane  # noqa: E402

# Markdown citation lines as the footer renders them:
#   [1] [Title](<https://url>)
_CITE_RE = re.compile(r"\[\d+\]\s+\[([^\]]+)\]\(<?([^)>\s]+)>?\)")


def _domains_from_answer(answer: str) -> list[str]:
    from urllib.parse import urlparse
    out = []
    for title, url in _CITE_RE.findall(answer or ""):
        title = title.strip().lower()
        host = ""
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            pass
        if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", title) and (
                not host or "vertexaisearch" in host
                or host.endswith("google.com")):
            out.append(title)
        elif host and "vertexaisearch" not in host \
                and not host.endswith("google.com"):
            out.append(host)
        elif title:
            out.append(title)
    return out


def main() -> int:
    seen, records = set(), []
    for path in sorted(glob.glob(os.path.join(REPO, "docs", "ask-*.json"))):
        try:
            d = json.loads(open(path, encoding="utf-8").read())
        except Exception:
            continue
        for f in d.get("fixtures") or []:
            ans = (f.get("answer") or "").strip()
            key = (f.get("id"), ans)
            if not ans or key in seen:
                continue
            seen.add(key)
            records.append((f.get("id"), f.get("question") or "", ans))

    cited = flagged = 0
    flags = []
    for fid, q, ans in records:
        domains = _domains_from_answer(ans)
        if not domains:
            continue
        cited += 1
        if not any(_domain_is_sane(d) for d in domains):
            flagged += 1
            flags.append((fid, sorted(set(domains)), q))

    print(f"=== source-quality sweep over {len(records)} recorded "
          f"answers ===")
    print(f"answers with citations: {cited}")
    print(f"flagged (ALL domains unlisted): {flagged}\n")
    for fid, doms, q in flags:
        print(f"  {fid:<34} {','.join(doms)[:60]:<62} q={q[:50]!r}")
    if cited:
        print(f"\nflag rate among cited answers: "
              f"{100.0 * flagged / cited:.1f}%")
    print("\nRead every flag above. A legitimate niche source flagged "
          "here is the long tail the warn-only period exists to size.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
