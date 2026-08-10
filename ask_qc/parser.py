"""Parse a daily /ask interaction log into structured records.

The log format (see db.append_ask_log_entry):

  # /ask interactions - YYYY-MM-DD

  ## 2026-06-01 14:30:00 UTC

  **Asker:** {label} in #{channel}

  **Q:** {question - possibly multiline, ends at next **A:**}

  **A:**

  {answer - possibly multiline, ends at next \\n---\\n or EOF}

  <details>
  <summary>...</summary>

  {prompt_block - optional, ends at </details>}
  </details>

  ---

The parser is permissive: any block where the timestamp header
doesn't match the regex is dropped silently - log corruption shouldn't
break the QC pipeline."""

from __future__ import annotations
import re
from typing import Optional

from ask_qc.models import AskInteraction


# Per-interaction header: "## 2026-06-01 14:30:00 UTC"
_HEADER_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)\s*$",
    re.MULTILINE,
)

# Asker line: "**Asker:** {label} in #{channel}"
# {label} may be "kloh", "BK (`bankerkyle`)", "kloh (`kloh.`)", or "2pale"
_ASKER_RE = re.compile(
    r"\*\*Asker:\*\*\s*(?P<label>.+?)\s+in\s+(?P<channel>#\S+)",
)

# Username inside the asker label: "BK (`bankerkyle`)" -> bankerkyle
_USERNAME_RE = re.compile(r"`([^`]+)`")

# Route line: "**Route:** `LOCAL/BANTER` · ungrounded · filter-retry: failed
# · guards: —". Recorded system state, kept verbatim — the grader reads it
# to decide whether an interaction is gradable at all. Absent on legacy
# entries written before the line shipped.
_ROUTE_RE = re.compile(r"^\*\*Route:\*\*\s*(?P<meta>.+?)\s*$", re.MULTILINE)

# Question body: between "**Q:**" and the next "**A:**"
_QA_SPLIT_RE = re.compile(
    r"\*\*Q:\*\*\s*\n?(?P<q>.*?)\n\s*\*\*A:\*\*\s*\n+(?P<a>.*?)"
    r"(?=\n<details>|\n---\s*$|\Z)",
    re.DOTALL | re.MULTILINE,
)

# Optional <details>...</details> block
_DETAILS_RE = re.compile(
    r"<details>\s*<summary>.*?</summary>\s*(?P<body>.*?)\s*</details>",
    re.DOTALL,
)


def parse_ask_log(text: str) -> list[AskInteraction]:
    """Split `text` on timestamp headers, parse each block.

    Returns the successfully-parsed interactions in source order.
    Malformed blocks are silently dropped (best-effort recovery - the
    QC report header should note unparseable counts if the caller
    wants to surface them; the parser itself is lossy)."""
    if not text:
        return []

    # Split on the header regex. re.split with a capturing group keeps
    # the header text in the result list at odd indices, so the pattern
    # becomes: [preamble, ts1, body1, ts2, body2, ...].
    parts = _HEADER_RE.split(text)
    if len(parts) < 3:
        return []

    interactions: list[AskInteraction] = []
    # parts[0] is the file preamble (the "# /ask interactions -" line);
    # iterate the (ts, body) pairs that follow.
    for i in range(1, len(parts), 2):
        ts = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        parsed = _parse_block(ts, body)
        if parsed is not None:
            interactions.append(parsed)
    return interactions


def _parse_block(ts_utc: str, body: str) -> Optional[AskInteraction]:
    """Parse a single interaction body. Returns None on malformed input."""
    asker_m = _ASKER_RE.search(body)
    if not asker_m:
        return None
    label = asker_m.group("label").strip()
    channel = asker_m.group("channel").strip()
    username_m = _USERNAME_RE.search(label)
    asker_username = username_m.group(1).strip() if username_m else None

    qa_m = _QA_SPLIT_RE.search(body)
    if not qa_m:
        return None
    question = qa_m.group("q").strip()
    answer = qa_m.group("a").strip()

    details_m = _DETAILS_RE.search(body)
    prompt_block = details_m.group("body").strip() if details_m else None

    route_m = _ROUTE_RE.search(body)
    route_meta = route_m.group("meta").strip() if route_m else None

    return AskInteraction(
        ts_utc=ts_utc,
        asker_label=label,
        asker_username=asker_username,
        channel=channel,
        question=question,
        answer=answer,
        prompt_block=prompt_block,
        route_meta=route_meta,
    )
