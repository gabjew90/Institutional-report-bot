"""Extract Q/A pairs from /ask interaction log for compact QC review.

For each `## 2026-... UTC` block in the input file, emit:
  ---
  TIMESTAMP
  ASKER: <user> in <channel>
  Q: <last 200 chars of question — final user utterance>
  A: <full answer>
  ---

Skips the giant WHO'S TALKING / profile / chat-context blocks
(they're prompt scaffolding, not part of what the user saw).
"""

import re
import sys
from pathlib import Path


def split_interactions(text: str) -> list[tuple[str, str]]:
    """Split log into (header, body) tuples by `## 2026-...` markers."""
    out: list[tuple[str, str]] = []
    parts = re.split(r"(?m)^(## 2026-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)$", text)
    # parts[0] is preamble, then [header, body, header, body, ...]
    for i in range(1, len(parts), 2):
        header = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        out.append((header, body))
    return out


def extract_qa(body: str) -> dict:
    """Pull asker, question, answer from one interaction body."""
    asker_m = re.search(r"\*\*Asker:\*\* (.+)", body)
    asker = asker_m.group(1).strip() if asker_m else "?"

    # Question: between **Q:** and the next **A:** (skip any embedded
    # blockquote prompt-scaffold). The actual user utterance is usually
    # the LAST line before **A:** — bot prepends a reply-quote block.
    q_m = re.search(r"\*\*Q:\*\* (.+?)\n\*\*A:\*\*", body, flags=re.DOTALL)
    if q_m:
        q_full = q_m.group(1).strip()
        # The asker's actual message is the last non-empty line
        # in the Q block (everything before is reply-context).
        q_lines = [ln.strip() for ln in q_full.splitlines() if ln.strip()]
        q_user = q_lines[-1] if q_lines else q_full
    else:
        q_user = "(no Q found)"

    # Answer: between **A:** and the next <details> or end of body
    a_m = re.search(r"\*\*A:\*\*\s*(.+?)(?:\n<details>|\Z)", body, flags=re.DOTALL)
    answer = a_m.group(1).strip() if a_m else "(no A found)"
    return {"asker": asker, "q": q_user, "a": answer}


def main():
    if len(sys.argv) < 2:
        print("usage: extract_ask_qa.py <input.md>")
        sys.exit(1)
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    pairs = split_interactions(text)
    print(f"# /ask Q/A digest — {len(pairs)} interactions\n")
    for header, body in pairs:
        qa = extract_qa(body)
        # Truncate long answers for compact reading
        a = qa["a"]
        if len(a) > 1500:
            a = a[:1500] + f"\n...[+{len(qa['a']) - 1500} chars truncated]"
        # Truncate long questions
        q = qa["q"]
        if len(q) > 400:
            q = q[:400] + "...[truncated]"
        print(f"---\n{header}")
        print(f"**Asker:** {qa['asker']}")
        print(f"**Q:** {q}")
        print(f"**A:** {a}")
        print()


if __name__ == "__main__":
    main()
