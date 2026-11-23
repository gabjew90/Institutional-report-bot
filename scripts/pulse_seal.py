"""The seal that ties a published pulse to the text the checks passed.

Why it exists (2026-11-23). The routine cannot write to GitHub from code:
every file it commits is passed by the model as an argument to a GitHub
tool call, so the model types the pulse out. The check trail travels the
same way, and of 51 trails on the branch at least ten were rewritten by
the model on the way ("abridged by the routine for transport"). A bridge
that read the trail would be reading the model's own account of its run.

So the evidence rides in the pulse file itself. `pulse_driver.py compose`
builds the published file in code, only after a preflight PASS and only
from the final.md that preflight saw, and writes this seal into its
frontmatter. The bridge recomputes it over the file that arrived
(`github_bridge/pulse_gate.py`). A file with no seal was not built by the
driver after a pass. A seal that does not match means the text changed
after the checks ran, which includes a slip in the model's transcription.

It is a checksum, not a signature: a model that set out to forge it could
compute one. The failures on record are skipped gates and edits made
after the last check, not forgery.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

SEAL_KEY = "gate_seal"
_SEAL_LINE_RE = re.compile(r"(?m)^" + SEAL_KEY + r":[ \t]*([0-9a-f]*)[ \t]*\r?\n?")
_HEX = 16


def normalize(text: str) -> str:
    """The text as the seal sees it. Differences a faithful copy can pick
    up in transport do not count: line endings, trailing spaces, runs of
    blank lines, blank lines at either end, and Unicode composition."""
    t = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [l.rstrip() for l in t.split("\n")]
    out: list[str] = []
    for l in lines:
        if l == "" and (not out or out[-1] == ""):
            continue
        out.append(l)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def without_seal(text: str) -> str:
    return _SEAL_LINE_RE.sub("", text or "", count=1)


def read_seal(text: str) -> str | None:
    """The seal written in `text`, or None when it carries none. Only the
    frontmatter is read: a `gate_seal:` line in the body is prose."""
    t = (text or "").replace("\r\n", "\n")
    if not t.startswith("---\n"):
        return None
    end = t.find("\n---", 4)
    if end < 0:
        return None
    m = _SEAL_LINE_RE.search(t[:end + 1])
    return m.group(1) if m else None


def compute(text: str) -> str:
    """The seal for `text` (the whole file, frontmatter included, with any
    seal line left out)."""
    return hashlib.sha256(normalize(without_seal(text)).encode("utf-8")).hexdigest()[:_HEX]


def stamp(text: str) -> str:
    """`text` with its seal written as the last frontmatter line."""
    t = without_seal(text)
    if not t.startswith("---\n"):
        raise ValueError("no frontmatter to stamp")
    end = t.find("\n---", 4)
    if end < 0:
        raise ValueError("frontmatter is not closed")
    return t[:end + 1] + f"{SEAL_KEY}: {compute(t)}\n" + t[end + 1:]


def mark_checked(text: str, passed: bool) -> str:
    """`text` with the seal line replaced by the bridge's verdict. The
    bridge rewrites the body before it archives the pulse (trade board,
    section split), so the seal no longer describes the archived file.
    The verdict does, and makes the archive the record of the check."""
    line = f"gate_check: {'pass' if passed else 'failed'}\n"
    if read_seal(text) is not None:
        return _SEAL_LINE_RE.sub(line, text, count=1)
    t = text or ""
    if t.startswith("---\n") and t.find("\n---", 4) >= 0:
        end = t.find("\n---", 4)
        return t[:end + 1] + line + t[end + 1:]
    return t


def problem(text: str) -> str | None:
    """Why this file must not be trusted as the checked text; None when
    its seal matches."""
    seal = read_seal(text)
    if seal is None:
        return ("no gate seal: the published file was not built by the driver "
                "after a preflight PASS")
    want = compute(text)
    if seal != want:
        return (f"the text differs from the text the checks passed "
                f"(seal {seal or 'empty'}, this text gives {want})")
    return None
