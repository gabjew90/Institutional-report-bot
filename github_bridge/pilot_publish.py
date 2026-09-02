"""Publish HIGH-priority source text to the pilot branch (piece 1).

WHY THIS EXISTS
===============
The pilot's readers run on GitHub Actions and therefore cannot reach
the Railway volume or SQLite. Whatever they read, the worker must
publish. This is that publisher: extracted full text plus a small
metadata file per HIGH document, committed to the `pilot-data` orphan
branch (owner call 2026-09-01, option B).

SCOPE DISCIPLINE
================
- HIGH only. The pilot is HIGH-only by design; publishing MEDIUM would
  quietly change the experiment's corpus.
- Gated on `PILOT_PUBLISH_ENABLED`, default OFF. It stays off until
  day -2 of the shakedown, so merely deploying this cannot start
  filling a branch.
- Publishes to `PILOT_BRANCH`, never to the production `pulse-data`
  branch. A bug here must not be able to touch production artifacts;
  that isolation is the entire reason option B was chosen.
- Idempotent by path: a document already published is skipped, so a
  redeploy or a retry does not duplicate the corpus (which would
  double-count in every pilot metric).
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

# A single document's text, capped. Bank PDFs run long and the pilot
# reads them whole; the cap exists so one pathological 400-page
# appendix cannot blow up a commit.
MAX_TEXT_BYTES = 400_000


def _slug(title: str) -> str:
    """Filesystem- and shell-safe slug. Space-free deliberately: the
    reader workflow reads a TSV of paths, and a space in a filename
    would split a field."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (title or "").strip())
    return re.sub(r"-+", "-", s).strip("-")[:60].lower() or "untitled"


def publish_high_document(*, pdf_file_id: int, file_name: str,
                          source: str, title: str, priority: str,
                          published_at: str | None,
                          full_text: str) -> bool:
    """Commit one HIGH document's text + meta. True when published.

    Best-effort by contract: this runs inside the analysis pipeline
    and must never be able to fail an analysis. Every exit is a return,
    never a raise.
    """
    from config import settings
    if not getattr(settings, "pilot_publish_enabled", False):
        return False
    # HIGH feeds the readers (source-text/). MEDIUM is published to
    # source-text-all/ for the GRADERS only (2026-09-02): production's
    # pulse draws on MEDIUM documents too, and grading it against the
    # HIGH-only set marked its sentences "unsupported" for documents the
    # pilot simply never had. Readers never read source-text-all/. LOW is
    # not published.
    pri = (priority or "").strip().lower()
    if pri not in ("high", "medium"):
        return False

    try:
        from github_bridge import client as gh
        from scripts.pilot_config import PILOT_BRANCH, SOURCE_TEXT_ALL_DIR, SOURCE_TEXT_DIR

        base = SOURCE_TEXT_DIR if pri == "high" else SOURCE_TEXT_ALL_DIR
        date = (published_at or "")[:10] or _today()
        stem = f"{pdf_file_id}__{_slug(title or file_name)}"
        text_path = f"{base}/{date}/{stem}.txt"
        meta_path = f"{base}/{date}/{pdf_file_id}.meta.json"

        # Idempotence: a published document is never republished. A
        # duplicate would be read twice and double-count in every
        # pilot metric.
        if gh.get_file(meta_path, ref=PILOT_BRANCH):
            return False

        text = (full_text or "")
        truncated = len(text.encode("utf-8")) > MAX_TEXT_BYTES
        if truncated:
            text = text.encode("utf-8")[:MAX_TEXT_BYTES].decode(
                "utf-8", "ignore")

        gh.put_file(text_path, text,
                    f"pilot: source text {pdf_file_id} ({source})",
                    ref=PILOT_BRANCH)
        gh.put_file(meta_path, json.dumps({
            "pdf_file_id": pdf_file_id,
            "file_name": file_name,
            "source": source,
            "title": title,
            "priority": priority,
            "published_at": published_at,
            "text_path": text_path,
            "truncated": truncated,
        }, indent=1),
            f"pilot: meta {pdf_file_id}", ref=PILOT_BRANCH)
        log.info(f"pilot: published {pdf_file_id} ({source}) "
                 f"{len(text)} chars{' [truncated]' if truncated else ''}")
        return True
    except Exception as e:
        # Never fail an analysis over the pilot. A missed document is
        # one fewer card; a raised exception here would be a
        # production incident caused by an experiment.
        log.warning(f"pilot publish failed for {pdf_file_id} "
                    f"(non-fatal): {e}")
        return False


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
